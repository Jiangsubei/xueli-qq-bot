import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional

from src.core.models import Conversation, MessageEvent, MessageType
from src.core.platform_models import InboundEvent, SessionRef
from src.core.platform_normalizers import get_attached_inbound_event

logger = logging.getLogger(__name__)


class ConversationSessionManager:
    """Manage per-conversation chat state for private and group chats."""

    def __init__(self, conversation_store: Optional[Any] = None) -> None:
        self._conversations: Dict[str, Conversation] = {}
        self._conversation_store = conversation_store
        self._locks: Dict[str, asyncio.Lock] = {}
        self._meta_lock = asyncio.Lock()

    def get_key_for_session(self, session: SessionRef) -> str:
        return session.qualified_key

    def get_key_for_inbound_event(self, event: InboundEvent) -> str:
        return self.get_key_for_session(event.session)

    def get_key(self, event: MessageEvent) -> str:
        if event.message_type not in {MessageType.PRIVATE.value, MessageType.GROUP.value}:
            return f"unknown:{event.message_type}:{event.user_id}"
        inbound_event = get_attached_inbound_event(event)
        if inbound_event is not None:
            return self.get_key_for_inbound_event(inbound_event)
        if event.message_type == MessageType.PRIVATE.value:
            return f"private:{event.user_id}"
        group_id = event.raw_data.get("group_id", "")
        return f"group:{group_id}"

    def get_optional(self, key: str) -> Optional[Conversation]:
        return self._conversations.get(key)

    def get(self, key: str) -> Conversation:
        conversation = self._conversations.get(key)
        if conversation is None:
            conversation = Conversation()
            self._conversations[key] = conversation
        return conversation

    async def get_or_restore(self, key: str) -> Conversation:
        """获取会话，若为新建空会话则从数据库恢复历史消息（供异步上下文调用）。

        使用双检锁确保并发安全：
        1. 快速路径：已存在且有消息或无需恢复时，直接返回
        2. 获取锁：meta_lock 保护 locks 字典访问
        3. 双检：获取锁后再次检查是否已创建
        4. 创建/恢复：在锁内创建，锁外恢复
        """
        if key in self._conversations:
            conv = self._conversations[key]
            if conv.messages or not self._conversation_store:
                return conv
            async with self._locks.setdefault(key, asyncio.Lock()):
                if not conv.messages:
                    await self.restore(conv, key)
                return conv

        async with self._meta_lock:
            if key in self._locks:
                lock = self._locks[key]
            else:
                lock = asyncio.Lock()
                self._locks[key] = lock

        async with lock:
            if key in self._conversations:
                return self._conversations[key]

            conv = Conversation()
            self._conversations[key] = conv

        if self._conversation_store:
            await self.restore(conv, key)

        return conv

    def clear(self, key: str) -> bool:
        if key not in self._conversations:
            return False
        del self._conversations[key]
        return True

    def clear_for_event(self, event: MessageEvent) -> bool:
        return self.clear(self.get_key(event))

    def clean_expired(self) -> None:
        """移除超过 6 小时无活动且无消息的空闲会话。"""
        now = time.time()
        stale_keys = []
        for key, conv in self._conversations.items():
            age_seconds = now - conv.last_update
            if not conv.messages and age_seconds > 6 * 3600:
                stale_keys.append(key)
        for key in stale_keys:
            self._conversations.pop(key, None)
        if stale_keys:
            logger.debug("[会话管理器] 清理过期会话")

    def count_active(self) -> int:
        return len(self._conversations)

    async def restore(self, conversation: Conversation, key: str) -> None:
        """Load the most recent closed session's messages into an empty conversation."""
        if not self._conversation_store:
            return
        if conversation.messages:
            return
        try:
            sessions = await self._conversation_store.get_conversations(
                self._extract_user_id_from_key(key), limit=3
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[会话管理器] 加载历史会话失败")
            return
        dialogue_key = self._dialogue_key_from_session_key(key)
        for record in sessions:
            if str(record.dialogue_key or "") == dialogue_key and record.turn_count > 0:
                restored_session_time = self._parse_timestamp(record.closed_at or record.updated_at)
                if restored_session_time <= 0:
                    restored_session_time = max(
                        (self._parse_timestamp(getattr(turn, "timestamp", "")) for turn in list(record.turns or [])),
                        default=0.0,
                    )
                conversation.restored_previous_session_time = restored_session_time
                conversation.restored_session_id = str(record.session_id or "")
                conversation.restored_session_pending = True
                for turn in record.turns:
                    user_text = str(turn.user or "").strip()
                    assistant_text = str(turn.assistant or "").strip()
                    image_desc = str(turn.image_description or "").strip()
                    msg_id = str(turn.source_message_id or "").strip()
                    turn_timestamp = self._parse_timestamp(turn.timestamp)
                    if user_text:
                        conversation.add_message(
                            "user",
                            user_text,
                            timestamp=turn_timestamp,
                            image_description=image_desc,
                            message_id=msg_id,
                            restored=True,
                        )
                    if assistant_text:
                        conversation.add_message(
                            "assistant",
                            assistant_text,
                            timestamp=turn_timestamp,
                            message_id=msg_id,
                            restored=True,
                        )
                logger.debug("[会话管理器] 已恢复历史会话")
                return

    def _extract_user_id_from_key(self, key: str) -> str:
        parts = key.split(":")
        return parts[-1] if parts else ""

    def _dialogue_key_from_session_key(self, key: str) -> str:
        """Strip platform prefix from session key to get dialogue_key."""
        parts = key.split(":")
        if len(parts) >= 3:
            return ":".join(parts[1:])
        return key

    def _parse_timestamp(self, value: Any) -> float:
        text = str(value or "").strip()
        if not text:
            return 0.0
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return 0.0
