from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.core.config import GroupReplyConfig, config
from src.core.message_trace import get_execution_key
from src.core.models import MessageEvent, MessageHandlingPlan, MessagePlanAction
from src.core.platform_normalizers import get_attached_inbound_event
from src.core.runtime_metrics import RuntimeMetrics
from src.handlers.conversation_engagement import build_message_observations
from src.handlers.conversation_planner import ConversationPlanner
from src.handlers.conversation_session_manager import ConversationSessionManager
from src.handlers.message_context import MessageContext
from src.handlers.conversation_window_models import BufferedWindow
from src.handlers.temporal_context import build_temporal_context, normalize_event_time

logger = logging.getLogger(__name__)

ImageAnalyzer = Callable[[MessageEvent, str, str], Awaitable[Dict[str, Any]]]
EventTextGetter = Callable[[MessageEvent], str]
SenderDisplayNameGetter = Callable[[MessageEvent], str]
HasImageGetter = Callable[[MessageEvent], bool]
ImageCountGetter = Callable[[MessageEvent], int]
ImageFileIdsGetter = Callable[[MessageEvent], List[str]]


class ConversationPlanCoordinator:
    """Coordinate group history, vision enrichment, and planner calls for group messages."""

    def __init__(
        self,
        planner: ConversationPlanner,
        session_manager: ConversationSessionManager,
        *,
        runtime_metrics: Optional[RuntimeMetrics] = None,
        group_reply_config: Optional[GroupReplyConfig] = None,
        image_analyzer: Optional[ImageAnalyzer] = None,
        event_text_getter: Optional[EventTextGetter] = None,
        sender_display_name_getter: Optional[SenderDisplayNameGetter] = None,
        has_image_getter: Optional[HasImageGetter] = None,
        image_count_getter: Optional[ImageCountGetter] = None,
        image_file_ids_getter: Optional[ImageFileIdsGetter] = None,
        context_window_size: int = 10,
        conversation_store: Optional[Any] = None,
    ) -> None:
        self.planner = planner
        self.session_manager = session_manager
        self.runtime_metrics = runtime_metrics
        self.group_reply_config = group_reply_config or config.app.group_reply
        self.image_analyzer = image_analyzer
        self.event_text_getter = event_text_getter
        self.sender_display_name_getter = sender_display_name_getter
        self.has_image_getter = has_image_getter
        self.image_count_getter = image_count_getter
        self.image_file_ids_getter = image_file_ids_getter
        self.context_window_size = max(0, int(context_window_size or 0))
        self._conversation_store = conversation_store

        self.conversation_plan_locks: Dict[str, asyncio.Lock] = {}
        max_parallel = max(1, int(self.group_reply_config.plan_request_max_parallel or 1))
        self.conversation_plan_max_parallel = max_parallel
        self.conversation_plan_semaphore = asyncio.Semaphore(max_parallel)

    async def close(self) -> None:
        """No-op: group history is persisted in SQLite; no in-memory state to clear."""
        pass

    def build_planner_message_info(
        self,
        event: MessageEvent,
        clean_text: str,
        image_analysis: Optional[Dict[str, Any]] = None,
        include_image_context: bool = True,
    ) -> Dict[str, Any]:
        clean_text = clean_text.strip()
        raw_text = self._event_text(event).strip()
        image_count = self._image_count(event)
        has_image = image_count > 0
        text_present = bool(clean_text)
        effective_has_image = has_image and include_image_context
        image_analysis = dict(image_analysis or {})
        per_image_descriptions = [
            str(item).strip()
            for item in image_analysis.get("per_image_descriptions", [])
            if str(item).strip()
        ]
        merged_description = str(image_analysis.get("merged_description", "")).strip()
        vision_available = bool(image_analysis.get("vision_available"))
        vision_failure_count = int(image_analysis.get("vision_failure_count", 0) or 0)
        vision_success_count = int(image_analysis.get("vision_success_count", 0) or 0)
        vision_source = str(image_analysis.get("vision_source", "")).strip()
        vision_error = str(image_analysis.get("vision_error", "")).strip()

        if effective_has_image and text_present:
            message_shape = "text_with_image"
        elif effective_has_image:
            message_shape = "image_only"
        elif has_image and text_present:
            message_shape = "text_with_image"
        elif has_image:
            message_shape = "image_only"
        else:
            message_shape = "text_only"

        planner_text = clean_text
        display_text = clean_text
        image_placeholder = ""
        if has_image:
            # 优先使用 vision 分析结果中的图片描述
            image_desc = str(merged_description or "").strip()
            if image_desc:
                image_placeholder = f"[图片描述：{image_desc}]"
            else:
                image_placeholder = "[图片]" if image_count == 1 else f"[图片 x{image_count}]"
            planner_text = f"{clean_text} {image_placeholder}".strip() if clean_text else image_placeholder

        if has_image:
            display_text = f"{clean_text} {image_placeholder}".strip() if clean_text else image_placeholder

        if merged_description:
            planner_text = f"{planner_text}\n图片摘要: {merged_description}".strip()
        elif per_image_descriptions:
            planner_text = f"{planner_text}\n图片描述: {'；'.join(per_image_descriptions)}".strip()
        elif effective_has_image and vision_failure_count > 0:
            planner_text = f"{planner_text}\n图片理解状态: 识图失败".strip()

        if not planner_text:
            planner_text = "[空]"

        return {
            "event_time": self._event_time(event),
            "text": planner_text,
            "display_text": display_text,
            "text_content": clean_text,
            "raw_text": raw_text,
            "has_image": has_image,
            "raw_has_image": has_image,
            "image_context_enabled": effective_has_image,
            "image_count": image_count if effective_has_image else 0,
            "raw_image_count": image_count,
            "text_present": text_present,
            "is_image_only": message_shape == "image_only",
            "message_shape": message_shape,
            "image_file_ids": self._image_file_ids(event) if effective_has_image else [],
            "per_image_descriptions": per_image_descriptions,
            "merged_description": merged_description,
            "vision_available": vision_available,
            "vision_failure_count": vision_failure_count,
            "vision_success_count": vision_success_count,
            "vision_source": vision_source,
            "vision_error": vision_error,
        }

    def format_window_context(self, reply_context: Optional[Dict[str, Any]]) -> str:
        if not reply_context:
            return ""
        window_messages = reply_context.get("window_messages") or []
        if not window_messages:
            return ""

        lines = [
            "=== 当前群聊最近上下文（按时间顺序）===",
            "如果你决定回复，请结合上下文自然接话。最近记录里可能包含助手自己之前的发言。",
        ]
        for index, item in enumerate(window_messages, 1):
            speaker = self._format_window_speaker(item)
            text = self._window_display_text(item)
            latest_note = " [当前消息]" if item.get("is_latest") else ""
            lines.append(f"{index}. {speaker}: {text}{latest_note}")

            merged_description = str(item.get("merged_description") or "").strip()
            if merged_description:
                lines.append(f"   图片摘要: {merged_description}")
            for image_index, description in enumerate(item.get("per_image_descriptions") or [], 1):
                lines.append(f"   第{image_index}张: {description}")
            if item.get("has_image") and item.get("vision_failure_count", 0) and not item.get("vision_available"):
                lines.append("   图片理解: 失败")
        lines.append("")
        return "\n".join(lines)

    async def build_message_context(
        self,
        *,
        event: MessageEvent,
        user_message: str,
        trace_id: str = "",
    ) -> MessageContext:
        history_key = self._history_key(event)
        history_items = await self._get_recent_history(history_key)
        current_message = await self._build_current_message(event=event, user_message=user_message, trace_id=trace_id)
        window_messages = self._compose_window_messages(history_items, current_message)
        execution_key = get_execution_key(event)
        conversation_key = self.session_manager.get_key(event)
        previous_message_time = self._message_event_time(history_items[-1]) if history_items else 0.0
        history_event_times = [item.get("event_time", 0.0) for item in window_messages]
        temporal_context = build_temporal_context(
            current_event_time=current_message.get("event_time", 0.0),
            chat_mode="group",
            previous_message_time=previous_message_time,
            conversation_last_time=previous_message_time,
            history_event_times=history_event_times,
        )
        planning_signals = self._build_planning_signals(window_messages=window_messages, temporal_context=temporal_context)
        reply_context = self._merge_reply_context(
            self._build_window_reply_context(window_messages, assistant_self_id=event.self_id),
            self._build_planner_batch_context(
                group_id=history_key,
                mode="single",
                batch_size=1,
                is_latest=True,
            ),
        )
        return MessageContext(
            trace_id=trace_id,
            execution_key=execution_key,
            conversation_key=conversation_key,
            user_message=str(current_message.get("text_content") or user_message or "").strip(),
            current_sender_label=self._format_window_speaker(current_message).replace("用户 ", "", 1),
            is_first_turn=False,
            current_event_time=float(current_message.get("event_time", 0.0) or 0.0),
            previous_message_time=float(previous_message_time or 0.0),
            conversation_last_time=float(previous_message_time or 0.0),
            temporal_context=temporal_context,
            window_messages=window_messages,
            recent_history_text=self._build_recent_history_text(window_messages),
            vision_analysis={
                "per_image_descriptions": list(current_message.get("per_image_descriptions") or []),
                "merged_description": str(current_message.get("merged_description", "") or ""),
                "vision_success_count": int(current_message.get("vision_success_count", 0) or 0),
                "vision_failure_count": int(current_message.get("vision_failure_count", 0) or 0),
                "vision_source": str(current_message.get("vision_source", "") or ""),
                "vision_error": str(current_message.get("vision_error", "") or ""),
                "vision_available": bool(current_message.get("vision_available", False)),
            },
            reply_context=reply_context,
            planning_signals=planning_signals,
        )

    async def plan_message(self, event: MessageEvent, user_message: str, *, trace_id: str = "") -> MessageHandlingPlan:
        context = await self.build_message_context(event=event, user_message=user_message, trace_id=trace_id)
        group_id = self._history_key(event)
        window_messages = list(context.window_messages or [])

        try:
            plan = await self._plan_with_window_messages(
                event=event,
                user_message=user_message,
                recent_messages=[],
                window_messages=window_messages,
                context=context,
            )
        finally:
            if window_messages:
                await self._record_user_message(group_id, window_messages[-1])

        reply_context = dict(context.reply_context or {})
        reply_mode = "proactive" if plan.should_reply else ""
        if reply_mode:
            reply_context["reply_mode"] = reply_mode
        reply_goal = str(getattr(getattr(plan, "prompt_plan", None), "reply_goal", "") or "").strip()
        if reply_goal:
            reply_context["reply_goal"] = reply_goal
        if context.planning_signals:
            reply_context["planning_signals"] = dict(context.planning_signals)
        if trace_id:
            reply_context["trace_id"] = trace_id
        plan = self._clone_plan(plan, reply_context=reply_context)
        self._record_plan_metric(plan.action)
        return plan

    async def plan_buffered_window(
        self,
        *,
        event: MessageEvent,
        window: BufferedWindow,
        trace_id: str = "",
    ) -> MessageHandlingPlan:
        context = await self._build_buffered_window_context(event=event, window=window, trace_id=trace_id)
        group_id = self._history_key(event)
        window_messages = list(context.window_messages or [])
        planner_user_message = str(window.merged_user_message or context.user_message or "").strip()

        try:
            plan = await self._plan_with_window_messages(
                event=event,
                user_message=planner_user_message,
                recent_messages=[],
                window_messages=window_messages,
                context=context,
            )
        finally:
            for item in list(window.messages or []):
                await self._record_user_message(group_id, item)

        reply_context = dict(context.reply_context or {})
        reply_mode = "proactive" if plan.should_reply else ""
        if reply_mode:
            reply_context["reply_mode"] = reply_mode
        reply_goal = str(getattr(getattr(plan, "prompt_plan", None), "reply_goal", "") or "").strip()
        if reply_goal:
            reply_context["reply_goal"] = reply_goal
        if context.planning_signals:
            reply_context["planning_signals"] = dict(context.planning_signals)
        if trace_id:
            reply_context["trace_id"] = trace_id
        plan = self._clone_plan(plan, reply_context=reply_context)
        self._record_plan_metric(plan.action)
        return plan

    async def build_direct_reply_context(
        self,
        *,
        event: MessageEvent,
        user_message: str,
        reply_mode: str,
        planner_mode: str = "direct",
        trace_id: str = "",
    ) -> Dict[str, Any]:
        context = await self.build_message_context(event=event, user_message=user_message, trace_id=trace_id)
        group_id = self._history_key(event)
        window_messages = list(context.window_messages or [])

        try:
            reply_context = self._merge_reply_context(
                dict(context.reply_context or {}),
                self._build_planner_batch_context(
                    group_id=group_id,
                    mode=planner_mode,
                    batch_size=1,
                    is_latest=True,
                ),
            )
            reply_context["reply_mode"] = reply_mode
            if trace_id:
                reply_context["trace_id"] = trace_id
            return reply_context
        finally:
            if window_messages:
                await self._record_user_message(group_id, window_messages[-1])

    async def record_assistant_reply(self, group_id: Optional[int], message: str) -> None:
        text = str(message or "").strip()
        if group_id is None or not text:
            return
        group_key = f"group:{group_id}"  # 与 add_turn 的 _dialogue_key 格式保持一致
        item = {
            "message_id": 0,
            "user_id": "",
            "speaker_role": "assistant",
            "speaker_name": config.get_assistant_name(),
            "event_time": time.time(),
            "text": text,
            "display_text": text,
            "text_content": text,
            "raw_text": text,
            "has_image": False,
            "raw_has_image": False,
            "image_context_enabled": False,
            "image_count": 0,
            "raw_image_count": 0,
            "text_present": bool(text),
            "is_image_only": False,
            "message_shape": "text_only",
            "image_file_ids": [],
            "per_image_descriptions": [],
            "merged_description": "",
            "vision_available": False,
            "vision_failure_count": 0,
            "vision_success_count": 0,
            "vision_source": "",
            "vision_error": "",
        }
        if self._conversation_store is not None:
            await self._conversation_store.add_group_message(group_key, item)

    def _get_plan_request_interval(self) -> float:
        return max(0.0, float(self.group_reply_config.plan_request_interval or 0))

    def _get_context_window_size(self) -> int:
        return self.context_window_size

    def _event_time(self, event: MessageEvent) -> float:
        return normalize_event_time(getattr(event, "time", 0.0)) or time.time()

    def _message_event_time(self, item: Optional[Dict[str, Any]]) -> float:
        return normalize_event_time((item or {}).get("event_time", 0.0))

    async def _get_recent_history(self, group_id: str) -> List[Dict[str, Any]]:
        count = self._get_context_window_size()
        if count <= 0:
            return []
        if self._conversation_store is None:
            return []
        messages = await self._conversation_store.get_recent_group_messages(group_id, limit=count)
        return messages

    def _history_key(self, event: MessageEvent) -> str:
        inbound_event = get_attached_inbound_event(event)
        if inbound_event is not None:
            session = inbound_event.session
            channel_id = str(session.channel_id or "").strip()
            if channel_id:
                if session.platform:
                    return f"{session.platform}:{session.scope}:{channel_id}"
                return f"{session.scope}:{channel_id}"
            return session.qualified_key
        group_id = event.raw_data.get("group_id", "unknown_group")
        return f"group:{group_id}"

    async def _build_current_message(self, *, event: MessageEvent, user_message: str, trace_id: str = "") -> Dict[str, Any]:
        image_analysis = await self._analyze_images_for_event(event, user_message, trace_id=trace_id)
        planner_message = self.build_planner_message_info(
            event,
            user_message,
            image_analysis=image_analysis,
            include_image_context=self.image_analyzer is not None,
        )
        display_name = self._sender_display_name(event)
        return {
            "message_id": int(event.message_id or 0),
            "user_id": str(event.user_id),
            "speaker_role": "user",
            "speaker_name": display_name,
            **planner_message,
        }

    async def _analyze_images_for_event(self, event: MessageEvent, user_message: str, *, trace_id: str = "") -> Dict[str, Any]:
        if not self._has_image(event) or self.image_analyzer is None:
            return {}
        try:
            try:
                return await self.image_analyzer(event, user_message, trace_id)
            except TypeError as exc:
                if "positional arguments" not in str(exc):
                    raise
                return await self.image_analyzer(event, user_message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[协调器] 规划前图片分析失败")
            return {
                "per_image_descriptions": [],
                "merged_description": "",
                "vision_success_count": 0,
                "vision_failure_count": self._image_count(event),
                "vision_source": "vision_error",
                "vision_error": str(exc),
                "vision_available": False,
            }

    def _event_text(self, event: MessageEvent) -> str:
        if self.event_text_getter is not None:
            return str(self.event_text_getter(event) or "")
        return event.extract_text()

    def _sender_display_name(self, event: MessageEvent) -> str:
        if self.sender_display_name_getter is not None:
            return str(self.sender_display_name_getter(event) or "")
        return event.get_sender_display_name()

    def _has_image(self, event: MessageEvent) -> bool:
        if self.has_image_getter is not None:
            return bool(self.has_image_getter(event))
        return event.has_image()

    def _image_count(self, event: MessageEvent) -> int:
        if self.image_count_getter is not None:
            return max(0, int(self.image_count_getter(event) or 0))
        return len(event.get_image_segments())

    def _image_file_ids(self, event: MessageEvent) -> List[str]:
        if self.image_file_ids_getter is not None:
            return [str(item) for item in self.image_file_ids_getter(event) if str(item or "")]
        return event.get_image_file_ids()

    def _compose_window_messages(
        self,
        history_items: List[Dict[str, Any]],
        current_message: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        window_messages: List[Dict[str, Any]] = []
        for item in history_items:
            copied = dict(item)
            copied["is_latest"] = False
            window_messages.append(copied)
        latest = dict(current_message)
        latest["is_latest"] = True
        window_messages.append(latest)
        return window_messages

    def _compose_buffered_window_messages(
        self,
        history_items: List[Dict[str, Any]],
        buffered_messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        window_messages: List[Dict[str, Any]] = []
        for item in history_items:
            copied = dict(item)
            copied["is_latest"] = False
            window_messages.append(copied)
        buffered = [dict(item) for item in buffered_messages]
        for item in buffered[:-1]:
            item["is_latest"] = False
            window_messages.append(item)
        if buffered:
            latest = dict(buffered[-1])
            latest["is_latest"] = True
            window_messages.append(latest)
        return window_messages

    async def _record_user_message(self, group_id: str, current_message: Dict[str, Any]) -> None:
        if self._conversation_store is None:
            return
        # group_id 是 _group_history_key(event) 的结果，格式为 "qq:group:{id}" 或 "group:{id}"
        # 统一转换为 "group:{id}" 格式，与 add_turn 的 _dialogue_key 保持一致
        parts = group_id.split(":")
        normalized = f"group:{parts[-1]}" if parts[-1].isdigit() else group_id
        persisted = dict(current_message)
        persisted.pop("is_latest", None)
        await self._conversation_store.add_group_message(normalized, persisted)

    async def _build_buffered_window_context(
        self,
        *,
        event: MessageEvent,
        window: BufferedWindow,
        trace_id: str = "",
    ) -> MessageContext:
        history_key = self._history_key(event)
        history_items = await self._get_recent_history(history_key)
        buffered_messages = await self._enrich_buffered_window_messages(list(window.messages or []), trace_id=trace_id)
        window_messages = self._compose_buffered_window_messages(history_items, buffered_messages)
        latest_message = next(
            (item for item in reversed(window_messages) if item.get("is_latest")),
            window_messages[-1] if window_messages else {},
        )
        execution_key = get_execution_key(event)
        conversation_key = self.session_manager.get_key(event)
        previous_message_time = 0.0
        previous_items = [item for item in window_messages if not bool(item.get("is_latest"))]
        if previous_items:
            previous_message_time = self._message_event_time(previous_items[-1])
        history_event_times = [item.get("event_time", 0.0) for item in window_messages]
        temporal_context = build_temporal_context(
            current_event_time=float(latest_message.get("event_time", 0.0) or time.time()),
            chat_mode="group",
            previous_message_time=previous_message_time,
            conversation_last_time=previous_message_time,
            history_event_times=history_event_times,
        )
        planning_signals = self._build_planning_signals(window_messages=window_messages, temporal_context=temporal_context)
        reply_context = self._merge_reply_context(
            self._build_window_reply_context(window_messages, assistant_self_id=event.self_id),
            self._build_planner_batch_context(
                group_id=history_key,
                mode="buffered_window",
                batch_size=max(1, len(buffered_messages)),
                is_latest=True,
            ),
        )
        return MessageContext(
            trace_id=trace_id,
            execution_key=execution_key,
            conversation_key=conversation_key,
            user_message=str(window.merged_user_message or latest_message.get("text_content") or latest_message.get("text") or "").strip(),
            current_sender_label=self._format_window_speaker(latest_message).replace("用户 ", "", 1),
            is_first_turn=False,
            current_event_time=float(latest_message.get("event_time", 0.0) or 0.0),
            previous_message_time=float(previous_message_time or 0.0),
            conversation_last_time=float(previous_message_time or 0.0),
            temporal_context=temporal_context,
            window_messages=window_messages,
            recent_history_text=self._build_recent_history_text(window_messages),
            vision_analysis={
                "per_image_descriptions": list(latest_message.get("per_image_descriptions") or []),
                "merged_description": str(latest_message.get("merged_description", "") or ""),
                "vision_success_count": int(latest_message.get("vision_success_count", 0) or 0),
                "vision_failure_count": int(latest_message.get("vision_failure_count", 0) or 0),
                "vision_source": str(latest_message.get("vision_source", "") or ""),
                "vision_error": str(latest_message.get("vision_error", "") or ""),
                "vision_available": bool(latest_message.get("vision_available", False)),
            },
            reply_context=reply_context,
            planning_signals=planning_signals,
        )

    async def _enrich_buffered_window_messages(
        self,
        buffered_messages: List[Dict[str, Any]],
        *,
        trace_id: str = "",
    ) -> List[Dict[str, Any]]:
        enriched: List[Dict[str, Any]] = []
        for item in buffered_messages:
            copied = dict(item)
            source_event = copied.get("_event")
            if isinstance(source_event, MessageEvent):
                text_content = str(copied.get("text_content") or copied.get("display_text") or copied.get("text") or "").strip()
                image_analysis = {}
                needs_image_enrichment = bool(copied.get("raw_has_image") or copied.get("has_image"))
                has_existing_analysis = bool(copied.get("merged_description")) or bool(copied.get("per_image_descriptions"))
                if needs_image_enrichment and not has_existing_analysis:
                    image_analysis = await self._analyze_images_for_event(source_event, text_content, trace_id=trace_id)
                planner_message = self.build_planner_message_info(
                    source_event,
                    text_content,
                    image_analysis=image_analysis,
                    include_image_context=self.image_analyzer is not None,
                )
                copied.update(planner_message)
                copied["message_id"] = str(copied.get("message_id") or getattr(source_event, "message_id", "") or "")
                copied["user_id"] = str(copied.get("user_id") or getattr(source_event, "user_id", "") or "")
                copied["speaker_role"] = str(copied.get("speaker_role") or "user")
                copied["speaker_name"] = str(copied.get("speaker_name") or self._sender_display_name(source_event))
            copied.pop("_event", None)
            enriched.append(copied)
        return enriched

    def _build_window_reply_context(self, window_messages: List[Dict[str, Any]], assistant_self_id: Any = "") -> Dict[str, Any]:
        return {
            "window_messages": window_messages,
            "buffered_message_count": len(window_messages),
            "assistant_self_id": str(assistant_self_id or ""),
        }

    def _build_planner_batch_context(
        self,
        *,
        group_id: str,
        mode: str,
        batch_size: int,
        is_latest: bool,
        merged_into_latest: bool = False,
    ) -> Dict[str, Any]:
        return {
            "group_id": group_id,
            "mode": mode,
            "batch_size": batch_size,
            "is_latest": is_latest,
            "merged_into_latest": merged_into_latest,
        }

    def _build_planning_signals(
        self,
        *,
        window_messages: List[Dict[str, Any]],
        temporal_context: Any,
    ) -> Dict[str, Any]:
        latest_message = next(
            (item for item in reversed(window_messages) if item.get("is_latest")),
            window_messages[-1] if window_messages else {},
        )
        previous_message = next(
            (item for item in reversed(window_messages) if not item.get("is_latest")),
            {},
        )
        signals = build_message_observations(
            str(latest_message.get("text_content") or latest_message.get("display_text") or latest_message.get("text") or ""),
            current_user_id=latest_message.get("user_id"),
            previous_speaker_role=previous_message.get("speaker_role"),
            previous_user_id=previous_message.get("user_id"),
            recent_gap_bucket=str(getattr(temporal_context, "recent_gap_bucket", "unknown") or "unknown"),
            recent_history_count=max(0, len(window_messages) - 1),
        )
        signals["assistant_turns_in_window"] = sum(
            1 for item in window_messages if str(item.get("speaker_role") or "").strip().lower() == "assistant"
        )
        return signals

    def _clone_plan(
        self,
        plan: MessageHandlingPlan,
        *,
        reason: Optional[str] = None,
        reply_context: Optional[Dict[str, Any]] = None,
    ) -> MessageHandlingPlan:
        return MessageHandlingPlan(
            action=plan.action,
            reason=reason or plan.reason,
            source=plan.source,
            raw_decision=plan.raw_decision,
            reply_context=reply_context if reply_context is not None else plan.reply_context,
            prompt_plan=plan.prompt_plan,
            reply_reference=plan.reply_reference,
        )

    def _merge_reply_context(
        self,
        base_context: Optional[Dict[str, Any]],
        planner_batch: Dict[str, Any],
    ) -> Dict[str, Any]:
        merged = dict(base_context or {})
        merged["planner_batch"] = planner_batch
        return merged

    def _build_rule_plan(
        self,
        action: MessagePlanAction,
        reason: str,
        source: str = "rule",
    ) -> MessageHandlingPlan:
        return MessageHandlingPlan(action=action.value, reason=reason, source=source)

    def _build_vision_fallback_plan(
        self,
        latest_message: Optional[Dict[str, Any]],
    ) -> Optional[MessageHandlingPlan]:
        if not latest_message:
            return None
        if not latest_message.get("raw_has_image"):
            return None
        if latest_message.get("text_present"):
            return None
        if latest_message.get("vision_available"):
            return None
        if int(latest_message.get("vision_failure_count", 0) or 0) <= 0:
            return None
        return self._build_rule_plan(
            MessagePlanAction.WAIT,
            "纯图片消息暂时没看清，先等待后续上下文",
            source="vision_fallback",
        )

    def _build_no_text_content_plan(
        self,
        latest_message: Optional[Dict[str, Any]],
    ) -> Optional[MessageHandlingPlan]:
        if self.image_analyzer is not None:
            return None
        if not latest_message:
            return None
        if not latest_message.get("raw_has_image"):
            return None
        if str(latest_message.get("text_content") or "").strip():
            return None
        return self._build_rule_plan(
            MessagePlanAction.IGNORE,
            "未启用视觉模型时，纯图片群消息不参与回复判断",
            source="no_text_content",
        )

    def _has_plannable_text(self, window_messages: List[Dict[str, Any]]) -> bool:
        latest_message = next(
            (item for item in reversed(window_messages) if item.get("is_latest")),
            window_messages[-1] if window_messages else None,
        )
        if not latest_message:
            return False
        text_content = str(latest_message.get("text_content") or "").strip()
        if text_content:
            return True
        planner_text = str(latest_message.get("text") or "").strip()
        return bool(planner_text and planner_text != "[空]")

    async def _plan_with_window_messages(
        self,
        *,
        event: MessageEvent,
        user_message: str,
        recent_messages: List[Dict[str, str]],
        window_messages: List[Dict[str, Any]],
        context: Optional[MessageContext] = None,
    ) -> MessageHandlingPlan:
        latest_message = next(
            (item for item in reversed(window_messages) if item.get("is_latest")),
            window_messages[-1] if window_messages else None,
        )
        no_text_plan = self._build_no_text_content_plan(latest_message)
        if no_text_plan is not None:
            return no_text_plan
        fallback_plan = self._build_vision_fallback_plan(latest_message)
        if fallback_plan is not None:
            return fallback_plan
        if not self._has_plannable_text(window_messages):
            return self._build_rule_plan(
                MessagePlanAction.IGNORE,
                "未启用视觉模型且当前消息没有可用文本，跳过本轮群聊规划",
                source="no_text_content",
            )
        planner_user_message = ""
        if latest_message:
            planner_user_message = str(
                latest_message.get("text_content") or latest_message.get("text") or ""
            ).strip()
        planner_user_message = planner_user_message or user_message
        return await self._execute_plan(
            event=event,
            user_message=planner_user_message,
            recent_messages=recent_messages,
            window_messages=window_messages,
            context=context,
        )

    async def _execute_plan(
        self,
        event: MessageEvent,
        user_message: str,
        recent_messages: List[Dict[str, str]],
        window_messages: Optional[List[Dict[str, Any]]] = None,
        context: Optional[MessageContext] = None,
    ) -> MessageHandlingPlan:
        group_id = str(event.raw_data.get("group_id", "unknown_group"))
        cooldown = self._get_plan_request_interval()
        conversation_lock = self.conversation_plan_locks.setdefault(group_id, asyncio.Lock())

        async with conversation_lock:
            async with self.conversation_plan_semaphore:
                plan = await self.planner.plan(
                    event=event,
                    user_message=user_message,
                    recent_messages=recent_messages,
                    window_messages=window_messages,
                    context=context,
                )
        if cooldown > 0:
            await asyncio.sleep(cooldown)
        return plan

    def _format_window_speaker(self, item: Dict[str, Any]) -> str:
        role = str(item.get("speaker_role") or "user").strip().lower()
        if role == "assistant":
            name = str(item.get("speaker_name") or config.get_assistant_name()).strip()
            return f"助手 {name or config.get_assistant_name()}"
        user_id = str(item.get("user_id") or item.get("speaker_name") or "unknown").strip() or "unknown"
        display_name = str(item.get("speaker_name") or "").strip()
        if display_name and display_name != user_id:
            return f"用户 {user_id}（{display_name}）"
        return f"用户 {user_id}"

    def _build_recent_history_text(self, window_messages: List[Dict[str, Any]]) -> str:
        builder = getattr(self.planner, "_build_recent_history_text", None)
        if callable(builder):
            return str(builder(window_messages) or "")
        history_items = [item for item in window_messages if not bool(item.get("is_latest"))]
        if not history_items:
            return "在当前这条群消息之前，群里刚刚聊过的内容暂时还没有。"
        lines = ["在当前这条群消息之前，群里刚刚聊了这些内容："]
        for item in history_items:
            lines.append(f"{self._format_window_speaker(item)}: {self._window_display_text(item)}")
        return "\n".join(lines)

    def _record_plan_metric(self, action: str, source: str = "") -> None:
        if self.runtime_metrics:
            self.runtime_metrics.record_planner_action(action, source=source)

    def _window_display_text(self, item: Dict[str, Any]) -> str:
        text = str(item.get("display_text") or item.get("text") or item.get("raw_text") or "").strip()
        raw_image_count = int(item.get("raw_image_count", item.get("image_count", 0)) or 0)
        has_image_indicator = bool(item.get("raw_has_image")) or raw_image_count > 0 or bool(item.get("image_description"))
        image_desc = str(item.get("image_description") or item.get("merged_description") or "").strip()
        if has_image_indicator and image_desc:
            if text and text != "[空]":
                return f"{text}[图片描述：{image_desc}]"
            return f"[图片描述：{image_desc}]"
        if text and text != "[空]":
            return text
        if has_image_indicator:
            return "[图片]" if raw_image_count <= 1 else f"[图片 x{raw_image_count}]"
        return text or "[空]"


__all__ = ["ConversationPlanCoordinator"]
