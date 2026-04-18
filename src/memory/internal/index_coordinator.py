from __future__ import annotations

import asyncio
import logging
from typing import Dict

from src.memory.retrieval.bm25_index import BM25Index
from src.memory.retrieval.two_stage_retriever import TwoStageRetriever
from src.memory.storage.markdown_store import MarkdownMemoryStore

logger = logging.getLogger(__name__)


class MemoryIndexCoordinator:
    """Manage BM25 index lifecycle and dirty-state bookkeeping."""

    def __init__(
        self,
        *,
        storage: MarkdownMemoryStore,
        bm25_index: BM25Index,
        retriever: TwoStageRetriever,
        auto_build_index: bool,
    ) -> None:
        self.storage = storage
        self.bm25_index = bm25_index
        self.retriever = retriever
        self.auto_build_index = auto_build_index
        self.index_built: Dict[str, bool] = {}
        self.index_dirty: Dict[str, bool] = {}

    async def initialize(self) -> None:
        if self.auto_build_index:
            await self.rebuild_all_indices()
        if self.retriever.config.rerank_enabled:
            self.retriever.initialize_reranker()

    async def rebuild_index(self, user_id: str) -> bool:
        try:
            memories = await self.storage.get_user_memories(user_id)
            success = self.bm25_index.build_index(user_id, memories)
            if success:
                self.index_built[user_id] = True
                self.index_dirty[user_id] = False
                logger.debug("索引重建完成：用户=%s，记忆数=%s", user_id, len(memories))
            return success
        except Exception as exc:
            logger.error("索引重建失败：用户=%s，错误=%s", user_id, exc)
            return False

    async def rebuild_all_indices(self) -> None:
        users_path = self.storage.users_path
        if not users_path.exists():
            return

        user_files = list(users_path.glob("*.md"))
        logger.debug("开始重建索引：用户数=%s", len(user_files))
        tasks = [self.rebuild_index(path.stem) for path in user_files]
        if not tasks:
            return

        results = await asyncio.gather(*tasks, return_exceptions=True)
        success_count = sum(1 for item in results if item is True)
        logger.debug("索引重建完成：成功=%s/%s", success_count, len(tasks))

    def mark_dirty(self, user_id: str) -> None:
        self.index_dirty[user_id] = True

    async def ensure_fresh(self, user_id: str) -> None:
        if self.index_dirty.get(user_id, False):
            await self.rebuild_index(user_id)

        if not self.index_built.get(user_id, False):
            await self.rebuild_index(user_id)


