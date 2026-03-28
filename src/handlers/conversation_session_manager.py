import logging
from typing import Dict, Optional

from src.core.models import Conversation, MessageEvent, MessageType

logger = logging.getLogger(__name__)


class ConversationSessionManager:
    """Manage per-conversation chat state for private and group chats."""

    def __init__(self) -> None:
        self._conversations: Dict[str, Conversation] = {}

    def get_key(self, event: MessageEvent) -> str:
        if event.message_type == MessageType.PRIVATE.value:
            return f"private:{event.user_id}"
        return f"group:{event.group_id}:{event.user_id}"

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
        expired_keys = [
            key for key, conversation in self._conversations.items() if conversation.is_expired()
        ]
        for key in expired_keys:
            del self._conversations[key]
            logger.debug("已清理过期会话：%s", key)

    def count_active(self) -> int:
        self.clean_expired()
        return len(self._conversations)
