from __future__ import annotations

import re
import time
from typing import Any, Dict, Iterable, List, Optional

from src.core.config import PlanningWindowConfig
from src.core.message_trace import get_execution_key
from src.core.models import MessageEvent
from src.handlers.conversation_engagement import build_message_observations
from src.handlers.conversation_window_models import BufferedWindow, WindowDispatchResult
from src.handlers.conversation_window_scheduler import ConversationWindowScheduler


class PlanningWindowService:
    """Thin facade over the per-conversation rolling window scheduler."""

    def __init__(self, host: Any, config: PlanningWindowConfig) -> None:
        self.host = host
        self.config = config
        self.scheduler = ConversationWindowScheduler()

    async def submit_private_event(
        self,
        *,
        event: MessageEvent,
        trace_id: str = "",
    ) -> WindowDispatchResult:
        del trace_id
        conversation_key = self.host._get_conversation_key(event)
        result = await self.scheduler.submit_event(
            conversation_key=conversation_key,
            chat_mode="private",
            event=event,
            window_seconds=float(getattr(self.host, "private_batch_window_seconds", self.config.private_window_seconds) or 0.0),
            queue_expire_seconds=float(getattr(self.config, "queue_expire_seconds", 60.0) or 0.0),
            message_builder=self._build_private_window_message,
            merge_builder=self._merge_window_text,
        )
        if result.status != "dispatch_window" or result.window is None:
            if result.status == "accepted_only" and not str(result.reason or "").strip():
                result.reason = "buffer_opened"
            return result
        latest_event = result.window.latest_event if isinstance(result.window.latest_event, MessageEvent) else event
        conversation = self.host._get_conversation(conversation_key)
        temporal_context = self.host._build_temporal_context(
            event=latest_event,
            conversation=conversation,
            reply_context={"window_messages": list(result.window.messages or [])},
        )
        result.window.planning_signals = self._build_private_planning_signals(
            event=latest_event,
            user_message=result.window.merged_user_message,
            conversation=conversation,
            temporal_context=temporal_context,
            pending_items=list(result.window.messages or []),
        )
        return result

    async def submit_group_event(
        self,
        *,
        event: MessageEvent,
        trace_id: str = "",
    ) -> WindowDispatchResult:
        del trace_id
        user_message = self.host.extract_user_message(event)
        if self._should_bypass_group_window(event, user_message):
            return WindowDispatchResult(status="bypassed", reason="bypassed")
        conversation_key = self.host.conversation_plan_coordinator._group_history_key(event)
        result = await self.scheduler.submit_event(
            conversation_key=conversation_key,
            chat_mode="group",
            event=event,
            window_seconds=float(
                getattr(self.host, "group_proactive_window_seconds", self.config.group_proactive_window_seconds) or 0.0
            ),
            queue_expire_seconds=float(getattr(self.config, "queue_expire_seconds", 60.0) or 0.0),
            message_builder=self._build_group_window_message,
            merge_builder=self._merge_window_text,
        )
        if result.status == "dispatch_window" and result.window is not None:
            result.window.planning_signals = {"window_batch_size": max(1, len(result.window.messages or []))}
        if result.status == "accepted_only" and not str(result.reason or "").strip():
            result.reason = "buffer_opened"
        return result

    async def mark_window_complete(self, conversation_key: str, seq: int) -> WindowDispatchResult:
        return await self.scheduler.mark_window_complete(conversation_key, seq)

    async def cleanup(self, *, active_keys: Optional[Iterable[str]] = None) -> None:
        max_window = max(
            30.0,
            float(getattr(self.host, "private_batch_window_seconds", self.config.private_window_seconds) or 0.0) * 8,
            float(getattr(self.host, "group_proactive_window_seconds", self.config.group_proactive_window_seconds) or 0.0) * 12,
            float(getattr(self.config, "queue_expire_seconds", 60.0) or 0.0) * 2,
        )
        await self.scheduler.cleanup(active_keys=active_keys, idle_seconds=max_window)

    async def close(self) -> None:
        await self.scheduler.close()

    def _should_bypass_group_window(self, event: MessageEvent, user_message: str) -> bool:
        normalized = str(user_message or "").strip()
        if self.host._is_direct_mention(event):
            return True
        if normalized.startswith("/"):
            return True
        if str(self.host._get_reply_to_message_id(event) or "").strip():
            return True
        return False

    def _build_private_window_message(self, event: MessageEvent) -> Dict[str, Any]:
        user_message = self.host.extract_user_message(event)
        return {
            "message_id": str(getattr(event, "message_id", "") or ""),
            "speaker_role": "user",
            "speaker_name": self.host._get_sender_display_name(event),
            "user_id": str(event.user_id),
            "event_time": float(getattr(event, "time", 0.0) or time.time()),
            "text_content": str(user_message or "").strip(),
            "text": str(user_message or "").strip(),
            "display_text": str(user_message or "").strip(),
            "has_image": bool(self.host._has_image_input(event)),
            "raw_has_image": bool(self.host._has_image_input(event)),
            "image_count": self.host._get_image_count(event),
            "raw_image_count": self.host._get_image_count(event),
            "trace_id": get_execution_key(event),
            "_event": event,
        }

    def _build_group_window_message(self, event: MessageEvent) -> Dict[str, Any]:
        user_message = self.host.extract_user_message(event)
        return {
            "message_id": str(getattr(event, "message_id", "") or ""),
            "speaker_role": "user",
            "speaker_name": self.host._get_sender_display_name(event),
            "user_id": str(event.user_id),
            "event_time": float(getattr(event, "time", 0.0) or time.time()),
            "text_content": str(user_message or "").strip(),
            "text": str(user_message or "").strip() or "[空]",
            "display_text": str(user_message or "").strip() or "[空]",
            "raw_text": str(self.host._get_event_text(event) or "").strip(),
            "has_image": bool(self.host._has_image_input(event)),
            "raw_has_image": bool(self.host._has_image_input(event)),
            "image_count": self.host._get_image_count(event),
            "raw_image_count": self.host._get_image_count(event),
            "text_present": bool(str(user_message or "").strip()),
            "is_image_only": bool(self.host._has_image_input(event) and not str(user_message or "").strip()),
            "message_shape": self._describe_message_shape(event, user_message),
            "image_file_ids": list(self.host._get_image_file_ids(event) or []),
            "per_image_descriptions": [],
            "merged_description": "",
            "vision_available": False,
            "vision_failure_count": 0,
            "vision_success_count": 0,
            "vision_source": "",
            "vision_error": "",
            "trace_id": get_execution_key(event),
            "_event": event,
        }

    def _describe_message_shape(self, event: MessageEvent, user_message: str) -> str:
        has_image = bool(self.host._has_image_input(event))
        if has_image and str(user_message or "").strip():
            return "text_with_image"
        if has_image:
            return "image_only"
        return "text_only"

    def _merge_window_text(self, items: List[Dict[str, Any]]) -> str:
        lines: List[str] = []
        for item in items:
            text = str(item.get("text_content") or item.get("text") or "").strip()
            if text and text != "[空]":
                lines.append(text)
        if not lines:
            return ""
        merged: List[str] = []
        for text in lines:
            if merged and merged[-1] == text:
                continue
            merged.append(text)
        return "\n".join(merged)

    def _build_private_planning_signals(
        self,
        *,
        event: MessageEvent,
        user_message: str,
        conversation: Any,
        temporal_context: Any,
        pending_items: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        text = str(user_message or "").strip()
        normalized = re.sub(r"\s+", "", text).lower()
        pending = list(pending_items or [])
        previous_role = ""
        if conversation.messages:
            previous_role = str(conversation.messages[-1].get("role") or "").strip().lower()
            if previous_role not in {"user", "assistant"}:
                previous_role = ""
        signals = {
            "window_batch_size": max(1, len(pending)),
            "merged_from_multiple_inputs": len(pending) > 1,
            "has_image_without_text": bool(self.host._has_image_input(event) and not normalized),
            "recent_gap_bucket": str(getattr(temporal_context, "recent_gap_bucket", "unknown") or "unknown"),
            "conversation_turn_count": len(conversation.messages),
        }
        signals.update(
            build_message_observations(
                text,
                current_user_id=event.user_id,
                previous_speaker_role=previous_role,
                previous_user_id=event.user_id,
                recent_gap_bucket=str(getattr(temporal_context, "recent_gap_bucket", "unknown") or "unknown"),
                recent_history_count=len(conversation.messages),
            )
        )
        return signals
