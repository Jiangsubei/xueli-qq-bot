from __future__ import annotations

import asyncio
import logging
from typing import Dict

from src.memory.retrieval.bm25_index import BM25Index
from src.memory.retrieval.two_stage_retriever import TwoStageRetriever
from src.memory.retrieval.vector_index import VectorIndex
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
        self.vector_index = getattr(retriever, "vector_index", None)
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
            archived = await self.storage.get_archived_user_memories_raw(user_id)
            for arch_mem in archived:
                if not any(m.id == arch_mem.id for m in memories):
                    arch_mem.metadata["_index_archived"] = True
                    memories.append(arch_mem)
            success = self.bm25_index.build_index(user_id, memories)
            if self.vector_index and memories:
                self.vector_index.build_index(user_id, memories)
            if success:
                self.index_built[user_id] = True
                self.index_dirty[user_id] = False
                if archived:
                    logger.debug("[索引协调] 索引重建完成")
                else:
                    logger.debug("[索引协调] 索引重建完成")
            return success
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[索引协调] 索引重建失败")
            return False

    async def rebuild_all_indices(self) -> None:
        users_path = self.storage.users_path
        if not users_path.exists():
            return

        user_files = list(users_path.glob("*.md"))
        logger.debug("[索引协调] 开始重建索引")
        tasks = [self.rebuild_index(path.stem) for path in user_files]
        if not tasks:
            return

        results = await asyncio.gather(*tasks, return_exceptions=True)
        success_count = sum(1 for item in results if item is True)
        logger.debug("[索引协调] 索引重建完成")

    def mark_dirty(self, user_id: str) -> None:
        self.index_dirty[user_id] = True

    async def ensure_fresh(self, user_id: str) -> None:
        if self.index_dirty.get(user_id, False):
            await self.rebuild_index(user_id)

        if not self.index_built.get(user_id, False):
            await self.rebuild_index(user_id)


