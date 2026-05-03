"""
SQLite-backed conversation storage.
Replaces the JSON-file-based ConversationStore for ACID persistence and
eliminated fragmentation (one DB file vs. many JSON files per session).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    image_description: str = ""

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
            "image_description": self.image_description,
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
            image_description=str(data.get("image_description", "") or ""),
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


_INIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    session_id          TEXT PRIMARY KEY,
    dialogue_key        TEXT NOT NULL,
    user_id             TEXT NOT NULL,
    message_type        TEXT DEFAULT 'private',
    group_id            TEXT DEFAULT '',
    started_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    closed_at           TEXT DEFAULT '',
    turn_count          INTEGER DEFAULT 0,
    latest_message_id   TEXT DEFAULT '',
    metadata            TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_conv_dialogue ON conversations(dialogue_key);
CREATE INDEX IF NOT EXISTS idx_conv_user_updated ON conversations(user_id, updated_at DESC);

-- One row per turn: stores user + assistant text together
CREATE TABLE IF NOT EXISTS conversation_turns (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id             INTEGER NOT NULL,
    session_id          TEXT NOT NULL,
    user                TEXT NOT NULL,
    assistant           TEXT NOT NULL,
    timestamp           TEXT NOT NULL,
    source_message_id   TEXT DEFAULT '',
    source_group_id     TEXT DEFAULT '',
    owner_user_id       TEXT NOT NULL,
    source_message_type TEXT DEFAULT 'private',
    dialogue_key        TEXT DEFAULT '',
    image_description   TEXT DEFAULT '',
    UNIQUE(session_id, turn_id)
);

CREATE INDEX IF NOT EXISTS idx_turns_session ON conversation_turns(session_id);
CREATE INDEX IF NOT EXISTS idx_turns_dialogue ON conversation_turns(dialogue_key, timestamp DESC);

-- Group chat message history
CREATE TABLE IF NOT EXISTS group_messages (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    group_key               TEXT NOT NULL,
    event_time              REAL NOT NULL,
    speaker_role            TEXT NOT NULL,
    speaker_name            TEXT DEFAULT '',
    text                    TEXT NOT NULL,
    display_text            TEXT NOT NULL,
    raw_text                TEXT NOT NULL,
    has_image               INTEGER DEFAULT 0,
    raw_image_count         INTEGER DEFAULT 0,
    image_context_enabled   INTEGER DEFAULT 0,
    image_count             INTEGER DEFAULT 0,
    message_shape           TEXT DEFAULT 'text_only',
    image_file_ids          TEXT DEFAULT '[]',
    per_image_descriptions  TEXT DEFAULT '[]',
    merged_description      TEXT DEFAULT '',
    vision_available        INTEGER DEFAULT 0,
    vision_failure_count    INTEGER DEFAULT 0,
    vision_success_count    INTEGER DEFAULT 0,
    vision_source           TEXT DEFAULT '',
    vision_error            TEXT DEFAULT '',
    message_id              TEXT DEFAULT '0',
    user_id                 TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_gm_group_time ON group_messages(group_key, event_time DESC);
"""


class SQLiteConversationStore:
    """SQLite-backed conversation and group-message storage."""

    def __init__(
        self,
        base_path: str = "memories/conversations",
        session_timeout_seconds: int = 3600,
    ) -> None:
        base = Path(base_path)
        base.mkdir(parents=True, exist_ok=True)
        self._db_path = base / "conversations.db"
        self._session_timeout_seconds = max(1, int(session_timeout_seconds))
        self._lock = asyncio.Lock()
        self._init_db()

        # In-memory active-session cache (session_id -> ConversationRecord)
        self._sessions: Dict[str, ConversationRecord] = {}
        self._active_session_by_dialogue: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Internal DB helpers
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        import sqlite3
        conn = sqlite3.connect(str(self._db_path), timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_INIT_SCHEMA)
        conn.close()
        logger.debug("[存储] SQLite 会话存储已初始化")

    def _connection(self) -> "sqlite3.Connection":
        import sqlite3
        return sqlite3.connect(str(self._db_path), timeout=30.0)

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
            # 群聊：整个群共用一个 key（不含 user_id），与 group_history_key 格式一致
            return f"group:{group_id}"
        return f"private:{user_id}"

    def _strip_platform_prefix(self, qualified_key: str) -> str:
        """去掉平台前缀，'qq:private:123' → 'private:123'，与 restore() 提取格式一致。"""
        parts = qualified_key.split(":")
        # 格式: platform:scope:id 或 scope:id
        if len(parts) >= 3:
            return ":".join(parts[1:])  # "qq:private:123" → "private:123"
        return qualified_key

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
        normalized = dialogue_key.replace(":", "_")
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        suffix = uuid.uuid4().hex[:8]
        return f"session_{user_id}_{normalized}_{stamp}_{suffix}"

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
                closed_session_id = self._save_session_sync(existing)
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

    # ------------------------------------------------------------------
    # Sync write helpers (called from async context)
    # ------------------------------------------------------------------

    def _save_session_sync(self, session: ConversationRecord) -> str:
        """Synchronously persist a session to SQLite (used on session timeout)."""
        if not session.turns:
            return ""
        import sqlite3
        conn = self._connection()
        try:
            metadata_json = json.dumps(session.metadata or {}, ensure_ascii=False)
            conn.execute(
                """
                INSERT OR REPLACE INTO conversations
                (session_id, dialogue_key, user_id, message_type, group_id,
                 started_at, updated_at, closed_at, turn_count, latest_message_id, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    session.dialogue_key,
                    session.user_id,
                    session.message_type,
                    session.group_id,
                    session.started_at,
                    session.updated_at,
                    session.closed_at or _now_iso(),
                    session.turn_count,
                    session.metadata.get("latest_message_id", ""),
                    metadata_json,
                ),
            )
            for turn in session.turns:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO conversation_turns
                    (turn_id, session_id, user, assistant, timestamp,
                     source_message_id, source_group_id, owner_user_id,
                     source_message_type, dialogue_key, image_description)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        turn.turn_id,
                        session.session_id,
                        turn.user,
                        turn.assistant,
                        turn.timestamp,
                        turn.source_message_id,
                        turn.source_group_id,
                        turn.owner_user_id,
                        turn.source_message_type,
                        turn.dialogue_key,
                        turn.image_description,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        return session.session_id

    # ------------------------------------------------------------------
    # ConversationTurns row mapper
    # ------------------------------------------------------------------

    def _rows_to_turns(self, rows: List[tuple], cols: List[str]) -> List[ConversationTurn]:
        turns: List[ConversationTurn] = []
        for row in rows:
            d = dict(zip(cols, row))
            turns.append(ConversationTurn(
                turn_id=int(d["turn_id"] or 0),
                user=str(d["user"] or ""),
                assistant=str(d["assistant"] or ""),
                timestamp=str(d["timestamp"] or ""),
                source_message_id=str(d["source_message_id"] or ""),
                source_group_id=str(d["source_group_id"] or ""),
                owner_user_id=str(d["owner_user_id"] or ""),
                source_message_type=str(d["source_message_type"] or ""),
                dialogue_key=str(d["dialogue_key"] or ""),
                image_description=str(d["image_description"] or ""),
            ))
        return turns

    # ------------------------------------------------------------------
    # Public API (mirrors ConversationStore interface)
    # ------------------------------------------------------------------

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
        image_description: str = "",
    ) -> ConversationTurnRegistration:
        resolved_dialogue_key = self._dialogue_key(
            user_id=str(user_id),
            dialogue_key=dialogue_key,
            message_type=message_type,
            group_id=group_id,
        )
        # 去掉平台前缀存储，与 restore() 提取格式保持一致
        stored_dialogue_key = self._strip_platform_prefix(resolved_dialogue_key)
        session, closed_session_id = self._ensure_session(
            user_id=str(user_id),
            dialogue_key=stored_dialogue_key,
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
            dialogue_key=stored_dialogue_key,
            image_description=str(image_description or ""),
        )
        session.turns.append(turn)
        session.updated_at = timestamp
        session.message_type = str(message_type or session.message_type or "private")
        session.group_id = str(group_id or session.group_id or "")
        session.metadata["latest_message_id"] = str(message_id or "")
        session.dirty_turns += 1

        # 每轮立即写入 SQLite（异步线程执行，避免阻塞事件循环）
        import asyncio
        asyncio.get_event_loop().run_in_executor(None, self._persist_turn_sync, session)

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

            return await self._persist_session(session)

    def _persist_turn_sync(self, session: ConversationRecord) -> None:
        """同步地将当前 session 的最新一条 turn 写入 SQLite（每轮立即持久化）。"""
        if not session.turns:
            return
        import sqlite3
        conn = None
        try:
            conn = self._connection()
            # 只写入 conversations 表的元数据（upsert）
            metadata_json = json.dumps(session.metadata or {}, ensure_ascii=False)
            closed_at = session.closed_at or _now_iso()
            conn.execute(
                """
                INSERT OR REPLACE INTO conversations
                (session_id, dialogue_key, user_id, message_type, group_id,
                 started_at, updated_at, closed_at, turn_count, latest_message_id, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    session.dialogue_key,
                    session.user_id,
                    session.message_type,
                    session.group_id,
                    session.started_at,
                    session.updated_at,
                    closed_at,
                    session.turn_count,
                    session.metadata.get("latest_message_id", ""),
                    metadata_json,
                ),
            )
            # 只写入最新的那条 turn
            turn = session.turns[-1]
            conn.execute(
                """
                INSERT OR REPLACE INTO conversation_turns
                (turn_id, session_id, user, assistant, timestamp,
                 source_message_id, source_group_id, owner_user_id,
                 source_message_type, dialogue_key, image_description)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn.turn_id,
                    session.session_id,
                    turn.user,
                    turn.assistant,
                    turn.timestamp,
                    turn.source_message_id,
                    turn.source_group_id,
                    turn.owner_user_id,
                    turn.source_message_type,
                    turn.dialogue_key,
                    turn.image_description,
                ),
            )
            conn.commit()
            logger.debug("[存储] 对话轮次已写入")
        except Exception as exc:
            logger.error("[存储] 保存对话轮次失败")
        finally:
            if conn:
                conn.close()

    async def _persist_session(self, session: ConversationRecord) -> Optional[ConversationRecord]:
        """完整持久化 session（会话关闭时调用，写入所有 turns）。"""
        import sqlite3
        owner_user_id = str(session.user_id or "")
        metadata_json = json.dumps(session.metadata or {}, ensure_ascii=False)
        closed_at = session.closed_at or _now_iso()
        session_dict = session.to_dict()
        session_id = session.session_id

        def _do_persist():
            conn = self._connection()
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO conversations
                    (session_id, dialogue_key, user_id, message_type, group_id,
                     started_at, updated_at, closed_at, turn_count, latest_message_id, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session.session_id,
                        session.dialogue_key,
                        session.user_id,
                        session.message_type,
                        session.group_id,
                        session.started_at,
                        session.updated_at,
                        closed_at,
                        session.turn_count,
                        session.metadata.get("latest_message_id", ""),
                        metadata_json,
                    ),
                )
                for turn in session.turns:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO conversation_turns
                        (turn_id, session_id, user, assistant, timestamp,
                         source_message_id, source_group_id, owner_user_id,
                         source_message_type, dialogue_key, image_description)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            turn.turn_id,
                            session.session_id,
                            turn.user,
                            turn.assistant,
                            turn.timestamp,
                            turn.source_message_id,
                            turn.source_group_id,
                            turn.owner_user_id,
                            turn.source_message_type,
                            turn.dialogue_key,
                            turn.image_description,
                        ),
                    )
                conn.commit()
                session.dirty_turns = 0
                logger.debug("[存储] 对话会话已写入")
            finally:
                conn.close()

        try:
            await asyncio.to_thread(_do_persist)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[存储] 保存对话会话失败")
            return None

        if session.closed_at:
            self._sessions.pop(session.session_id, None)
        return ConversationRecord.from_dict(copy.deepcopy(session_dict))

    async def load_session(self, user_id: str, session_id: str) -> Optional[ConversationRecord]:
        import sqlite3

        def _do_load():
            conn = self._connection()
            try:
                cur = conn.execute(
                    "SELECT * FROM conversations WHERE session_id = ?",
                    (str(session_id),),
                )
                row = cur.fetchone()
                if not row:
                    return None
                cols = [c[0] for c in cur.description]
                record_dict = dict(zip(cols, row))
                record_dict["metadata"] = json.loads(record_dict.get("metadata", "{}") or "{}")

                turn_cur = conn.execute(
                    "SELECT * FROM conversation_turns WHERE session_id = ? ORDER BY turn_id",
                    (str(session_id),),
                )
                turn_rows = turn_cur.fetchall()
                turn_cols = [c[0] for c in turn_cur.description]
                record_dict["turns"] = [t.to_dict() for t in self._rows_to_turns(turn_rows, turn_cols)]
                return ConversationRecord.from_dict(record_dict)
            finally:
                conn.close()

        try:
            return await asyncio.to_thread(_do_load)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[存储] 加载对话会话失败")
            return None

    async def update_session_metadata(
        self,
        user_id: str,
        session_id: str,
        metadata: Dict[str, Any],
    ) -> Optional[ConversationRecord]:
        import sqlite3

        sid = str(session_id)
        live = self._sessions.get(sid)
        live_dict = None
        if live is not None:
            live.metadata.update(dict(metadata or {}))
            live_dict = live.to_dict()
            payload_metadata = dict(live_dict.get("metadata") or {})
        else:
            live_dict = None
            payload_metadata = dict(metadata or {})

        def _do_update():
            conn = self._connection()
            try:
                base_metadata = {}
                if live_dict is None:
                    cur = conn.execute(
                        "SELECT metadata FROM conversations WHERE session_id = ?",
                        (sid,),
                    )
                    row = cur.fetchone()
                    if not row:
                        return None
                    base_metadata = json.loads(row[0] or "{}")
                merged_metadata = {**base_metadata, **payload_metadata}
                metadata_json = json.dumps(merged_metadata, ensure_ascii=False)
                conn.execute(
                    "UPDATE conversations SET metadata = ? WHERE session_id = ?",
                    (metadata_json, sid),
                )
                conn.commit()
                if live_dict is not None:
                    result_dict = copy.deepcopy(live_dict)
                    result_dict["metadata"] = merged_metadata
                else:
                    result_dict = {"metadata": merged_metadata}
                return result_dict
            finally:
                conn.close()

        try:
            result_dict = await asyncio.to_thread(_do_update)
            if result_dict is None:
                return None
            return ConversationRecord.from_dict(result_dict)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[存储] 写入会话元数据失败")
            return None

    async def get_conversations(self, user_id: str, limit: int = 10) -> List[ConversationRecord]:
        import sqlite3

        def _do_get():
            conn = self._connection()
            try:
                cur = conn.execute(
                    """
                    SELECT * FROM conversations
                    WHERE user_id = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (str(user_id), limit),
                )
                rows = cur.fetchall()
                cols = [c[0] for c in cur.description]
                records: List[ConversationRecord] = []
                for row in rows:
                    record_dict = dict(zip(cols, row))
                    record_dict["metadata"] = json.loads(record_dict.get("metadata", "{}") or "{}")

                    turn_cur = conn.execute(
                        "SELECT * FROM conversation_turns WHERE session_id = ? ORDER BY turn_id",
                        (record_dict["session_id"],),
                    )
                    turn_rows = turn_cur.fetchall()
                    turn_cols = [c[0] for c in turn_cur.description]
                    record_dict["turns"] = [t.to_dict() for t in self._rows_to_turns(turn_rows, turn_cols)]
                    records.append(ConversationRecord.from_dict(record_dict))
                return records
            finally:
                conn.close()

        return await asyncio.to_thread(_do_get)

    def active_session_ids(self) -> List[str]:
        return list(self._sessions.keys())

    # ------------------------------------------------------------------
    # Group message storage
    # ------------------------------------------------------------------

    async def add_group_message(self, group_key: str, message: Dict[str, Any]) -> None:
        """Persist a group chat message to SQLite."""
        import sqlite3

        def _do_add():
            conn = self._connection()
            try:
                conn.execute(
                    """
                    INSERT INTO group_messages
                    (group_key, event_time, speaker_role, speaker_name, text, display_text,
                     raw_text, has_image, raw_image_count, image_context_enabled, image_count,
                     message_shape, image_file_ids, per_image_descriptions, merged_description,
                     vision_available, vision_failure_count, vision_success_count,
                     vision_source, vision_error, message_id, user_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        group_key,
                        float(message.get("event_time", 0.0) or 0.0),
                        str(message.get("speaker_role", "user") or "user"),
                        str(message.get("speaker_name", "") or ""),
                        str(message.get("text", "") or ""),
                        str(message.get("display_text", "") or ""),
                        str(message.get("raw_text", "") or ""),
                        int(bool(message.get("has_image"))),
                        int(message.get("raw_image_count", 0) or 0),
                        int(bool(message.get("image_context_enabled"))),
                        int(message.get("image_count", 0) or 0),
                        str(message.get("message_shape", "text_only") or "text_only"),
                        json.dumps(message.get("image_file_ids", []), ensure_ascii=False),
                        json.dumps(message.get("per_image_descriptions", []), ensure_ascii=False),
                        str(message.get("merged_description", "") or ""),
                        int(bool(message.get("vision_available"))),
                        int(message.get("vision_failure_count", 0) or 0),
                        int(message.get("vision_success_count", 0) or 0),
                        str(message.get("vision_source", "") or ""),
                        str(message.get("vision_error", "") or ""),
                        str(message.get("message_id", "0") or "0"),
                        str(message.get("user_id", "") or ""),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

        async with self._lock:
            await asyncio.to_thread(_do_add)

    async def get_recent_group_messages(
        self,
        group_key: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Load the most recent `limit` group messages for `group_key`, in chronological order."""
        import sqlite3
        # 归一化：支持 "qq:group:123456" 和 "group:123456" 两种格式查询
        parts = group_key.split(":")
        normalized_key = f"group:{parts[-1]}" if len(parts) >= 2 and parts[-1].isdigit() else group_key

        def _do_get():
            conn = self._connection()
            try:
                cur = conn.execute(
                    """
                    SELECT * FROM group_messages
                    WHERE group_key = ?
                    ORDER BY event_time DESC
                    LIMIT ?
                    """,
                    (normalized_key, limit),
                )
                results: List[Dict[str, Any]] = []
                for row in cur.fetchall():
                    cols = [c[0] for c in cur.description]
                    d = dict(zip(cols, row))
                    d["has_image"] = bool(d["has_image"])
                    d["raw_has_image"] = bool(d["has_image"])
                    d["image_context_enabled"] = bool(d["image_context_enabled"])
                    d["vision_available"] = bool(d["vision_available"])
                    d["image_file_ids"] = json.loads(d.get("image_file_ids", "[]"))
                    d["per_image_descriptions"] = json.loads(d.get("per_image_descriptions", "[]"))
                    results.append(d)
                return list(reversed(results))
            finally:
                conn.close()

        return await asyncio.to_thread(_do_get)

    async def clear_group_messages(self, group_key: str) -> None:
        """Delete all messages for a group_key."""
        import sqlite3

        def _do_clear():
            conn = self._connection()
            try:
                conn.execute("DELETE FROM group_messages WHERE group_key = ?", (group_key,))
                conn.commit()
            finally:
                conn.close()

        async with self._lock:
            await asyncio.to_thread(_do_clear)
