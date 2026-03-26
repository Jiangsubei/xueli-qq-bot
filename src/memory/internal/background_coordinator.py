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
    """Schedule save/extraction side effects through a shared task manager."""

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
    ) -> None:
        if self.extractor:
            self.extractor.add_dialogue_turn(user_id, user_message, assistant_message)

        turn_count = self.conversation_store.add_turn(user_id, user_message, assistant_message)
        logger.debug("对话缓存: user=%s, turns=%s", user_id, turn_count)

        if turn_count >= self.conversation_store._save_interval:
            self.schedule_conversation_save(user_id)

    def schedule_conversation_save(self, user_id: str) -> asyncio.Task:
        async def save_conversation() -> None:
            try:
                result = await self.conversation_store.save_conversation(user_id)
                if result:
                    logger.info("对话已保存: user=%s, record=%s", user_id, result.record_id)
            except Exception as exc:
                logger.error("对话保存失败: user=%s, error=%s", user_id, exc, exc_info=True)

        return self.task_manager.create_task(save_conversation(), name=f"memory-save-{user_id}")

    async def maybe_extract_memories(self, user_id: str) -> List[MemoryItem]:
        if not self.auto_extract_memory:
            logger.info("自动记忆提取已禁用: user=%s", user_id)
            return []

        if not self.extractor:
            logger.warning("无法提取记忆: user=%s, extractor 未初始化", user_id)
            return []

        should_extract = self.extractor.should_extract(user_id)
        logger.info("检查记忆提取: user=%s, trigger=%s", user_id, should_extract)
        if not should_extract:
            return []

        memories = await self.extractor.extract_memories(user_id)
        if memories:
            self.on_memory_changed(user_id)
        return memories

    def schedule_memory_extraction(self, user_id: str) -> asyncio.Task:
        async def extract() -> None:
            try:
                memories = await self.maybe_extract_memories(user_id)
                if memories:
                    logger.info("记忆提取完成: user=%s, memories=%s", user_id, len(memories))
            except Exception as exc:
                logger.error("记忆提取任务失败: user=%s, error=%s", user_id, exc, exc_info=True)

        return self.task_manager.create_task(extract(), name=f"memory-extract-{user_id}")

    def force_extraction(self, user_id: str) -> asyncio.Task:
        if not self.extractor:
            return self.task_manager.create_task(asyncio.sleep(0), name=f"memory-extract-empty-{user_id}")

        async def extract() -> List[MemoryItem]:
            memories = await self.extractor.extract_memories(user_id)
            if memories:
                self.on_memory_changed(user_id)
            return memories

        return self.task_manager.create_task(extract(), name=f"memory-force-extract-{user_id}")

    async def flush_conversation_buffers(self) -> None:
        user_ids = list(self.conversation_store._buffer.keys())
        for user_id in user_ids:
            await self.conversation_store.save_conversation(user_id, force=True)

    async def flush(self) -> None:
        await self.flush_conversation_buffers()
        await self.task_manager.flush()

    async def close(self) -> None:
        await self.flush_conversation_buffers()
        await self.task_manager.cancel_all()
