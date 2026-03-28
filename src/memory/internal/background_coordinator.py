from __future__ import annotations

import asyncio
import logging
from typing import Callable, List, Optional

from src.memory.extraction.memory_extractor import MemoryExtractor
from src.memory.storage.conversation_store import ConversationStore
from src.memory.storage.markdown_store import MemoryItem

from .task_manager import MemoryTaskManager

logger = logging.getLogger(__name__)


class MemoryBackgroundCoordinator:
    """Schedule conversation saves and memory extraction around session files."""

    def __init__(
        self,
        *,
        conversation_store: ConversationStore,
        extractor: Optional[MemoryExtractor],
        task_manager: MemoryTaskManager,
        auto_extract_memory: bool,
        on_memory_changed: Callable[[str], None],
    ) -> None:
        self.conversation_store = conversation_store
        self.extractor = extractor
        self.task_manager = task_manager
        self.auto_extract_memory = auto_extract_memory
        self.on_memory_changed = on_memory_changed

    def register_dialogue_turn(
        self,
        user_id: str,
        user_message: str,
        assistant_message: str,
        *,
        dialogue_key: Optional[str] = None,
        message_type: str = "private",
        group_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> None:
        registration = self.conversation_store.add_turn(
            user_id=user_id,
            user_message=user_message,
            assistant_message=assistant_message,
            dialogue_key=dialogue_key,
            message_type=message_type,
            group_id=group_id,
            message_id=message_id,
        )
        logger.debug(
            "registered dialogue turn: user=%s session=%s turn=%s",
            user_id,
            registration.session_id,
            registration.turn_id,
        )

        if self.extractor:
            self.extractor.add_dialogue_turn(
                user_id=user_id,
                user_message=user_message,
                assistant_message=assistant_message,
                session_id=registration.session_id,
                turn_id=registration.turn_id,
                dialogue_key=registration.dialogue_key,
                message_type=message_type,
                group_id=group_id,
                message_id=message_id,
            )

        if registration.closed_session_id:
            self.schedule_conversation_save(
                registration.closed_session_user_id or user_id,
                session_id=registration.closed_session_id,
                force=True,
            )

        if registration.should_save_current:
            self.schedule_conversation_save(user_id, session_id=registration.session_id)

    def schedule_conversation_save(
        self,
        user_id: str,
        *,
        session_id: Optional[str] = None,
        dialogue_key: Optional[str] = None,
        force: bool = False,
    ) -> asyncio.Task:
        async def save_conversation() -> None:
            try:
                result = await self.conversation_store.save_conversation(
                    user_id=user_id,
                    session_id=session_id,
                    dialogue_key=dialogue_key,
                    force=force,
                )
                if result:
                    logger.info("saved conversation session: user=%s session=%s", user_id, result.session_id)
            except Exception as exc:
                logger.error("conversation save failed: user=%s error=%s", user_id, exc, exc_info=True)

        task_name = f"memory-save-{session_id or dialogue_key or user_id}"
        return self.task_manager.create_task(save_conversation(), name=task_name)

    async def maybe_extract_memories(
        self,
        user_id: str,
        *,
        dialogue_key: Optional[str] = None,
        message_type: str = "private",
        group_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> List[MemoryItem]:
        if not self.auto_extract_memory:
            logger.info("automatic memory extraction disabled: user=%s", user_id)
            return []
        if not self.extractor:
            logger.warning("memory extractor unavailable: user=%s", user_id)
            return []

        resolved_session_id = self._resolve_session_id(
            user_id=user_id,
            dialogue_key=dialogue_key,
            message_type=message_type,
            group_id=group_id,
            session_id=session_id,
        )
        if not resolved_session_id:
            return []

        if not self.extractor.should_extract(resolved_session_id):
            return []

        await self.conversation_store.save_conversation(user_id=user_id, session_id=resolved_session_id, force=True)
        memories = await self.extractor.extract_memories(user_id, session_id=resolved_session_id)
        if memories:
            self.on_memory_changed(user_id)
        return memories

    def schedule_memory_extraction(
        self,
        user_id: str,
        *,
        dialogue_key: Optional[str] = None,
        message_type: str = "private",
        group_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> asyncio.Task:
        async def extract() -> None:
            try:
                memories = await self.maybe_extract_memories(
                    user_id,
                    dialogue_key=dialogue_key,
                    message_type=message_type,
                    group_id=group_id,
                    session_id=session_id,
                )
                if memories:
                    logger.info("memory extraction complete: user=%s count=%s", user_id, len(memories))
            except Exception as exc:
                logger.error("memory extraction task failed: user=%s error=%s", user_id, exc, exc_info=True)

        task_name = f"memory-extract-{session_id or dialogue_key or user_id}"
        return self.task_manager.create_task(extract(), name=task_name)

    def force_extraction(
        self,
        user_id: str,
        *,
        dialogue_key: Optional[str] = None,
        message_type: str = "private",
        group_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> asyncio.Task:
        if not self.extractor:
            return self.task_manager.create_task(asyncio.sleep(0), name=f"memory-extract-empty-{user_id}")

        async def extract() -> List[MemoryItem]:
            resolved_session_id = self._resolve_session_id(
                user_id=user_id,
                dialogue_key=dialogue_key,
                message_type=message_type,
                group_id=group_id,
                session_id=session_id,
            )
            if not resolved_session_id:
                return []
            await self.conversation_store.save_conversation(user_id=user_id, session_id=resolved_session_id, force=True)
            memories = await self.extractor.extract_memories(user_id, session_id=resolved_session_id)
            if memories:
                self.on_memory_changed(user_id)
            return memories

        task_name = f"memory-force-extract-{session_id or dialogue_key or user_id}"
        return self.task_manager.create_task(extract(), name=task_name)

    def flush_conversation_session(
        self,
        *,
        user_id: str,
        message_type: str = "private",
        group_id: Optional[str] = None,
        dialogue_key: Optional[str] = None,
    ) -> Optional[asyncio.Task]:
        session_id = self.conversation_store.close_session(
            user_id=user_id,
            dialogue_key=dialogue_key,
            message_type=message_type,
            group_id=group_id,
        )
        if not session_id:
            return None
        if self.extractor:
            self.extractor.clear_buffer(session_id=session_id)
        return self.schedule_conversation_save(user_id, session_id=session_id, force=True)

    async def flush_conversation_buffers(self) -> None:
        for session_id in list(self.conversation_store.active_session_ids()):
            owner_user_id = self.conversation_store.get_session_owner(session_id)
            if not owner_user_id:
                continue
            await self.conversation_store.save_conversation(
                user_id=owner_user_id,
                session_id=session_id,
                force=True,
            )

    async def flush(self) -> None:
        await self.flush_conversation_buffers()
        await self.task_manager.flush()

    async def close(self) -> None:
        self.conversation_store.close_all_sessions()
        await self.flush_conversation_buffers()
        await self.task_manager.cancel_all()

    def _resolve_session_id(
        self,
        *,
        user_id: str,
        dialogue_key: Optional[str],
        message_type: str,
        group_id: Optional[str],
        session_id: Optional[str],
    ) -> str:
        if str(session_id or "").strip():
            return str(session_id).strip()
        return self.conversation_store.get_active_session_id(
            user_id=user_id,
            dialogue_key=dialogue_key,
            message_type=message_type,
            group_id=group_id,
        )
