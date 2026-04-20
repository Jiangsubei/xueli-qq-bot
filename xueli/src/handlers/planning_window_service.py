from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from src.core.config import PlanningWindowConfig
from src.core.message_trace import get_execution_key
from src.core.models import MessageEvent, PlanningWindowResult
from src.handlers.conversation_engagement import build_companionship_signals
from src.handlers.message_context import MessageContext


class PlanningWindowService:
    """Collect and stabilize nearby inputs before they reach the planner."""

    def __init__(self, host: Any, config: PlanningWindowConfig) -> None:
        self.host = host
        self.config = config
        self._lock = asyncio.Lock()
        self._versions: Dict[str, int] = defaultdict(int)
        self._pending_inputs: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    async def process_private_message(
        self,
        *,
        event: MessageEvent,
        trace_id: str = "",
    ) -> PlanningWindowResult:
        conversation_key = self.host._get_conversation_key(event)
        conversation = self.host._get_conversation(conversation_key)
        user_message = self.host.extract_user_message(event)
        temporal_context = self.host._build_temporal_context(
            event=event,
            conversation=conversation,
            reply_context=None,
        )
        current_version = await self._push_pending_input(conversation_key=conversation_key, event=event, user_message=user_message)
        hold_reason = self._private_hold_reason(event, user_message)
        if hold_reason:
            return PlanningWindowResult(
                merged_user_message=user_message,
                planning_signals=self._build_private_planning_signals(
                    event=event,
                    user_message=user_message,
                    conversation=conversation,
                    temporal_context=temporal_context,
                ),
                window_reason=hold_reason,
                bypassed=False,
            )
        await asyncio.sleep(max(0.0, float(getattr(self.host, "private_batch_window_seconds", self.config.private_window_seconds) or 0.0)))
        if await self._has_newer_input(conversation_key, current_version):
            return PlanningWindowResult(
                merged_user_message=user_message,
                planning_signals=self._build_private_planning_signals(
                    event=event,
                    user_message=user_message,
                    conversation=conversation,
                    temporal_context=temporal_context,
                ),
                window_reason="用户仍在短时间内连续补充，先等待本轮私聊输入稳定",
                bypassed=False,
            )
        pending_items = await self._consume_pending_input(conversation_key)
        merged = self._merge_pending_text(pending_items, user_message)
        planning_signals = self._build_private_planning_signals(
            event=event,
            user_message=merged,
            conversation=conversation,
            temporal_context=temporal_context,
            pending_items=pending_items,
        )
        window_messages = [
            {
                "message_id": str(item.get("message_id") or ""),
                "speaker_role": "user",
                "speaker_name": self.host._get_sender_display_name(event),
                "user_id": str(event.user_id),
                "event_time": float(item.get("event_time", 0.0) or 0.0),
                "text_content": str(item.get("text") or ""),
                "text": str(item.get("text") or ""),
                "display_text": str(item.get("text") or ""),
                "has_image": bool(item.get("has_image")),
                "raw_has_image": bool(item.get("has_image")),
                "image_count": 1 if item.get("has_image") else 0,
                "raw_image_count": 1 if item.get("has_image") else 0,
                "is_latest": index == len(pending_items) - 1,
            }
            for index, item in enumerate(pending_items)
        ]
        return PlanningWindowResult(
            merged_user_message=merged,
            window_messages=window_messages,
            planning_signals=planning_signals,
            window_reason="private_window_merged" if len(pending_items) > 1 else "private_window_stable",
            bypassed=False,
        )

    async def process_group_message(
        self,
        *,
        event: MessageEvent,
        trace_id: str = "",
    ) -> PlanningWindowResult:
        del trace_id
        user_message = self.host.extract_user_message(event)
        if self._should_bypass_group_window(event, user_message):
            return PlanningWindowResult(
                merged_user_message=user_message,
                planning_signals={},
                window_reason="group_window_bypassed",
                bypassed=True,
            )
        group_key = self.host.conversation_plan_coordinator._group_history_key(event)
        version = await self._push_pending_input(conversation_key=group_key, event=event, user_message=user_message)
        await asyncio.sleep(max(0.0, float(getattr(self.host, "group_proactive_window_seconds", self.config.group_proactive_window_seconds) or 0.0)))
        if await self._has_newer_input(group_key, version):
            return PlanningWindowResult(
                merged_user_message=user_message,
                planning_signals={},
                window_reason="group_window_wait_for_more",
                bypassed=False,
            )
        pending_items = await self._consume_pending_input(group_key)
        merged = self._merge_pending_text(pending_items, user_message)
        return PlanningWindowResult(
            merged_user_message=merged,
            window_messages=[
                {
                    "message_id": str(item.get("message_id") or ""),
                    "speaker_role": "user",
                    "speaker_name": self.host._get_sender_display_name(event),
                    "user_id": str(event.user_id),
                    "event_time": float(item.get("event_time", 0.0) or 0.0),
                    "text_content": str(item.get("text") or ""),
                    "text": str(item.get("text") or ""),
                    "display_text": str(item.get("text") or ""),
                    "has_image": bool(item.get("has_image")),
                    "raw_has_image": bool(item.get("has_image")),
                    "image_count": 1 if item.get("has_image") else 0,
                    "raw_image_count": 1 if item.get("has_image") else 0,
                    "is_latest": index == len(pending_items) - 1,
                }
                for index, item in enumerate(pending_items)
            ],
            planning_signals={"window_batch_size": max(1, len(pending_items))},
            window_reason="group_window_merged" if len(pending_items) > 1 else "group_window_stable",
            bypassed=False,
        )

    def cleanup(self, *, active_keys: Optional[List[str]] = None) -> None:
        active_key_set = set(active_keys or [])
        has_active_keys = active_keys is not None
        now = time.time()
        max_window = max(
            30.0,
            float(getattr(self.host, "private_batch_window_seconds", self.config.private_window_seconds) or 0.0) * 8,
            float(getattr(self.host, "group_proactive_window_seconds", self.config.group_proactive_window_seconds) or 0.0) * 12,
        )
        stale_keys: List[str] = []
        for key, items in list(self._pending_inputs.items()):
            if has_active_keys and key not in active_key_set:
                stale_keys.append(key)
                continue
            latest_time = max((float(item.get("inserted_at", 0.0) or 0.0) for item in items), default=0.0)
            if latest_time and now - latest_time > max_window:
                stale_keys.append(key)
        for key in stale_keys:
            self._pending_inputs.pop(key, None)
            self._versions.pop(key, None)

    def get_pending_inputs(self) -> Dict[str, List[Dict[str, Any]]]:
        return {key: list(value) for key, value in self._pending_inputs.items()}

    def get_versions(self) -> Dict[str, int]:
        return dict(self._versions)

    def _should_bypass_group_window(self, event: MessageEvent, user_message: str) -> bool:
        normalized = str(user_message or "").strip()
        if self.host._is_direct_mention(event):
            return True
        if normalized.startswith("/"):
            return True
        if str(self.host._get_reply_to_message_id(event) or "").strip():
            return True
        return False

    def _private_hold_reason(self, event: MessageEvent, user_message: str) -> str:
        text = str(user_message or "").strip()
        normalized = re.sub(r"\s+", "", text).lower()
        if any(token in normalized for token in ["等等", "等下", "稍等", "先别回", "我补充", "我再发", "还没说完"]):
            return "用户明显还在继续补充，私聊先等待下一条消息"
        if self.host._has_image_input(event) and not normalized:
            return "私聊当前只有图片，先等待用户补充文字或更多上下文"
        return ""

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
        return {
            "batch_count": max(1, len(pending)),
            "merged_from_multiple_inputs": len(pending) > 1,
            "explicit_hold_signal": bool(self._private_hold_reason(event, user_message)),
            "has_image_without_text": bool(self.host._has_image_input(event) and not normalized),
            "looks_fragmented": bool(len(normalized) <= 6 and normalized in {"那个", "就是", "然后", "还有", "等会", "继续", "在吗"}),
            "ends_like_incomplete": bool(text.endswith(("...", "..", "。。。", "然后", "就是"))),
            "recent_gap_bucket": str(getattr(temporal_context, "recent_gap_bucket", "unknown") or "unknown"),
            "conversation_turn_count": len(conversation.messages),
            **build_companionship_signals(
                text,
                current_user_id=event.user_id,
                previous_speaker_role=previous_role,
                previous_user_id=event.user_id,
                recent_gap_bucket=str(getattr(temporal_context, "recent_gap_bucket", "unknown") or "unknown"),
                recent_history_count=len(conversation.messages),
            ),
        }

    async def _push_pending_input(self, *, conversation_key: str, event: MessageEvent, user_message: str) -> int:
        async with self._lock:
            self._versions[conversation_key] += 1
            version = self._versions[conversation_key]
            self._pending_inputs[conversation_key].append(
                {
                    "version": version,
                    "message_id": str(getattr(event, "message_id", "") or ""),
                    "event_time": float(getattr(event, "time", 0.0) or time.time()),
                    "inserted_at": time.time(),
                    "text": str(user_message or "").strip(),
                    "has_image": self.host._has_image_input(event),
                    "trace_id": get_execution_key(event),
                }
            )
            return version

    async def _has_newer_input(self, conversation_key: str, version: int) -> bool:
        async with self._lock:
            return int(self._versions.get(conversation_key, 0) or 0) > int(version)

    async def _consume_pending_input(self, conversation_key: str) -> List[Dict[str, Any]]:
        async with self._lock:
            return list(self._pending_inputs.pop(conversation_key, []))

    def _merge_pending_text(self, items: List[Dict[str, Any]], fallback_text: str) -> str:
        lines: List[str] = []
        for item in items:
            text = str(item.get("text") or "").strip()
            if text:
                lines.append(text)
        if not lines:
            return str(fallback_text or "").strip()
        merged: List[str] = []
        for text in lines:
            if merged and merged[-1] == text:
                continue
            merged.append(text)
        return "\n".join(merged)
