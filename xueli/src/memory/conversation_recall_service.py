from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.memory.storage.conversation_store import ConversationRecord, ConversationStore, ConversationTurn


@dataclass
class ConversationRecallService:
    """Locate prior discussion snippets for direct topic recall."""

    conversation_store: ConversationStore
    recent_session_limit: int = 12
    recall_entry_limit: int = 2
    min_match_score: float = 0.18
    max_excerpt_chars: int = 72
    recall_confidence_decay_per_day: float = 0.01
    recall_confidence_minimum: float = 0.3

    def compute_confidence(self, timestamp: Optional[str]) -> float:
        if not timestamp:
            return 1.0
        try:
            if timestamp.endswith("Z"):
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            else:
                dt = datetime.fromisoformat(timestamp)
        except (ValueError, TypeError):
            return 1.0
        now = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        days = (now - dt).total_seconds() / 86400.0
        confidence = 1.0 - days * self.recall_confidence_decay_per_day
        return max(self.recall_confidence_minimum, confidence)

    async def build_recall_entries(
        self,
        *,
        user_id: str,
        query: str,
        message_type: str = "private",
        group_id: Optional[str] = None,
        dialogue_key: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        normalized_query = self._normalize_text(query)
        if len(normalized_query) < 2:
            return []

        resolved_dialogue_key = self.conversation_store.build_dialogue_key(
            user_id=str(user_id),
            dialogue_key=dialogue_key,
            message_type=message_type,
            group_id=group_id,
        )
        sessions = await self.conversation_store.get_conversations(str(user_id), limit=max(1, int(self.recent_session_limit)))
        matched_sessions = [
            record
            for record in sessions
            if str(record.dialogue_key or "") == resolved_dialogue_key and record.turn_count > 0
        ]

        matches: List[Dict[str, Any]] = []
        for record in reversed(matched_sessions):
            for turn in record.turns:
                score = self._score_turn(query, turn)
                if score < self.min_match_score:
                    continue
                matches.append(
                    {
                        "record": record,
                        "turn": turn,
                        "score": score,
                        "recall_confidence": self.compute_confidence(str(turn.timestamp or record.started_at or "")),
                    }
                )

        if not matches:
            return []

        first_match = min(matches, key=lambda item: self._turn_sort_key(item["record"], item["turn"]))
        latest_match = max(
            matches,
            key=lambda item: (
                item["score"],
                *self._turn_sort_key(item["record"], item["turn"]),
            ),
        )

        selected = [first_match]
        if self._match_identity(first_match) != self._match_identity(latest_match):
            selected.append(latest_match)

        entries: List[Dict[str, Any]] = []
        for index, match in enumerate(selected[: max(1, int(self.recall_entry_limit))], start=1):
            label = "第一次提到相关话题" if index == 1 else "最近一次提到相关话题"
            entries.append(
                {
                    "content": self._format_entry(label=label, record=match["record"], turn=match["turn"]),
                    "metadata": {
                        "session_id": match["record"].session_id,
                        "dialogue_key": match["record"].dialogue_key,
                        "source_turn_start": match["turn"].turn_id,
                        "source_turn_end": match["turn"].turn_id,
                        "score": match["score"],
                        "recall_confidence": match.get("recall_confidence", 1.0),
                    },
                }
            )
        return entries

    def _format_entry(self, *, label: str, record: ConversationRecord, turn: ConversationTurn) -> str:
        stamp = str(turn.timestamp or record.updated_at or record.closed_at or "").replace("T", " ")[:16]
        prefix = f"{label}（{stamp}，第{int(turn.turn_id)}轮）" if stamp else f"{label}（第{int(turn.turn_id)}轮）"
        user_excerpt = self._excerpt(turn.user)
        assistant_excerpt = self._excerpt(turn.assistant)
        parts = [f"{prefix}：用户说“{user_excerpt}”"]
        if assistant_excerpt:
            parts.append(f"你当时回复“{assistant_excerpt}”")
        return "；".join(parts)

    def _excerpt(self, text: str) -> str:
        normalized = re.sub(r"\s+", " ", str(text or "").strip())
        if len(normalized) <= self.max_excerpt_chars:
            return normalized
        return normalized[: max(1, self.max_excerpt_chars - 3)].rstrip() + "..."

    def _score_turn(self, query: str, turn: ConversationTurn) -> float:
        query_text = self._normalize_text(query)
        if not query_text:
            return 0.0
        user_score = self._score_text(query_text, turn.user)
        assistant_score = self._score_text(query_text, turn.assistant) * 0.35
        return max(user_score, user_score + assistant_score)

    def _score_text(self, normalized_query: str, text: str) -> float:
        normalized_text = self._normalize_text(text)
        if not normalized_query or not normalized_text:
            return 0.0
        if normalized_query in normalized_text or normalized_text in normalized_query:
            return min(len(normalized_query), len(normalized_text)) / max(len(normalized_query), len(normalized_text))
        query_chars = set(normalized_query)
        text_chars = set(normalized_text)
        return len(query_chars & text_chars) / max(len(query_chars), 1)

    def _normalize_text(self, text: str) -> str:
        compact = re.sub(r"\s+", "", str(text or "").strip().lower())
        return re.sub(r"[^\w\u4e00-\u9fff]", "", compact)

    def _turn_sort_key(self, record: ConversationRecord, turn: ConversationTurn) -> tuple[str, int]:
        return (str(turn.timestamp or record.started_at or ""), int(turn.turn_id or 0))

    def _match_identity(self, match: Dict[str, Any]) -> tuple[str, int]:
        record = match["record"]
        turn = match["turn"]
        return str(record.session_id or ""), int(turn.turn_id or 0)
