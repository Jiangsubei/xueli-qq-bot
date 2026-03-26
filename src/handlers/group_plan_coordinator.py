from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.core.config import GroupReplyConfig, config
from src.core.models import MessageEvent, MessageHandlingPlan, MessagePlanAction
from src.core.runtime_metrics import RuntimeMetrics
from src.handlers.conversation_session_manager import ConversationSessionManager
from src.handlers.group_reply_planner import GroupReplyPlanner

logger = logging.getLogger(__name__)

ImageAnalyzer = Callable[[MessageEvent, str], Awaitable[Dict[str, Any]]]


@dataclass
class PendingGroupPlanRequest:
    event: MessageEvent
    user_message: str
    recent_messages: List[Dict[str, str]]
    future: asyncio.Future
    received_at: float


class GroupPlanCoordinator:
    """Coordinate burst buffering, vision enrichment, and planner calls for group messages."""

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
        self.group_plan_buffer_locks: Dict[str, asyncio.Lock] = {}
        self.group_plan_buffers: Dict[str, List[PendingGroupPlanRequest]] = {}
        self.group_plan_flush_tasks: Dict[str, asyncio.Task] = {}
        max_parallel = max(1, int(self.group_reply_config.plan_request_max_parallel or 1))
        self.group_plan_max_parallel = max_parallel
        self.group_plan_semaphore = asyncio.Semaphore(max_parallel)

    async def close(self) -> None:
        tasks = list(self.group_plan_flush_tasks.values())
        self.group_plan_flush_tasks.clear()
        self.group_plan_buffers.clear()
        if tasks:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

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
            "=== 当前群聊短时间窗口内的连续消息（按时间顺序）===",
            "如果你决定回复，请结合整段对话自然接话，不要只盯着最后一条消息。",
        ]
        for index, item in enumerate(window_messages, 1):
            user_id = item.get("user_id", "unknown")
            text = str(item.get("text") or item.get("raw_text") or "").strip() or "[空]"
            image_note = f" [图片 {item.get('image_count', 1)} 张]" if item.get("has_image") else ""
            latest_note = " [最新]" if item.get("is_latest") else ""
            lines.append(f"{index}. 用户 {user_id}: {text}{image_note}{latest_note}")

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
        key = self.session_manager.get_key(event)
        conversation = self.session_manager.get_optional(key)
        recent_messages = conversation.get_messages(max_length=6) if conversation else []
        group_id = str(event.group_id or "unknown_group")
        burst_window = self._get_group_burst_window_seconds()
        burst_merge_enabled = self._is_burst_merge_enabled()

        if not burst_merge_enabled or burst_window <= 0:
            window_messages = await self._build_group_window_messages(
                [
                    PendingGroupPlanRequest(
                        event=event,
                        user_message=user_message,
                        recent_messages=recent_messages,
                        future=asyncio.get_running_loop().create_future(),
                        received_at=time.monotonic(),
                    )
                ]
            )
            plan = await self._plan_with_window_messages(
                event=event,
                user_message=user_message,
                recent_messages=recent_messages,
                window_messages=window_messages,
            )
            plan = self._clone_plan(
                plan,
                reply_context=self._merge_reply_context(
                    self._build_group_window_reply_context(window_messages),
                    self._build_planner_batch_context(
                        group_id=group_id,
                        mode="disabled" if not burst_merge_enabled else "window_off",
                        batch_size=1,
                        is_latest=True,
                    ),
                ),
            )
            self._record_plan_metric(plan.action)
            return plan

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        pending_request = PendingGroupPlanRequest(
            event=event,
            user_message=user_message,
            recent_messages=recent_messages,
            future=future,
            received_at=time.monotonic(),
        )

        buffer_lock = self.group_plan_buffer_locks.setdefault(group_id, asyncio.Lock())
        async with buffer_lock:
            batch = self.group_plan_buffers.setdefault(group_id, [])
            batch.append(pending_request)
            batch_size = len(batch)
            max_messages = self._get_group_burst_max_messages()
            min_messages = self._get_group_burst_min_messages()
            flush_delay = self._get_group_plan_flush_delay(batch, burst_window)

            logger.info(
                "[planner_buffer] queued group=%s size=%s min=%s max=%s flush_in=%.2fs message_id=%s user=%s",
                group_id,
                batch_size,
                min_messages,
                max_messages,
                flush_delay,
                event.message_id,
                event.user_id,
            )

            if batch_size >= max_messages:
                self._schedule_group_plan_flush(group_id, 0)
            else:
                self._schedule_group_plan_flush(group_id, flush_delay)

        return await future

    def _is_burst_merge_enabled(self) -> bool:
        value = self._get_legacy_group_reply_value(
            "GROUP_REPLY_BURST_MERGE_ENABLED",
            self.group_reply_config.burst_merge_enabled,
        )
        return bool(value)

    def _get_group_burst_window_seconds(self) -> float:
        value = self._get_legacy_group_reply_value(
            "GROUP_REPLY_BURST_WINDOW_SECONDS",
            self.group_reply_config.burst_window_seconds,
        )
        return max(0.0, float(value or 0))

    def _get_group_burst_min_messages(self) -> int:
        value = self._get_legacy_group_reply_value(
            "GROUP_REPLY_BURST_MIN_MESSAGES",
            self.group_reply_config.burst_min_messages,
        )
        return max(1, int(value or 1))

    def _get_group_burst_max_messages(self) -> int:
        value = self._get_legacy_group_reply_value(
            "GROUP_REPLY_BURST_MAX_MESSAGES",
            self.group_reply_config.burst_max_messages,
        )
        return max(1, int(value or 8))

    def _get_plan_request_interval(self) -> float:
        value = self._get_legacy_group_reply_value(
            "GROUP_REPLY_PLAN_REQUEST_INTERVAL",
            self.group_reply_config.plan_request_interval,
        )
        return max(0.0, float(value or 0))

    def _get_legacy_group_reply_value(self, attr_name: str, default: Any) -> Any:
        legacy_value = getattr(config, attr_name, None)
        return default if legacy_value is None else legacy_value

    def _sort_group_plan_batch(self, batch: List[PendingGroupPlanRequest]) -> List[PendingGroupPlanRequest]:
        return sorted(
            batch,
            key=lambda item: (float(item.received_at or 0), int(getattr(item.event, "message_id", 0) or 0)),
        )

    def _get_group_plan_flush_delay(
        self,
        batch: List[PendingGroupPlanRequest],
        burst_window: float,
    ) -> float:
        if burst_window <= 0 or not batch:
            return 0.0
        latest_message_time = max(float(item.received_at or 0) for item in batch)
        elapsed = max(0.0, time.monotonic() - latest_message_time)
        return max(0.0, burst_window - elapsed)

    async def _analyze_images_for_request(self, item: PendingGroupPlanRequest) -> Dict[str, Any]:
        if not item.event.has_image() or self.image_analyzer is None:
            return {}
        try:
            return await self.image_analyzer(item.event, item.user_message)
        except Exception as exc:
            logger.warning(
                "[planner] image analysis failed before planning: message_id=%s error=%s",
                item.event.message_id,
                exc,
                exc_info=True,
            )
            return {
                "per_image_descriptions": [],
                "merged_description": "",
                "vision_success_count": 0,
                "vision_failure_count": len(item.event.get_image_segments()),
                "vision_source": "vision_error",
                "vision_error": str(exc),
                "vision_available": False,
            }

    async def _build_group_window_messages(self, batch: List[PendingGroupPlanRequest]) -> List[Dict[str, Any]]:
        window_messages: List[Dict[str, Any]] = []
        sorted_batch = self._sort_group_plan_batch(batch)
        selected_batch = sorted_batch[-self._get_group_burst_max_messages() :]
        image_analyses = await asyncio.gather(
            *(self._analyze_images_for_request(item) for item in selected_batch),
            return_exceptions=False,
        )
        latest_index = len(selected_batch) - 1
        for index, item in enumerate(selected_batch):
            planner_message = self.build_planner_message_info(
                item.event,
                item.user_message,
                image_analysis=image_analyses[index],
                include_image_context=self.image_analyzer is not None,
            )
            window_messages.append(
                {
                    "message_id": item.event.message_id,
                    "user_id": str(item.event.user_id),
                    **planner_message,
                    "is_latest": index == latest_index,
                }
            )
        return window_messages

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
        for item in window_messages:
            text_content = str(item.get("text_content") or "").strip()
            if text_content:
                return True
            planner_text = str(item.get("text") or "").strip()
            if planner_text and planner_text != "[空]":
                return True
        return False

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
                "未启用视觉模型且窗口内没有可用文本，跳过本轮群聊规划",
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
        if not planner_user_message or planner_user_message == "[空]":
            for item in reversed(window_messages):
                candidate = str(item.get("text_content") or item.get("text") or "").strip()
                if candidate and candidate != "[空]":
                    planner_user_message = candidate
                    break
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

    def _schedule_group_plan_flush(self, group_id: str, delay: float) -> None:
        task = self.group_plan_flush_tasks.get(group_id)
        if task and not task.done():
            task.cancel()
        self.group_plan_flush_tasks[group_id] = asyncio.create_task(
            self._flush_group_plan_after_delay(group_id, max(0.0, delay))
        )

    async def _flush_group_plan_after_delay(self, group_id: str, delay: float) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await self._flush_group_plan(group_id)
        except asyncio.CancelledError:
            return

    async def _flush_group_plan(self, group_id: str) -> None:
        buffer_lock = self.group_plan_buffer_locks.setdefault(group_id, asyncio.Lock())
        async with buffer_lock:
            batch = self.group_plan_buffers.get(group_id, [])
            if not batch:
                return
            self.group_plan_buffers[group_id] = []
            self.group_plan_flush_tasks.pop(group_id, None)

        ordered_batch = self._sort_group_plan_batch(batch)
        batch_size = len(ordered_batch)
        min_messages = self._get_group_burst_min_messages()
        if batch_size < min_messages:
            logger.info(
                "[planner_buffer] flush group=%s size=%s mode=individual threshold=%s",
                group_id,
                batch_size,
                min_messages,
            )
            await self._flush_group_plan_individually(group_id, ordered_batch)
            return

        latest_request = ordered_batch[-1]
        window_messages = await self._build_group_window_messages(ordered_batch)

        logger.info(
            "[planner_buffer] flush group=%s size=%s mode=burst latest_message_id=%s latest_user=%s",
            group_id,
            batch_size,
            latest_request.event.message_id,
            latest_request.event.user_id,
        )

        try:
            plan = await self._plan_with_window_messages(
                event=latest_request.event,
                user_message=latest_request.user_message,
                recent_messages=latest_request.recent_messages,
                window_messages=window_messages,
            )
        except Exception as exc:
            logger.exception("[planner] batch planning failed: group=%s error=%s", group_id, exc)
            plan = self._build_rule_plan(
                MessagePlanAction.IGNORE,
                "群聊规划异常，回退为忽略",
                source="fallback",
            )

        latest_reply_context = self._merge_reply_context(
            self._build_group_window_reply_context(window_messages),
            self._build_planner_batch_context(
                group_id=group_id,
                mode="burst",
                batch_size=batch_size,
                is_latest=True,
            ),
        )
        if plan.should_reply:
            plan = self._clone_plan(plan, reply_context=latest_reply_context)
        else:
            plan = self._clone_plan(
                plan,
                reply_context=self._merge_reply_context(
                    plan.reply_context,
                    self._build_planner_batch_context(
                        group_id=group_id,
                        mode="burst",
                        batch_size=batch_size,
                        is_latest=True,
                    ),
                ),
            )

        for item in ordered_batch:
            if item.future.done():
                continue
            if plan.should_reply and item is latest_request:
                self._record_plan_metric(plan.action)
                item.future.set_result(plan)
                continue
            if plan.should_reply:
                self._record_plan_metric(MessagePlanAction.WAIT.value, source="burst_merge")
                item.future.set_result(
                    self._clone_plan(
                        self._build_rule_plan(
                            MessagePlanAction.WAIT,
                            "当前消息已并入同一热聊窗口，由较新的群消息统一触发回复",
                            source="burst_merge",
                        ),
                        reply_context=self._merge_reply_context(
                            None,
                            self._build_planner_batch_context(
                                group_id=group_id,
                                mode="burst",
                                batch_size=batch_size,
                                is_latest=False,
                                merged_into_latest=True,
                            ),
                        ),
                    )
                )
                continue
            self._record_plan_metric(plan.action)
            item.future.set_result(
                self._clone_plan(
                    plan,
                    reply_context=self._merge_reply_context(
                        plan.reply_context,
                        self._build_planner_batch_context(
                            group_id=group_id,
                            mode="burst",
                            batch_size=batch_size,
                            is_latest=item is latest_request,
                        ),
                    ),
                )
            )

    async def _flush_group_plan_individually(
        self,
        group_id: str,
        batch: List[PendingGroupPlanRequest],
    ) -> None:
        batch_size = len(batch)
        for item in batch:
            if item.future.done():
                continue

            try:
                window_messages = await self._build_group_window_messages([item])
                plan = await self._plan_with_window_messages(
                    event=item.event,
                    user_message=item.user_message,
                    recent_messages=item.recent_messages,
                    window_messages=window_messages,
                )
            except Exception as exc:
                logger.exception(
                    "[planner] individual planning failed: group=%s message_id=%s error=%s",
                    group_id,
                    item.event.message_id,
                    exc,
                )
                plan = self._build_rule_plan(
                    MessagePlanAction.IGNORE,
                    "群聊规划异常，回退为忽略",
                    source="fallback",
                )
                window_messages = []

            reply_context = self._merge_reply_context(
                self._build_group_window_reply_context(window_messages) if window_messages else plan.reply_context,
                self._build_planner_batch_context(
                    group_id=group_id,
                    mode="individual",
                    batch_size=batch_size,
                    is_latest=True,
                ),
            )
            plan = self._clone_plan(plan, reply_context=reply_context)
            self._record_plan_metric(plan.action)
            item.future.set_result(plan)

    def _record_plan_metric(self, action: str, source: str = "") -> None:
        if self.runtime_metrics:
            self.runtime_metrics.record_planner_action(action, source=source)
