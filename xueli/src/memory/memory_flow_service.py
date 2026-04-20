from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.handlers.reply_pipeline import PreparedReplyRequest

logger = logging.getLogger(__name__)


class MemoryFlowService:
    """Coordinate reply-side memory effects without leaking them into prompt compilation."""

    def __init__(self, memory_manager: Any = None) -> None:
        self.memory_manager = memory_manager

    def on_reply_generated(
        self,
        *,
        host: Any,
        event: Any,
        prepared: "PreparedReplyRequest",
        reply_text: str,
    ) -> None:
        if not self.memory_manager or not str(prepared.original_user_message or "").strip():
            return
        try:
            dialogue_key = host._get_conversation_key(event)
            self.memory_manager.register_dialogue_turn(
                user_id=str(event.user_id),
                user_message=prepared.original_user_message,
                assistant_message=reply_text,
                dialogue_key=dialogue_key,
                message_type=event.message_type,
                group_id=str(event.group_id or ""),
                message_id=str(event.message_id or ""),
            )
            scheduler = getattr(self.memory_manager, "schedule_memory_extraction", None)
            if callable(scheduler):
                scheduler(
                    str(event.user_id),
                    dialogue_key=dialogue_key,
                    message_type=event.message_type,
                    group_id=str(event.group_id or ""),
                )
        except Exception as exc:
            logger.warning("记录记忆副作用失败：%s", exc, exc_info=True)
