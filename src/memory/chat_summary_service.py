from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from src.memory.storage.conversation_store import ConversationRecord, ConversationStore


def _now_iso() -> str:
    return datetime.now().isoformat()


@dataclass
class ChatSummaryService:
    """Build and persist compact summaries for closed conversation sessions."""

    conversation_store: ConversationStore
    max_points: int = 3
    max_point_chars: int = 72
    max_summary_chars: int = 220

    async def refresh_session_summary(
        self,
        *,
        user_id: str,
        record: ConversationRecord,
    ) -> Optional[ConversationRecord]:
        if record is None or not str(record.closed_at or "").strip() or record.turn_count <= 0:
            return None

        existing_summary = str((record.metadata or {}).get("session_summary") or "").strip()
        existing_turn_count = int((record.metadata or {}).get("session_summary_turn_count", 0) or 0)
        if existing_summary and existing_turn_count == record.turn_count:
            return record

        summary = self.build_summary(record)
        if not summary:
            return None

        return await self.conversation_store.update_session_metadata(
            user_id=str(user_id),
            session_id=record.session_id,
            metadata={
                "session_summary": summary,
                "session_summary_turn_count": record.turn_count,
                "session_summary_updated_at": _now_iso(),
                "last_active_at": str(record.updated_at or ""),
            },
        )

    def get_summary(self, record: ConversationRecord) -> str:
        existing = str((record.metadata or {}).get("session_summary") or "").strip()
        if existing:
            return existing
        return self.build_summary(record)

    def build_summary(self, record: ConversationRecord) -> str:
        points = []
        seen = set()
        for turn in reversed(list(record.turns or [])):
            user_text = self._normalize_fragment(getattr(turn, "user", ""))
            if user_text and user_text not in seen:
                seen.add(user_text)
                points.append(user_text)
            if len(points) >= max(1, int(self.max_points)):
                break

        points.reverse()
        if not points:
            last_assistant = self._normalize_fragment(getattr(record.turns[-1], "assistant", "")) if record.turns else ""
            return last_assistant[: self.max_summary_chars] if last_assistant else ""

        summary = "；".join(points)
        if len(summary) <= self.max_summary_chars:
            return summary
        return summary[: max(1, self.max_summary_chars - 3)].rstrip() + "..."

    def _normalize_fragment(self, text: str) -> str:
        normalized = re.sub(r"\s+", " ", str(text or "").strip())
        if not normalized:
            return ""
        if len(normalized) > self.max_point_chars:
            normalized = normalized[: max(1, self.max_point_chars - 3)].rstrip() + "..."
        return normalized
