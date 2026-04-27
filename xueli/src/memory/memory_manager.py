"""
Memory manager facade coordinating storage, retrieval, extraction, migration,
and runtime-safe background work.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from src.core.runtime_metrics import RuntimeMetrics
from src.memory.chat_summary_service import ChatSummaryService
from src.memory.conversation_recall_service import ConversationRecallService
from src.memory.extraction.memory_extractor import ExtractionConfig, MemoryExtractor
from src.memory.internal import (
    MemoryAccessContext,
    MemoryAccessPolicy,
    MemoryBackgroundCoordinator,
    MemoryIndexCoordinator,
    MemoryRetrievalCoordinator,
    MemoryTaskManager,
)
from src.memory.retrieval.bm25_index import BM25Index, SearchResult
from src.memory.retrieval.two_stage_retriever import RetrievalConfig, TwoStageRetriever
from src.memory.retrieval.vector_index import VectorIndex
from src.memory.person_fact_service import PersonFactService
from src.memory.session_restore_service import SessionRestoreService
from src.memory.storage.sqlite_conversation_store import SQLiteConversationStore
from src.memory.storage.important_memory_store import ImportantMemoryItem, ImportantMemoryStore
from src.memory.storage.markdown_store import MemoryItem, MarkdownMemoryStore
from src.memory.storage.person_fact_store import PersonFactItem, PersonFactStore

logger = logging.getLogger(__name__)


@dataclass
class MemoryManagerConfig:
    """Memory manager configuration."""

    storage_base_path: str = "memories"
    retrieval_config: RetrievalConfig = field(default_factory=RetrievalConfig)
    extraction_config: ExtractionConfig = field(default_factory=ExtractionConfig)
    ordinary_decay_enabled: bool = True
    ordinary_half_life_days: float = 30.0
    ordinary_forget_threshold: float = 0.5
    auto_build_index: bool = True
    auto_extract_memory: bool = True
    user_important_budget_chars: int = 360
    addressing_budget_chars: int = 180
    shared_budget_chars: int = 260
    session_restore_budget_chars: int = 260
    precise_recall_budget_chars: int = 260
    dynamic_budget_chars: int = 420


class MemoryManager:
    """Stable public facade for the memory subsystem."""

    def __init__(
        self,
        llm_callback: Optional[Callable[[str, List[Dict[str, str]]], Any]] = None,
        config: Optional[MemoryManagerConfig] = None,
        runtime_metrics: Optional[RuntimeMetrics] = None,
    ):
        self.config = config or MemoryManagerConfig()
        self.llm_callback = llm_callback
        self.runtime_metrics = runtime_metrics

        self.storage = MarkdownMemoryStore(
            base_path=self.config.storage_base_path,
            ordinary_decay_enabled=self.config.ordinary_decay_enabled,
            ordinary_half_life_days=self.config.ordinary_half_life_days,
            ordinary_forget_threshold=self.config.ordinary_forget_threshold,
        )
        self.bm25_index = BM25Index()
        self.vector_index = VectorIndex()
        self.retriever = TwoStageRetriever(
            bm25_index=self.bm25_index,
            config=self.config.retrieval_config,
            vector_index=self.vector_index,
        )
        self.important_memory_store = ImportantMemoryStore(
            base_path=os.path.join(self.config.storage_base_path, "important")
        )
        self.person_fact_store = PersonFactStore(
            base_path=os.path.join(self.config.storage_base_path, "person_facts")
        )
        self.conversation_store = SQLiteConversationStore(
            base_path=os.path.join(self.config.storage_base_path, "conversations"),
        )
        self.chat_summary_service = ChatSummaryService(
            conversation_store=self.conversation_store,
        )
        self.session_restore_service = SessionRestoreService(
            conversation_store=self.conversation_store,
            summary_service=self.chat_summary_service,
        )
        self.access_policy = MemoryAccessPolicy()
        self.conversation_recall_service = ConversationRecallService(
            conversation_store=self.conversation_store,
        )
        self.person_fact_service = PersonFactService(
            store=self.person_fact_store,
            important_memory_store=self.important_memory_store,
            access_policy=self.access_policy,
        )

        self.extractor: Optional[MemoryExtractor] = None
        if llm_callback:
            self.extractor = MemoryExtractor(
                memory_store=self.storage,
                llm_callback=llm_callback,
                config=self.config.extraction_config,
                important_memory_store=self.important_memory_store,
            )
        else:
            logger.warning("未提供 llm_callback，自动记忆提取已禁用")

        self.task_manager = MemoryTaskManager()
        self.index_coordinator = MemoryIndexCoordinator(
            storage=self.storage,
            bm25_index=self.bm25_index,
            retriever=self.retriever,
            auto_build_index=self.config.auto_build_index,
        )
        self.retrieval_coordinator = MemoryRetrievalCoordinator(
            storage=self.storage,
            important_memory_store=self.important_memory_store,
            conversation_store=self.conversation_store,
            retriever=self.retriever,
            index_coordinator=self.index_coordinator,
            session_restore_service=self.session_restore_service,
            conversation_recall_service=self.conversation_recall_service,
            access_policy=self.access_policy,
            runtime_metrics=self.runtime_metrics,
            prompt_budgets={
                "user_important": self.config.user_important_budget_chars,
                "addressing": self.config.addressing_budget_chars,
                "shared": self.config.shared_budget_chars,
                "session_restore": self.config.session_restore_budget_chars,
                "precise_recall": self.config.precise_recall_budget_chars,
                "dynamic": self.config.dynamic_budget_chars,
            },
        )
        self.background_coordinator = MemoryBackgroundCoordinator(
            conversation_store=self.conversation_store,
            extractor=self.extractor,
            summary_service=self.chat_summary_service,
            person_fact_service=self.person_fact_service,
            task_manager=self.task_manager,
            auto_extract_memory=self.config.auto_extract_memory,
            on_memory_changed=self.index_coordinator.mark_dirty,
            storage=self.storage,
            important_memory_store=self.important_memory_store,
            llm_callback=llm_callback,
        )

    async def initialize(self):
        logger.debug("开始初始化记忆管理器")
        migration_count = await self._migrate_existing_memories()
        if migration_count:
            self._inc_memory_migrations(migration_count)
        compaction_count = await self._compact_existing_memories()
        if compaction_count:
            self._inc_memory_compactions(compaction_count)
        await self._sync_existing_person_facts()
        await self.index_coordinator.initialize()
        self.background_coordinator.start_digestion()
        logger.debug("记忆管理器初始化完成")

    async def rebuild_index(self, user_id: str):
        return await self.index_coordinator.rebuild_index(user_id)

    async def rebuild_all_indices(self):
        await self.index_coordinator.rebuild_all_indices()

    def mark_index_dirty(self, user_id: str):
        self.index_coordinator.mark_dirty(user_id)

    async def ensure_index_fresh(self, user_id: str):
        await self.index_coordinator.ensure_fresh(user_id)

    def build_access_context(
        self,
        *,
        user_id: str,
        message_type: str = "private",
        group_id: Optional[str] = None,
        read_scope: Optional[str] = None,
    ) -> MemoryAccessContext:
        return self.access_policy.build_context(
            requester_user_id=str(user_id),
            message_type=message_type,
            group_id=str(group_id or ""),
            read_scope=read_scope or "user",
        )

    def _get_search_result_score(self, result: SearchResult) -> float:
        return self.retrieval_coordinator.get_search_result_score(result)

    async def add_memory(
        self,
        content: str,
        user_id: Optional[str] = None,
        source: str = "",
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict] = None,
    ) -> Optional[MemoryItem]:
        normalized_metadata = self.access_policy.normalize_memory_record(
            content=content,
            owner_user_id=str(user_id or ""),
            metadata=self._merge_tags_into_metadata(tags, metadata),
            source=source,
        )
        result = await self.storage.add_memory(
            content=content,
            user_id=user_id,
            source=source,
            tags=tags,
            metadata=normalized_metadata,
        )
        if result and user_id:
            self.mark_index_dirty(user_id)
            self._inc_memory_writes()
        return result

    async def search_memories(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        use_rerank: Optional[bool] = None,
        read_scope: Optional[str] = None,
        message_type: str = "private",
        group_id: Optional[str] = None,
        access_context: Optional[MemoryAccessContext] = None,
    ) -> List[SearchResult]:
        return await self.retrieval_coordinator.search_memories(
            user_id=user_id,
            query=query,
            top_k=top_k,
            use_rerank=use_rerank,
            read_scope=read_scope,
            access_context=self._resolve_memory_access_context(
                user_id=user_id,
                read_scope=read_scope,
                message_type=message_type,
                group_id=group_id,
                access_context=access_context,
            ),
        )

    async def quick_check_relevance(
        self,
        user_id: str,
        query: str,
        threshold: float = 0.5,
    ) -> Optional[MemoryItem]:
        return await self.retrieval_coordinator.quick_check_relevance(
            user_id=user_id,
            query=query,
            threshold=threshold,
        )

    async def search_memories_with_context(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        include_conversations: bool = True,
        include_sections: Optional[Dict[str, bool]] = None,
        section_intensity: Optional[Dict[str, str]] = None,
        read_scope: Optional[str] = None,
        message_type: str = "private",
        group_id: Optional[str] = None,
        access_context: Optional[MemoryAccessContext] = None,
    ) -> Dict[str, Any]:
        result = await self.retrieval_coordinator.search_memories_with_context(
            user_id=user_id,
            query=query,
            top_k=top_k,
            include_conversations=include_conversations,
            include_sections=include_sections,
            section_intensity=section_intensity,
            read_scope=read_scope,
            access_context=self._resolve_memory_access_context(
                user_id=user_id,
                read_scope=read_scope,
                message_type=message_type,
                group_id=group_id,
                access_context=access_context,
            ),
        )
        used_memory_ids: List[str] = result.pop("used_memory_ids", [])
        if used_memory_ids:
            self._schedule_mark_recalled(user_id, used_memory_ids)
        return result

    async def build_prompt_context(
        self,
        *,
        user_id: str,
        query: str,
        include_sections: Optional[Dict[str, bool]] = None,
        section_intensity: Optional[Dict[str, str]] = None,
        read_scope: Optional[str] = None,
        message_type: str = "private",
        group_id: Optional[str] = None,
        access_context: Optional[MemoryAccessContext] = None,
    ) -> Dict[str, Any]:
        result = await self.retrieval_coordinator.build_prompt_context(
            user_id=user_id,
            query=query,
            include_sections=include_sections,
            section_intensity=section_intensity,
            read_scope=read_scope,
            access_context=self._resolve_memory_access_context(
                user_id=user_id,
                read_scope=read_scope,
                message_type=message_type,
                group_id=group_id,
                access_context=access_context,
            ),
        )
        used_memory_ids: List[str] = result.pop("used_memory_ids", [])
        if used_memory_ids:
            self._schedule_mark_recalled(user_id, used_memory_ids)
        return result

    async def search_important_memories(
        self,
        user_id: str,
        query: str,
        limit: int = 5,
        read_scope: Optional[str] = None,
        message_type: str = "private",
        group_id: Optional[str] = None,
        access_context: Optional[MemoryAccessContext] = None,
    ) -> List[Dict[str, Any]]:
        return await self.retrieval_coordinator.search_important_memories(
            user_id=user_id,
            query=query,
            limit=limit,
            read_scope=read_scope,
            access_context=self._resolve_memory_access_context(
                user_id=user_id,
                read_scope=read_scope,
                message_type=message_type,
                group_id=group_id,
                access_context=access_context,
            ),
        )

    def _resolve_memory_access_context(
        self,
        *,
        user_id: str,
        read_scope: Optional[str],
        message_type: str,
        group_id: Optional[str],
        access_context: Optional[MemoryAccessContext],
    ) -> MemoryAccessContext:
        if access_context is not None:
            return access_context
        return self.build_access_context(
            user_id=str(user_id),
            message_type=message_type,
            group_id=group_id,
            read_scope=read_scope,
        )

    async def get_user_memories(self, user_id: str) -> List[MemoryItem]:
        return await self.storage.get_user_memories(user_id)

    async def mark_recalled_memories(self, user_id: str, memory_ids: List[str]) -> int:
        """标记记忆被召回使用，增加使用痕迹。

        按 ID 前缀路由到对应存储层：
        - imp_* → ImportantMemoryStore
        - mem_* / 其他 → MarkdownMemoryStore
        返回总更新条目数。
        """
        if not memory_ids:
            return 0

        ordinary_ids = [mid for mid in memory_ids if not str(mid).startswith("imp_")]
        important_ids = [mid for mid in memory_ids if str(mid).startswith("imp_")]

        total = 0
        if ordinary_ids:
            total += await self.storage.mark_recalled(user_id, ordinary_ids)
            if ordinary_ids:
                self.mark_index_dirty(user_id)
        if important_ids:
            total += await self.important_memory_store.mark_recalled(user_id, important_ids)
        return total

    def _schedule_mark_recalled(self, user_id: str, memory_ids: List[str]) -> None:
        async def _mark():
            try:
                updated = await self.mark_recalled_memories(user_id, memory_ids)
                if updated:
                    logger.debug("记忆召回回写完成：用户=%s，更新条目=%s", user_id, updated)
            except Exception as exc:
                logger.debug("记忆召回回写失败（非致命）：用户=%s，错误=%s", user_id, exc)

        self.task_manager.create_task(_mark(), name=f"memory-mark-recalled-{user_id}")

    async def get_person_facts(
        self,
        user_id: str,
        access_context: Optional[MemoryAccessContext] = None,
        limit: int = 6,
    ) -> List[Dict[str, Any]]:
        return await self.person_fact_service.get_prompt_entries(
            user_id=user_id,
            access_context=access_context,
            limit=limit,
        )

    async def format_person_facts_for_prompt(
        self,
        user_id: str,
        access_context: Optional[MemoryAccessContext] = None,
        limit: int = 6,
    ) -> str:
        return await self.person_fact_service.format_facts_for_prompt(
            user_id=user_id,
            access_context=access_context,
            limit=limit,
        )

    async def delete_memory(self, mem_id: str, user_id: Optional[str] = None) -> bool:
        result = await self.storage.delete_memory(mem_id, user_id)
        if result and user_id:
            self.mark_index_dirty(user_id)
        return result

    async def update_memory(
        self,
        mem_id: str,
        content: str,
        user_id: Optional[str] = None,
    ) -> bool:
        result = await self.storage.update_memory(mem_id, content, user_id)
        if result and user_id:
            self.mark_index_dirty(user_id)
        return result

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
        image_description: str = "",
    ):
        self.background_coordinator.register_dialogue_turn(
            user_id=user_id,
            user_message=user_message,
            assistant_message=assistant_message,
            dialogue_key=dialogue_key,
            message_type=message_type,
            group_id=group_id,
            message_id=message_id,
            image_description=image_description,
        )
        self._sync_background_task_metric()

    async def maybe_extract_memories(
        self,
        user_id: str,
        dialogue_key: Optional[str] = None,
        *,
        message_type: str = "private",
        group_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> List[MemoryItem]:
        return await self.background_coordinator.maybe_extract_memories(
            user_id,
            dialogue_key=dialogue_key,
            message_type=message_type,
            group_id=group_id,
            session_id=session_id,
        )

    def schedule_memory_extraction(
        self,
        user_id: str,
        dialogue_key: Optional[str] = None,
        *,
        message_type: str = "private",
        group_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ):
        task = self.background_coordinator.schedule_memory_extraction(
            user_id,
            dialogue_key=dialogue_key,
            message_type=message_type,
            group_id=group_id,
            session_id=session_id,
        )
        self._sync_background_task_metric()
        return task

    def force_extraction(
        self,
        user_id: str,
        dialogue_key: Optional[str] = None,
        *,
        message_type: str = "private",
        group_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ):
        task = self.background_coordinator.force_extraction(
            user_id,
            dialogue_key=dialogue_key,
            message_type=message_type,
            group_id=group_id,
            session_id=session_id,
        )
        self._sync_background_task_metric()
        return task

    def flush_conversation_session(
        self,
        *,
        user_id: str,
        message_type: str = "private",
        group_id: Optional[str] = None,
        dialogue_key: Optional[str] = None,
    ):
        task = self.background_coordinator.flush_conversation_session(
            user_id=user_id,
            message_type=message_type,
            group_id=group_id,
            dialogue_key=dialogue_key,
        )
        self._sync_background_task_metric()
        return task

    async def flush_background_tasks(self) -> None:
        await self.background_coordinator.flush()
        self._sync_background_task_metric()

    async def add_important_memory(
        self,
        user_id: str,
        content: str,
        source: str = "manual",
        priority: int = 1,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[ImportantMemoryItem]:
        normalized_metadata = self.access_policy.normalize_memory_record(
            content=content,
            owner_user_id=str(user_id or ""),
            metadata=metadata,
            source=source,
        )
        memory = await self.important_memory_store.add_memory(
            user_id=user_id,
            content=content,
            source=source,
            priority=priority,
            metadata=normalized_metadata,
        )
        if memory:
            await self.person_fact_service.sync_user_facts(str(user_id))
            self._inc_memory_writes()
        return memory

    async def get_important_memories(
        self,
        user_id: str,
        min_priority: int = 1,
        limit: int = 10,
        read_scope: Optional[str] = None,
        access_context: Optional[MemoryAccessContext] = None,
    ) -> List[ImportantMemoryItem]:
        return await self.retrieval_coordinator.get_important_memories(
            user_id=user_id,
            min_priority=min_priority,
            limit=limit,
            read_scope=read_scope,
            access_context=access_context,
        )

    async def format_important_memories_for_prompt(
        self,
        user_id: str,
        limit: int = 5,
        read_scope: Optional[str] = None,
        access_context: Optional[MemoryAccessContext] = None,
    ) -> str:
        return await self.retrieval_coordinator.format_important_memories_for_prompt(
            user_id=user_id,
            limit=limit,
            read_scope=read_scope,
            access_context=access_context,
        )

    async def delete_important_memory(self, user_id: str, content_substring: str) -> bool:
        result = await self.important_memory_store.delete_memory(user_id, content_substring)
        if result:
            await self.person_fact_service.sync_user_facts(str(user_id))
        return result

    async def update_important_memory(self, user_id: str, memory_id: str, content: str) -> bool:
        result = await self.important_memory_store.update_memory(user_id, memory_id, content)
        if result:
            await self.person_fact_service.sync_user_facts(str(user_id))
            self._inc_memory_writes()
        return result

    async def delete_important_memory_by_id(self, user_id: str, memory_id: str) -> bool:
        result = await self.important_memory_store.delete_memory_by_id(user_id, memory_id)
        if result:
            await self.person_fact_service.sync_user_facts(str(user_id))
            self._inc_memory_writes()
        return result

    async def clear_important_memories(self, user_id: str) -> bool:
        result = await self.important_memory_store.clear_memories(user_id)
        if result:
            await self.person_fact_store.clear_facts(str(user_id))
        return result

    async def close(self):
        await self.background_coordinator.close()
        self._sync_background_task_metric()
        if self.retriever:
            await self.retriever.close()
        logger.debug("记忆管理器已关闭")

    def get_stats(self) -> Dict[str, Any]:
        self._sync_background_task_metric()
        stats = {
            "storage_path": str(self.storage.base_path),
            "indices_built": len(self.index_coordinator.index_built),
            "indices_dirty": len(self.index_coordinator.index_dirty),
            "extractor_enabled": self.extractor is not None,
            "background_tasks": self.task_manager.count(),
        }
        if self.runtime_metrics:
            snapshot = self.runtime_metrics.snapshot()
            stats.update(
                {
                    "memory_reads": snapshot.get("memory_reads", 0),
                    "memory_shared_reads": snapshot.get("memory_shared_reads", 0),
                    "memory_scene_rule_hits": snapshot.get("memory_scene_rule_hits", 0),
                    "memory_access_denied": snapshot.get("memory_access_denied", 0),
                    "memory_writes": snapshot.get("memory_writes", 0),
                    "memory_migrations": snapshot.get("memory_migrations", 0),
                    "memory_compactions": snapshot.get("memory_compactions", 0),
                }
            )
        return stats

    async def _migrate_existing_memories(self) -> int:
        changed = 0
        for user_id in self.storage.get_user_ids():
            memories = await self.storage.get_user_memories(user_id)
            rewritten = False
            for memory in memories:
                normalized = self.access_policy.normalize_memory_record(
                    content=memory.content,
                    owner_user_id=str(user_id),
                    metadata=self._merge_tags_into_metadata(memory.tags, memory.metadata),
                    source=memory.source,
                )
                if normalized != (memory.metadata or {}):
                    memory.metadata = normalized
                    memory.owner_user_id = str(user_id)
                    rewritten = True
                    changed += 1
            if rewritten:
                await self.storage.replace_user_memories(user_id, memories)

        for user_id in self.important_memory_store.get_user_ids():
            memories = await self.important_memory_store.get_memories(user_id, min_priority=1)
            rewritten = False
            for memory in memories:
                normalized = self.access_policy.normalize_memory_record(
                    content=memory.content,
                    owner_user_id=str(user_id),
                    metadata=memory.metadata,
                    source=memory.source,
                )
                if normalized != (memory.metadata or {}):
                    memory.metadata = normalized
                    memory.owner_user_id = str(user_id)
                    rewritten = True
                    changed += 1
            if rewritten:
                await self.important_memory_store.replace_memories(user_id, memories)
        return changed

    async def _compact_existing_memories(self) -> int:
        changed = 0
        for user_id in self.storage.get_user_ids():
            memories = await self.storage.get_user_memories(user_id)
            compacted = self._compact_memory_items(memories)
            if len(compacted) != len(memories):
                await self.storage.replace_user_memories(user_id, compacted)
                changed += len(memories) - len(compacted)

        for user_id in self.important_memory_store.get_user_ids():
            memories = await self.important_memory_store.get_memories(user_id, min_priority=1)
            compacted = self._compact_important_items(memories)
            if len(compacted) != len(memories):
                await self.important_memory_store.replace_memories(user_id, compacted)
                changed += len(memories) - len(compacted)
        return changed

    async def _sync_existing_person_facts(self) -> None:
        user_ids = set(self.important_memory_store.get_user_ids()) | set(self.person_fact_store.get_user_ids())
        for user_id in sorted(uid for uid in user_ids if uid):
            await self.person_fact_service.sync_user_facts(str(user_id))

    def _compact_memory_items(self, memories: List[MemoryItem]) -> List[MemoryItem]:
        seen: Dict[str, MemoryItem] = {}
        for memory in memories:
            key = self._memory_compaction_key(memory.content, memory.metadata)
            existing = seen.get(key)
            if existing is None:
                seen[key] = memory
                continue
            if len(str(memory.content or "")) < len(str(existing.content or "")):
                existing.content = memory.content
            existing.updated_at = max(existing.updated_at, memory.updated_at)
            existing.tags = sorted(set((existing.tags or []) + (memory.tags or [])))
        return list(seen.values())

    def _compact_important_items(self, memories: List[ImportantMemoryItem]) -> List[ImportantMemoryItem]:
        seen: Dict[str, ImportantMemoryItem] = {}
        for memory in memories:
            key = self._memory_compaction_key(memory.content, memory.metadata)
            existing = seen.get(key)
            if existing is None:
                seen[key] = memory
                continue
            existing.priority = max(existing.priority, memory.priority)
            if len(str(memory.content or "")) < len(str(existing.content or "")):
                existing.content = memory.content
            existing.metadata.update(memory.metadata or {})
        result = list(seen.values())
        result.sort(key=lambda item: (item.priority, item.created_at), reverse=True)
        return result

    def _memory_compaction_key(self, content: str, metadata: Optional[Dict[str, Any]]) -> str:
        normalized_content = re.sub(r"\s+", " ", str(content or "").strip().lower())
        prepared = self.access_policy.normalize_memory_record(content="", metadata=metadata)
        category = prepared.get("content_category", "unknown")
        scope = prepared.get("applicability_scope", {})
        return f"{category}|{scope}|{normalized_content}"

    def _merge_tags_into_metadata(
        self,
        tags: Optional[List[str]],
        metadata: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        merged = dict(metadata or {})
        if tags and "tags" not in merged:
            merged["tags"] = list(tags)
        return merged

    def _inc_memory_writes(self, count: int = 1) -> None:
        if self.runtime_metrics:
            self.runtime_metrics.inc_memory_write(count)

    def _inc_memory_migrations(self, count: int = 1) -> None:
        if self.runtime_metrics:
            self.runtime_metrics.inc_memory_migration(count)

    def _inc_memory_compactions(self, count: int = 1) -> None:
        if self.runtime_metrics:
            self.runtime_metrics.inc_memory_compaction(count)

    def _sync_background_task_metric(self) -> None:
        if self.runtime_metrics:
            self.runtime_metrics.set_state(background_tasks=self.task_manager.count())
