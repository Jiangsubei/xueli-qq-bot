from __future__ import annotations

import asyncio
import copy
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiofiles

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now().isoformat()


@dataclass
class ConversationTurn:
    turn_id: int
    user: str
    assistant: str
    timestamp: str
    source_message_id: str
    source_group_id: str
    owner_user_id: str
    source_message_type: str = ""
    dialogue_key: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "user": self.user,
            "assistant": self.assistant,
            "timestamp": self.timestamp,
            "source_message_id": self.source_message_id,
            "source_group_id": self.source_group_id,
            "owner_user_id": self.owner_user_id,
            "source_message_type": self.source_message_type,
            "dialogue_key": self.dialogue_key,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConversationTurn":
        return cls(
            turn_id=int(data.get("turn_id", 0) or 0),
            user=str(data.get("user", "") or ""),
            assistant=str(data.get("assistant", "") or ""),
            timestamp=str(data.get("timestamp", "") or ""),
            source_message_id=str(data.get("source_message_id", "") or ""),
            source_group_id=str(data.get("source_group_id", "") or ""),
            owner_user_id=str(data.get("owner_user_id", "") or ""),
            source_message_type=str(data.get("source_message_type", "") or ""),
            dialogue_key=str(data.get("dialogue_key", "") or ""),
        )


@dataclass
class ConversationRecord:
    session_id: str
    dialogue_key: str
    user_id: str
    message_type: str
    group_id: str
    started_at: str
    updated_at: str
    closed_at: str
    turns: List[ConversationTurn] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    dirty_turns: int = 0

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "dialogue_key": self.dialogue_key,
            "user_id": self.user_id,
            "message_type": self.message_type,
            "group_id": self.group_id,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "closed_at": self.closed_at,
            "turns": [turn.to_dict() for turn in self.turns],
            "metadata": copy.deepcopy(self.metadata or {}),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConversationRecord":
        return cls(
            session_id=str(data.get("session_id", "") or ""),
            dialogue_key=str(data.get("dialogue_key", "") or ""),
            user_id=str(data.get("user_id", "") or ""),
            message_type=str(data.get("message_type", "") or ""),
            group_id=str(data.get("group_id", "") or ""),
            started_at=str(data.get("started_at", "") or ""),
            updated_at=str(data.get("updated_at", "") or ""),
            closed_at=str(data.get("closed_at", "") or ""),
            turns=[ConversationTurn.from_dict(item) for item in list(data.get("turns") or [])],
            metadata=dict(data.get("metadata") or {}),
            dirty_turns=0,
        )


@dataclass
class ConversationTurnRegistration:
    session_id: str
    turn_id: int
    turn_count: int
    user_id: str
    dialogue_key: str
    closed_session_id: str = ""
    closed_session_user_id: str = ""


class ConversationStore:
    """Persist raw conversation turns as session-scoped JSON files."""

    def __init__(
        self,
        base_path: str = "memories/conversations",
        session_timeout_seconds: int = 3600,
    ) -> None:
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._session_timeout_seconds = max(1, int(session_timeout_seconds))
        self._sessions: Dict[str, ConversationRecord] = {}
        self._active_session_by_dialogue: Dict[str, str] = {}

    def _get_user_dir(self, user_id: str) -> Path:
        user_dir = self.base_path / str(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir

    def _session_file_path(self, user_id: str, session_id: str) -> Path:
        return self._get_user_dir(user_id) / f"{session_id}.json"

    def _dialogue_key(
        self,
        *,
        user_id: str,
        dialogue_key: Optional[str] = None,
        message_type: str = "private",
        group_id: Optional[str] = None,
    ) -> str:
        if str(dialogue_key or "").strip():
            return str(dialogue_key).strip()
        if str(message_type or "private") == "group" and str(group_id or "").strip():
            return f"group:{group_id}:{user_id}"
        return f"private:{user_id}"

    def build_dialogue_key(
        self,
        *,
        user_id: str,
        dialogue_key: Optional[str] = None,
        message_type: str = "private",
        group_id: Optional[str] = None,
    ) -> str:
        return self._dialogue_key(
            user_id=user_id,
            dialogue_key=dialogue_key,
            message_type=message_type,
            group_id=group_id,
        )

    def _new_session_id(self, user_id: str, dialogue_key: str) -> str:
        normalized_dialogue = dialogue_key.replace(":", "_")
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        suffix = uuid.uuid4().hex[:8]
        return f"session_{user_id}_{normalized_dialogue}_{stamp}_{suffix}"

    def _session_expired(self, session: ConversationRecord) -> bool:
        if not session.updated_at:
            return False
        try:
            updated_at = datetime.fromisoformat(session.updated_at)
        except ValueError:
            return False
        return (datetime.now() - updated_at).total_seconds() > self._session_timeout_seconds

    def _ensure_session(
        self,
        *,
        user_id: str,
        dialogue_key: str,
        message_type: str,
        group_id: str,
    ) -> tuple[ConversationRecord, str]:
        active_session_id = self._active_session_by_dialogue.get(dialogue_key, "")
        closed_session_id = ""

        if active_session_id:
            existing = self._sessions.get(active_session_id)
            if existing is None:
                self._active_session_by_dialogue.pop(dialogue_key, None)
                active_session_id = ""
            elif self._session_expired(existing):
                existing.closed_at = existing.closed_at or _now_iso()
                closed_session_id = existing.session_id
                self._active_session_by_dialogue.pop(dialogue_key, None)
                active_session_id = ""
            else:
                return existing, closed_session_id

        now_text = _now_iso()
        session = ConversationRecord(
            session_id=self._new_session_id(user_id, dialogue_key),
            dialogue_key=dialogue_key,
            user_id=str(user_id),
            message_type=str(message_type or "private"),
            group_id=str(group_id or ""),
            started_at=now_text,
            updated_at=now_text,
            closed_at="",
            turns=[],
            metadata={},
            dirty_turns=0,
        )
        self._sessions[session.session_id] = session
        self._active_session_by_dialogue[dialogue_key] = session.session_id
        return session, closed_session_id

    def add_turn(
        self,
        user_id: str,
        user_message: str,
        assistant_message: str,
        *,
        dialogue_key: Optional[str] = None,
        message_type: str = "private",
        group_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> ConversationTurnRegistration:
        resolved_dialogue_key = self._dialogue_key(
            user_id=str(user_id),
            dialogue_key=dialogue_key,
            message_type=message_type,
            group_id=group_id,
        )
        session, closed_session_id = self._ensure_session(
            user_id=str(user_id),
            dialogue_key=resolved_dialogue_key,
            message_type=str(message_type or "private"),
            group_id=str(group_id or ""),
        )
        timestamp = _now_iso()
        turn = ConversationTurn(
            turn_id=len(session.turns) + 1,
            user=str(user_message or ""),
            assistant=str(assistant_message or ""),
            timestamp=timestamp,
            source_message_id=str(message_id or ""),
            source_group_id=str(group_id or ""),
            owner_user_id=str(user_id),
            source_message_type=str(message_type or "private"),
            dialogue_key=resolved_dialogue_key,
        )
        session.turns.append(turn)
        session.updated_at = timestamp
        session.message_type = str(message_type or session.message_type or "private")
        session.group_id = str(group_id or session.group_id or "")
        session.metadata["latest_message_id"] = str(message_id or "")
        session.dirty_turns += 1

        closed_user_id = ""
        if closed_session_id:
            closed_session = self._sessions.get(closed_session_id)
            closed_user_id = str(closed_session.user_id or user_id) if closed_session else str(user_id)

        return ConversationTurnRegistration(
            session_id=session.session_id,
            turn_id=turn.turn_id,
            turn_count=session.turn_count,
            user_id=str(user_id),
            dialogue_key=resolved_dialogue_key,
            closed_session_id=closed_session_id,
            closed_session_user_id=closed_user_id,
        )

    def get_active_session_id(
        self,
        *,
        user_id: str,
        dialogue_key: Optional[str] = None,
        message_type: str = "private",
        group_id: Optional[str] = None,
    ) -> str:
        resolved_dialogue_key = self._dialogue_key(
            user_id=str(user_id),
            dialogue_key=dialogue_key,
            message_type=message_type,
            group_id=group_id,
        )
        session_id = self._active_session_by_dialogue.get(resolved_dialogue_key, "")
        if not session_id:
            return ""
        session = self._sessions.get(session_id)
        if session is None:
            self._active_session_by_dialogue.pop(resolved_dialogue_key, None)
            return ""
        if self._session_expired(session):
            session.closed_at = session.closed_at or _now_iso()
            self._active_session_by_dialogue.pop(resolved_dialogue_key, None)
            return ""
        return session_id

    def get_session_owner(self, session_id: str) -> str:
        session = self._sessions.get(str(session_id))
        return str(session.user_id or "") if session else ""

    def get_session_snapshot(self, session_id: str) -> Optional[ConversationRecord]:
        session = self._sessions.get(str(session_id))
        if session is None:
            return None
        return ConversationRecord.from_dict(copy.deepcopy(session.to_dict()))

    def close_session(
        self,
        *,
        user_id: str,
        dialogue_key: Optional[str] = None,
        message_type: str = "private",
        group_id: Optional[str] = None,
    ) -> str:
        resolved_dialogue_key = self._dialogue_key(
            user_id=str(user_id),
            dialogue_key=dialogue_key,
            message_type=message_type,
            group_id=group_id,
        )
        session_id = self._active_session_by_dialogue.pop(resolved_dialogue_key, "")
        session = self._sessions.get(session_id)
        if session is None:
            return ""
        session.closed_at = session.closed_at or _now_iso()
        return session_id

    def close_all_sessions(self) -> List[str]:
        closed_ids: List[str] = []
        for session_id in list(self._active_session_by_dialogue.values()):
            session = self._sessions.get(session_id)
            if session is None:
                continue
            session.closed_at = session.closed_at or _now_iso()
            closed_ids.append(session_id)
        self._active_session_by_dialogue.clear()
        return closed_ids

    async def save_conversation(
        self,
        user_id: Optional[str] = None,
        *,
        dialogue_key: Optional[str] = None,
        session_id: Optional[str] = None,
        force: bool = False,
    ) -> Optional[ConversationRecord]:
        async with self._lock:
            resolved_session_id = str(session_id or "").strip()
            if not resolved_session_id and dialogue_key:
                resolved_session_id = self._active_session_by_dialogue.get(str(dialogue_key), "")
            if not resolved_session_id:
                return None

            session = self._sessions.get(resolved_session_id)
            if session is None or not session.turns:
                return None
            if not force:
                return None

            owner_user_id = str(user_id or session.user_id or "")
            payload = session.to_dict()
            file_path = self._session_file_path(owner_user_id, session.session_id)

            try:
                async with aiofiles.open(file_path, "w", encoding="utf-8") as handle:
                    await handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
                session.dirty_turns = 0
                logger.debug("对话会话已写入：用户=%s，会话=%s，轮次=%s", owner_user_id, session.session_id, session.turn_count)
                if session.closed_at:
                    self._sessions.pop(session.session_id, None)
                return ConversationRecord.from_dict(copy.deepcopy(payload))
            except Exception as exc:
                logger.error(
                    "保存对话会话失败：用户=%s，会话=%s，错误=%s",
                    owner_user_id,
                    session.session_id,
                    exc,
                    exc_info=True,
                )
                return None

    async def load_session(self, user_id: str, session_id: str) -> Optional[ConversationRecord]:
        file_path = self._session_file_path(str(user_id), str(session_id))
        if not file_path.exists():
            return None
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as handle:
                return ConversationRecord.from_dict(json.loads(await handle.read()))
        except Exception as exc:
            logger.warning(
                "加载对话会话失败：用户=%s，会话=%s，错误=%s",
                user_id,
                session_id,
                exc,
            )
            return None

    async def update_session_metadata(
        self,
        user_id: str,
        session_id: str,
        metadata: Dict[str, Any],
    ) -> Optional[ConversationRecord]:
        file_path = self._session_file_path(str(user_id), str(session_id))
        async with self._lock:
            payload: Optional[Dict[str, Any]] = None

            live_session = self._sessions.get(str(session_id))
            if live_session is not None:
                live_session.metadata.update(dict(metadata or {}))
                payload = live_session.to_dict()
            elif file_path.exists():
                try:
                    async with aiofiles.open(file_path, "r", encoding="utf-8") as handle:
                        payload = json.loads(await handle.read())
                except Exception as exc:
                    logger.warning(
                        "读取会话元数据失败：用户=%s，会话=%s，错误=%s",
                        user_id,
                        session_id,
                        exc,
                    )
                    return None

            if payload is None:
                return None

            payload["metadata"] = {**dict(payload.get("metadata") or {}), **dict(metadata or {})}
            try:
                async with aiofiles.open(file_path, "w", encoding="utf-8") as handle:
                    await handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
                return ConversationRecord.from_dict(copy.deepcopy(payload))
            except Exception as exc:
                logger.warning(
                    "写入会话元数据失败：用户=%s，会话=%s，错误=%s",
                    user_id,
                    session_id,
                    exc,
                )
                return None

    async def get_conversations(self, user_id: str, limit: int = 10) -> List[ConversationRecord]:
        user_dir = self.base_path / str(user_id)
        if not user_dir.exists():
            return []

        records: List[ConversationRecord] = []
        files = sorted(user_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        for file_path in files[:limit]:
            try:
                async with aiofiles.open(file_path, "r", encoding="utf-8") as handle:
                    records.append(ConversationRecord.from_dict(json.loads(await handle.read())))
            except Exception as exc:
                logger.warning("读取对话文件失败：文件=%s，错误=%s", file_path.name, exc)
        return records

    def active_session_ids(self) -> List[str]:
        return list(self._sessions.keys())
