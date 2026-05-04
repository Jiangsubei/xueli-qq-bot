"""Immutable message log for group chat context management."""

from __future__ import annotations

import asyncio
import bisect
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from src.core.models import ImmutableMessage

logger = logging.getLogger(__name__)


class ImmutableMessageLog:
    """
    Append-only message log with time-based snapshot support.

    特性：
    - append-only，不修改不删除历史
    - 按 event_time 索引
    - 支持时间点快照
    """

    def __init__(self, group_id: str) -> None:
        self._group_id = group_id
        self._messages: List["ImmutableMessage"] = []

    @property
    def group_id(self) -> str:
        return self._group_id

    def append(self, message: "ImmutableMessage") -> None:
        """追加消息（单生产者，无需外部锁）"""
        self._messages.append(message)
        self._messages.sort(key=lambda m: m.event_time)

    def get_snapshot(self, before_time: float) -> Tuple[List["ImmutableMessage"], float]:
        """
        获取时间点快照

        Returns:
            (messages, snapshot_time) - 快照中的消息列表和快照时间点
        """
        idx = bisect.bisect_right(
            self._messages,
            before_time,
            key=lambda m: m.event_time,
        )
        return list(self._messages[:idx]), before_time

    def get_snapshot_before_or_at(self, before_time: float) -> Tuple[List["ImmutableMessage"], float]:
        """获取包含 before_time 时刻的快照"""
        idx = bisect.bisect_right(
            self._messages,
            before_time,
            key=lambda m: m.event_time,
        )
        return list(self._messages[:idx]), before_time

    @property
    def all_messages(self) -> List["ImmutableMessage"]:
        return list(self._messages)

    def __len__(self) -> int:
        return len(self._messages)

    def __repr__(self) -> str:
        return f"ImmutableMessageLog(group_id={self._group_id}, messages={len(self._messages)})"


class PersistentImmutableMessageLog(ImmutableMessageLog):
    """支持持久化的不可变日志"""

    def __init__(
        self,
        group_id: str,
        db_path: Optional[str] = None,
    ) -> None:
        super().__init__(group_id)
        self._db_path = db_path
        self._conn = None

    async def load_from_db(self) -> None:
        """从数据库加载历史消息"""
        if not self._db_path:
            return
        try:
            import aiosqlite

            async with aiosqlite.connect(self._db_path) as conn:
                conn.row_factory = aiosqlite.Row
                cursor = await conn.execute(
                    """
                    SELECT message_id, user_id, content, event_time, received_time, raw_data
                    FROM messages
                    WHERE group_id = ?
                    ORDER BY event_time ASC
                    """,
                    (self._group_id,),
                )
                rows = await cursor.fetchall()
                for row in rows:
                    from src.core.models import ImmutableMessage

                    import json

                    raw_data = json.loads(row["raw_data"]) if row["raw_data"] else {}
                    message = ImmutableMessage(
                        message_id=str(row["message_id"] or ""),
                        user_id=str(row["user_id"] or ""),
                        content=str(row["content"] or ""),
                        event_time=float(row["event_time"] or 0.0),
                        received_time=float(row["received_time"] or 0.0),
                        raw_data=raw_data,
                    )
                    self._messages.append(message)
                logger.debug(f"[不可变日志] 从数据库加载 {len(rows)} 条消息")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[不可变日志] 从数据库加载失败: {exc}")

    async def persist_append(self, message: "ImmutableMessage") -> None:
        """追加并持久化"""
        self.append(message)
        if self._db_path:
            await self._write_to_db(message)

    async def _write_to_db(self, message: "ImmutableMessage") -> None:
        """写入单条消息到数据库"""
        if not self._db_path:
            return
        try:
            import aiosqlite
            import json

            async with aiosqlite.connect(self._db_path) as conn:
                await conn.execute(
                    """
                    INSERT INTO messages (group_id, message_id, user_id, content, event_time, received_time, raw_data)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._group_id,
                        message.message_id,
                        message.user_id,
                        message.content,
                        message.event_time,
                        message.received_time,
                        json.dumps(message.raw_data, ensure_ascii=False),
                    ),
                )
                await conn.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[不可变日志] 写入数据库失败: {exc}")

    async def init_db(self) -> None:
        """初始化数据库表"""
        if not self._db_path:
            return
        try:
            import aiosqlite

            async with aiosqlite.connect(self._db_path) as conn:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_id TEXT NOT NULL,
                        message_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        content TEXT NOT NULL,
                        event_time REAL NOT NULL,
                        received_time REAL NOT NULL,
                        raw_data TEXT,
                        UNIQUE(group_id, message_id)
                    )
                    """,
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_messages_group_time ON messages(group_id, event_time)"
                )
                await conn.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[不可变日志] 初始化数据库失败: {exc}")
