import logging
from typing import Any, Dict, Optional

from src.core.models import Conversation, MessageEvent, MessageType
from src.core.platform_models import InboundEvent, SessionRef
from src.core.platform_normalizers import get_or_normalize_onebot_inbound_event

logger = logging.getLogger(__name__)


class ConversationSessionManager:
    """Manage per-conversation chat state for private and group chats."""

    def __init__(self, conversation_store: Optional[Any] = None) -> None:
        self._conversations: Dict[str, Conversation] = {}
        self._conversation_store = conversation_store

    def get_key_for_session(self, session: SessionRef) -> str:
        return session.qualified_key

    def get_key_for_inbound_event(self, event: InboundEvent) -> str:
        return self.get_key_for_session(event.session)

    def get_key(self, event: MessageEvent) -> str:
        if event.message_type not in {MessageType.PRIVATE.value, MessageType.GROUP.value}:
            return f"unknown:{event.message_type}:{event.user_id}"
        inbound_event = get_or_normalize_onebot_inbound_event(event)
        return self.get_key_for_inbound_event(inbound_event)

    def get_optional(self, key: str) -> Optional[Conversation]:
        return self._conversations.get(key)

    def get(self, key: str) -> Conversation:
        conversation = self._conversations.get(key)
        if conversation is None:
            conversation = Conversation()
            self._conversations[key] = conversation
        return conversation

    def clear(self, key: str) -> bool:
        if key not in self._conversations:
            return False
        del self._conversations[key]
        return True

    def clear_for_event(self, event: MessageEvent) -> bool:
        return self.clear(self.get_key(event))

    def clean_expired(self) -> None:
        pass

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
        except Exception as exc:
            logger.warning("加载历史会话失败：%s，错误=%s", key, exc)
            return
        dialogue_key = self._dialogue_key_from_session_key(key)
        for record in sessions:
            if str(record.dialogue_key or "") == dialogue_key and record.turn_count > 0:
                for turn in record.turns:
                    user_text = str(turn.user or "").strip()
                    assistant_text = str(turn.assistant or "").strip()
                    if user_text:
                        conversation.add_message("user", user_text)
                    if assistant_text:
                        conversation.add_message("assistant", assistant_text)
                logger.debug("已恢复历史会话：key=%s，轮次=%s", key, record.turn_count)
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
