from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional

from src.core.config import GroupReplyConfig, config
from src.core.models import MessageEvent, MessageHandlingPlan, MessagePlanAction
from src.core.runtime_metrics import RuntimeMetrics
from src.handlers.conversation_session_manager import ConversationSessionManager
from src.handlers.group_reply_planner import GroupReplyPlanner

logger = logging.getLogger(__name__)

ImageAnalyzer = Callable[[MessageEvent, str], Awaitable[Dict[str, Any]]]


class GroupPlanCoordinator:
    """Coordinate group history, vision enrichment, and planner calls for group messages."""

    def __init__(
        self,
        planner: GroupReplyPlanner,
        session_manager: ConversationSessionManager,
        *,
        runtime_metrics: Optional[RuntimeMetrics] = None,
        group_reply_config: Optional[GroupReplyConfig] = None,
        image_analyzer: Optional[ImageAnalyzer] = None,
    ) -> None:
        self.planner = planner
        self.session_manager = session_manager
        self.runtime_metrics = runtime_metrics
        self.group_reply_config = group_reply_config or config.app.group_reply
        self.image_analyzer = image_analyzer

        self.group_plan_locks: Dict[str, asyncio.Lock] = {}
        max_parallel = max(1, int(self.group_reply_config.plan_request_max_parallel or 1))
        self.group_plan_max_parallel = max_parallel
        self.group_plan_semaphore = asyncio.Semaphore(max_parallel)

        self.group_message_history: Dict[str, Deque[Dict[str, Any]]] = {}
        self._history_limit_floor = 50

    async def close(self) -> None:
        self.group_message_history.clear()

    def build_planner_message_info(
        self,
        event: MessageEvent,
        clean_text: str,
        image_analysis: Optional[Dict[str, Any]] = None,
        include_image_context: bool = True,
    ) -> Dict[str, Any]:
        clean_text = clean_text.strip()
        raw_text = event.extract_text().strip()
        image_count = len(event.get_image_segments())
        has_image = image_count > 0
        text_present = bool(clean_text)
        effective_has_image = has_image and include_image_context

        if effective_has_image and text_present:
            message_shape = "text_with_image"
        elif effective_has_image:
            message_shape = "image_only"
        else:
            message_shape = "text_only"

        planner_text = clean_text
        if effective_has_image:
            image_placeholder = "[图片]" if image_count == 1 else f"[图片 x{image_count}]"
            planner_text = f"{clean_text} {image_placeholder}".strip() if clean_text else image_placeholder

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

        if merged_description:
            planner_text = f"{planner_text}\n图片摘要: {merged_description}".strip()
        elif per_image_descriptions:
            planner_text = f"{planner_text}\n图片描述: {'；'.join(per_image_descriptions)}".strip()
        elif effective_has_image and vision_failure_count > 0:
            planner_text = f"{planner_text}\n图片理解状态: 识图失败".strip()

        if not planner_text:
            planner_text = "[空]"

        return {
            "text": planner_text,
            "text_content": clean_text,
            "raw_text": raw_text,
            "has_image": effective_has_image,
            "raw_has_image": has_image,
            "image_context_enabled": effective_has_image,
            "image_count": image_count if effective_has_image else 0,
            "raw_image_count": image_count,
            "text_present": text_present,
            "is_image_only": message_shape == "image_only",
            "message_shape": message_shape,
            "image_file_ids": event.get_image_file_ids() if effective_has_image else [],
            "per_image_descriptions": per_image_descriptions,
            "merged_description": merged_description,
            "vision_available": vision_available,
            "vision_failure_count": vision_failure_count,
            "vision_success_count": vision_success_count,
            "vision_source": vision_source,
            "vision_error": vision_error,
        }

    def format_group_window_context(self, reply_context: Optional[Dict[str, Any]]) -> str:
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
            text = str(item.get("text") or item.get("raw_text") or "").strip() or "[空]"
            image_note = f" [图片 {item.get('image_count', 1)} 张]" if item.get("has_image") else ""
            latest_note = " [当前消息]" if item.get("is_latest") else ""
            lines.append(f"{index}. {speaker}: {text}{image_note}{latest_note}")

            merged_description = str(item.get("merged_description") or "").strip()
            if merged_description:
                lines.append(f"   图片摘要: {merged_description}")
            for image_index, description in enumerate(item.get("per_image_descriptions") or [], 1):
                lines.append(f"   第{image_index}张: {description}")
            if item.get("has_image") and item.get("vision_failure_count", 0) and not item.get("vision_available"):
                lines.append("   图片理解: 失败")
        lines.append("")
        return "\n".join(lines)

    async def plan_group_message(self, event: MessageEvent, user_message: str) -> MessageHandlingPlan:
        group_id = str(event.group_id or "unknown_group")
        history_items = self._get_recent_group_history(group_id)
        current_message = await self._build_current_message(event=event, user_message=user_message)
        window_messages = self._compose_window_messages(history_items, current_message)

        try:
            plan = await self._plan_with_window_messages(
                event=event,
                user_message=user_message,
                recent_messages=[],
                window_messages=window_messages,
            )
        finally:
            self._record_group_user_message(group_id, current_message)

        reply_context = self._merge_reply_context(
            self._build_group_window_reply_context(window_messages),
            self._build_planner_batch_context(
                group_id=group_id,
                mode="single",
                batch_size=1,
                is_latest=True,
            ),
        )
        reply_mode = "proactive" if plan.should_reply else ""
        if reply_mode:
            reply_context["reply_mode"] = reply_mode
        plan = self._clone_plan(plan, reply_context=reply_context)
        self._record_plan_metric(plan.action)
        return plan

    async def record_assistant_reply(self, group_id: Optional[int], message: str) -> None:
        text = str(message or "").strip()
        if group_id is None or not text:
            return
        group_key = str(group_id)
        item = {
            "message_id": 0,
            "user_id": "",
            "speaker_role": "assistant",
            "speaker_name": config.get_assistant_name(),
            "text": text,
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
        self._append_group_history(group_key, item)

    def _get_plan_request_interval(self) -> float:
        return max(0.0, float(self.group_reply_config.plan_request_interval or 0))

    def _get_plan_context_message_count(self) -> int:
        return max(0, int(self.group_reply_config.plan_context_message_count or 0))

    def _max_history_buffer_size(self) -> int:
        return max(self._history_limit_floor, self._get_plan_context_message_count() + 10)

    def _history_deque(self, group_id: str) -> Deque[Dict[str, Any]]:
        history = self.group_message_history.get(group_id)
        maxlen = self._max_history_buffer_size()
        if history is None or history.maxlen != maxlen:
            preserved = list(history or [])
            history = deque(preserved[-maxlen:], maxlen=maxlen)
            self.group_message_history[group_id] = history
        return history

    def _append_group_history(self, group_id: str, item: Dict[str, Any]) -> None:
        self._history_deque(group_id).append(dict(item))

    def _get_recent_group_history(self, group_id: str) -> List[Dict[str, Any]]:
        count = self._get_plan_context_message_count()
        if count <= 0:
            return []
        history = list(self._history_deque(group_id))
        return [dict(item) for item in history[-count:]]

    async def _build_current_message(self, *, event: MessageEvent, user_message: str) -> Dict[str, Any]:
        image_analysis = await self._analyze_images_for_event(event, user_message)
        planner_message = self.build_planner_message_info(
            event,
            user_message,
            image_analysis=image_analysis,
            include_image_context=self.image_analyzer is not None,
        )
        return {
            "message_id": int(event.message_id or 0),
            "user_id": str(event.user_id),
            "speaker_role": "user",
            "speaker_name": str(event.user_id),
            **planner_message,
        }

    async def _analyze_images_for_event(self, event: MessageEvent, user_message: str) -> Dict[str, Any]:
        if not event.has_image() or self.image_analyzer is None:
            return {}
        try:
            return await self.image_analyzer(event, user_message)
        except Exception as exc:
            logger.warning(
                "[planner] image analysis failed before planning: message_id=%s error=%s",
                event.message_id,
                exc,
                exc_info=True,
            )
            return {
                "per_image_descriptions": [],
                "merged_description": "",
                "vision_success_count": 0,
                "vision_failure_count": len(event.get_image_segments()),
                "vision_source": "vision_error",
                "vision_error": str(exc),
                "vision_available": False,
            }

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

    def _record_group_user_message(self, group_id: str, current_message: Dict[str, Any]) -> None:
        persisted = dict(current_message)
        persisted.pop("is_latest", None)
        self._append_group_history(group_id, persisted)

    def _build_group_window_reply_context(self, window_messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "window_messages": window_messages,
            "buffered_message_count": len(window_messages),
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
    ) -> MessageHandlingPlan:
        latest_message = next(
            (item for item in reversed(window_messages) if item.get("is_latest")),
            window_messages[-1] if window_messages else None,
        )
        if not self._has_plannable_text(window_messages):
            no_text_plan = self._build_no_text_content_plan(latest_message)
            if no_text_plan is not None:
                return no_text_plan
            fallback_plan = self._build_vision_fallback_plan(latest_message)
            if fallback_plan is not None:
                return fallback_plan
            return self._build_rule_plan(
                MessagePlanAction.IGNORE,
                "未启用视觉模型且当前消息没有可用文本，跳过本轮群聊规划",
                source="no_text_content",
            )
        fallback_plan = self._build_vision_fallback_plan(latest_message)
        if fallback_plan is not None:
            return fallback_plan
        planner_user_message = ""
        if latest_message:
            planner_user_message = str(
                latest_message.get("text_content") or latest_message.get("text") or ""
            ).strip()
        planner_user_message = planner_user_message or user_message
        return await self._execute_group_plan(
            event=event,
            user_message=planner_user_message,
            recent_messages=recent_messages,
            window_messages=window_messages,
        )

    async def _execute_group_plan(
        self,
        event: MessageEvent,
        user_message: str,
        recent_messages: List[Dict[str, str]],
        window_messages: Optional[List[Dict[str, Any]]] = None,
    ) -> MessageHandlingPlan:
        group_id = str(event.group_id or "unknown_group")
        cooldown = self._get_plan_request_interval()
        group_lock = self.group_plan_locks.setdefault(group_id, asyncio.Lock())

        async with group_lock:
            async with self.group_plan_semaphore:
                plan = await self.planner.plan(
                    event=event,
                    user_message=user_message,
                    recent_messages=recent_messages,
                    window_messages=window_messages,
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
        return f"用户 {user_id}"

    def _record_plan_metric(self, action: str, source: str = "") -> None:
        if self.runtime_metrics:
            self.runtime_metrics.record_planner_action(action, source=source)
