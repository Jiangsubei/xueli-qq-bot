"""Context recorder for immutable timeline snapshot model."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from src.core.immutable_message_log import ImmutableMessageLog, PersistentImmutableMessageLog
from src.core.models import ConversationSnapshot, ImmutableMessage

if TYPE_CHECKING:
    from src.core.models import MessageEvent

logger = logging.getLogger(__name__)


class ContextRecorder:
    """
    上下文记录器（单一写入者）

    职责：
    - 接收所有群聊消息，按时间顺序追加写入不可变日志
    - 提供时间点快照读取接口
    - 单生产者模式，消并发写入竞态
    """

    def __init__(
        self,
        conversation_store: Optional[Any] = None,
        db_path: Optional[str] = None,
    ) -> None:
        self._logs: Dict[str, ImmutableMessageLog] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._meta_lock = asyncio.Lock()
        self._conversation_store = conversation_store
        self._db_path = db_path

    async def get_or_create_log(self, group_id: str) -> ImmutableMessageLog:
        """获取或创建不可变日志"""
        if group_id in self._logs:
            return self._logs[group_id]

        async with self._meta_lock:
            if group_id in self._locks:
                lock = self._locks[group_id]
            else:
                lock = asyncio.Lock()
                self._locks[group_id] = lock

        async with lock:
            if group_id in self._logs:
                return self._logs[group_id]

            if self._db_path:
                log: ImmutableMessageLog = PersistentImmutableMessageLog(group_id, self._db_path)
                await log.init_db()
                await log.load_from_db()
            else:
                log = ImmutableMessageLog(group_id)

            self._logs[group_id] = log
            return log

    async def record(
        self,
        group_id: str,
        message_id: str,
        user_id: str,
        content: str,
        event_time: float,
        received_time: Optional[float] = None,
        raw_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """记录消息到不可变日志"""
        log = await self.get_or_create_log(group_id)
        message = ImmutableMessage(
            message_id=message_id,
            user_id=user_id,
            content=content,
            event_time=event_time,
            received_time=received_time or time.time(),
            raw_data=raw_data or {},
        )
        if isinstance(log, PersistentImmutableMessageLog):
            await log.persist_append(message)
        else:
            log.append(message)

    async def record_from_event(
        self,
        event: "MessageEvent",
        group_id: Optional[str] = None,
    ) -> None:
        """从 MessageEvent 记录消息"""
        if group_id is None:
            group_id = event.raw_data.get("group_id") if event.raw_data else None
        if not group_id:
            return

        message_id = str(getattr(event, "message_id", "") or "")
        user_id = str(getattr(event, "user_id", "") or "")
        content = event.extract_text() if hasattr(event, "extract_text") else ""
        event_time = float(getattr(event, "time", time.time()) or time.time())
        raw_data = dict(getattr(event, "raw_data", {}) or {})

        await self.record(
            group_id=group_id,
            message_id=message_id,
            user_id=user_id,
            content=content,
            event_time=event_time,
            received_time=time.time(),
            raw_data=raw_data,
        )

    async def get_snapshot(
        self,
        group_id: str,
        before_time: float,
    ) -> Tuple[List[ImmutableMessage], float]:
        """
        获取时间点快照

        Returns:
            (messages, snapshot_time) - 快照中的消息列表和快照时间点
        """
        if group_id not in self._logs:
            return [], before_time

        log = self._logs[group_id]
        return log.get_snapshot(before_time)

    async def get_snapshot_for_event(
        self,
        event: "MessageEvent",
        group_id: Optional[str] = None,
    ) -> ConversationSnapshot:
        """获取事件对应时间点的快照"""
        if group_id is None:
            group_id = event.raw_data.get("group_id") if event.raw_data else None
        if not group_id:
            return ConversationSnapshot(
                group_id="",
                messages=[],
                snapshot_time=0.0,
                created_at=time.time(),
            )

        event_time = float(getattr(event, "time", time.time()) or time.time())
        messages, snapshot_time = await self.get_snapshot(group_id, event_time)
        return ConversationSnapshot(
            group_id=group_id,
            messages=messages,
            snapshot_time=snapshot_time,
            created_at=time.time(),
        )

    async def get_full_history(self, group_id: str) -> List[ImmutableMessage]:
        """获取完整历史（用于分析/调试）"""
        if group_id not in self._logs:
            return []
        return self._logs[group_id].all_messages

    async def close(self) -> None:
        """关闭记录器，清理资源"""
        self._logs.clear()
        self._locks.clear()
