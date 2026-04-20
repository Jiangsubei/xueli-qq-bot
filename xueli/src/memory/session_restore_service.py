from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.memory.chat_summary_service import ChatSummaryService
from src.memory.storage.conversation_store import ConversationRecord, ConversationStore


@dataclass
class SessionRestoreService:
    """Load recent same-dialogue session summaries for prompt restoration."""

    conversation_store: ConversationStore
    summary_service: ChatSummaryService
    recent_session_limit: int = 6
    restore_entry_limit: int = 2

    async def build_restore_entries(
        self,
        *,
        user_id: str,
        message_type: str = "private",
        group_id: Optional[str] = None,
        dialogue_key: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        resolved_dialogue_key = self.conversation_store.build_dialogue_key(
            user_id=str(user_id),
            dialogue_key=dialogue_key,
            message_type=message_type,
            group_id=group_id,
        )
        sessions = await self.conversation_store.get_conversations(str(user_id), limit=max(1, int(self.recent_session_limit)))
        matched = [
            record
            for record in sessions
            if str(record.dialogue_key or "") == resolved_dialogue_key and str(record.closed_at or "").strip() and record.turn_count > 0
        ]

        entries: List[Dict[str, Any]] = []
        for index, record in enumerate(matched[: max(1, int(self.restore_entry_limit))], start=1):
            summary = self.summary_service.get_summary(record)
            if not summary:
                continue
            entries.append(
                {
                    "content": self._format_restore_entry(index=index, record=record, summary=summary),
                    "metadata": {
                        "session_id": record.session_id,
                        "dialogue_key": record.dialogue_key,
                        "closed_at": record.closed_at,
                        "turn_count": record.turn_count,
                    },
                }
            )
        return entries

    def _format_restore_entry(self, *, index: int, record: ConversationRecord, summary: str) -> str:
        label = "上一轮会话" if index == 1 else f"更早一轮会话{index - 1}"
        closed_at = str(record.closed_at or record.updated_at or "").replace("T", " ")[:16]
        suffix = f"（{closed_at}，{record.turn_count}轮）" if closed_at else f"（{record.turn_count}轮）"
        return f"{label}{suffix}：{summary}"
