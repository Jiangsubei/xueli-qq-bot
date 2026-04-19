from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from src.memory.conversation_recall_service import ConversationRecallService
from src.memory.retrieval.bm25_index import ChineseTokenizer, SearchResult
from src.memory.retrieval.two_stage_retriever import RetrievalContext, TwoStageRetriever
from src.memory.session_restore_service import SessionRestoreService
from src.memory.storage.conversation_store import ConversationStore
from src.memory.storage.important_memory_store import ImportantMemoryItem, ImportantMemoryStore
from src.memory.storage.markdown_store import MemoryItem, MarkdownMemoryStore

from .access_policy import MemoryAccessContext, MemoryAccessPolicy
from .index_coordinator import MemoryIndexCoordinator

logger = logging.getLogger(__name__)


class MemoryRetrievalCoordinator:
    """Handle memory scope, retrieval, and prompt-context assembly."""

    def __init__(
        self,
        *,
        storage: MarkdownMemoryStore,
        important_memory_store: ImportantMemoryStore,
        conversation_store: ConversationStore,
        retriever: TwoStageRetriever,
        index_coordinator: MemoryIndexCoordinator,
        memory_read_scope: str,
        access_policy: MemoryAccessPolicy,
        session_restore_service: Optional[SessionRestoreService] = None,
        conversation_recall_service: Optional[ConversationRecallService] = None,
        runtime_metrics: Optional[Any] = None,
        prompt_budgets: Optional[Dict[str, int]] = None,
    ) -> None:
        self.storage = storage
        self.important_memory_store = important_memory_store
        self.conversation_store = conversation_store
        self.retriever = retriever
        self.index_coordinator = index_coordinator
        self.session_restore_service = session_restore_service
        self.conversation_recall_service = conversation_recall_service
        self.memory_read_scope = memory_read_scope
        self.access_policy = access_policy
        self.runtime_metrics = runtime_metrics
        self.prompt_budgets = {
            "user_important": 360,
            "addressing": 180,
            "shared": 260,
            "session_restore": 260,
            "precise_recall": 260,
            "dynamic": 420,
            **(prompt_budgets or {}),
        }

    def normalize_read_scope(self, read_scope: Optional[str] = None) -> str:
        value = read_scope if read_scope is not None else self.memory_read_scope
        return self.access_policy.normalize_read_scope(value)

    def get_scope_user_ids(self, user_id: str, read_scope: Optional[str] = None) -> List[str]:
        normalized_scope = self.normalize_read_scope(read_scope)
        if normalized_scope == "user":
            return [str(user_id)]

        user_ids = {str(user_id)}
        user_ids.update(self.storage.get_user_ids())
        user_ids.update(self.important_memory_store.get_user_ids())
        return sorted(uid for uid in user_ids if uid)

    def get_search_result_score(self, result: SearchResult) -> float:
        if result.combined_score is not None:
            return float(result.combined_score)
        if result.local_score is not None:
            return float(result.local_score)
        if result.rerank_score is not None:
            return float(result.rerank_score)
        return float(result.bm25_score or 0.0)

    def _memory_patch_status(self, metadata: Optional[Dict[str, Any]]) -> str:
        return str(dict(metadata or {}).get("patch_status") or "").strip().lower()

    def _should_skip_memory(self, metadata: Optional[Dict[str, Any]]) -> bool:
        return self._memory_patch_status(metadata) in {"superseded", "contextualized"}

    async def search_memories(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        use_rerank: Optional[bool] = None,
        read_scope: Optional[str] = None,
        access_context: Optional[MemoryAccessContext] = None,
    ) -> List[SearchResult]:
        context = self._resolve_access_context(
            user_id=user_id,
            read_scope=read_scope,
            access_context=access_context,
        )
        normalized_scope = context.read_scope
        scope_user_ids = self.get_scope_user_ids(user_id, normalized_scope)

        aggregated: List[SearchResult] = []
        per_user_top_k = max(top_k, 1)
        for owner_user_id in scope_user_ids:
            aggregated.extend(
                await self._search_memories_for_owner(
                    owner_user_id=owner_user_id,
                    query=query,
                    top_k=per_user_top_k,
                    use_rerank=use_rerank,
                    access_context=context,
                )
            )

        filtered: List[SearchResult] = []
        denied = 0
        for result in aggregated:
            memory = getattr(result, "memory", None)
            if not memory:
                continue
            if self._should_skip_memory(getattr(memory, "metadata", {})):
                continue
            if self.access_policy.is_accessible(
                owner_user_id=str(memory.owner_user_id or user_id),
                metadata=memory.metadata,
                context=context,
            ):
                filtered.append(result)
            else:
                denied += 1
        if denied:
            self._inc_memory_access_denied(denied)

        filtered.sort(
            key=lambda result: (
                self.get_search_result_score(result),
                getattr(result.memory, "updated_at", ""),
            ),
            reverse=True,
        )
        shared_hits = sum(
            1 for item in filtered[:top_k] if self.access_policy.is_shared(getattr(item.memory, "metadata", {}))
        )
        self._inc_memory_read(shared=shared_hits > 0)
        return filtered[:top_k]

    async def quick_check_relevance(
        self,
        user_id: str,
        query: str,
        threshold: float = 0.5,
    ) -> Optional[MemoryItem]:
        await self.index_coordinator.ensure_fresh(user_id)
        return await self.retriever.quick_check(user_id, query, threshold)

    async def search_memories_with_context(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        include_conversations: bool = True,
        include_sections: Optional[Dict[str, bool]] = None,
        section_intensity: Optional[Dict[str, str]] = None,
        read_scope: Optional[str] = None,
        access_context: Optional[MemoryAccessContext] = None,
    ) -> Dict[str, Any]:
        prompt_context = await self.build_prompt_context(
            user_id=user_id,
            query=query,
            include_sections=include_sections,
            section_intensity=section_intensity,
            read_scope=read_scope,
            access_context=access_context,
        )

        history_messages = prompt_context.get("history_messages", [])
        if not include_conversations:
            history_messages = []

        return {
            "memories": prompt_context.get("dynamic_memories", []),
            "conversations": [],
            "context_text": prompt_context.get("prompt_text", ""),
            "history_messages": history_messages,
            "prompt_sections": prompt_context.get("sections", {}),
            "session_restore": prompt_context.get("session_restore", []),
            "precise_recall": prompt_context.get("precise_recall", []),
        }

    async def build_prompt_context(
        self,
        *,
        user_id: str,
        query: str,
        include_sections: Optional[Dict[str, bool]] = None,
        section_intensity: Optional[Dict[str, str]] = None,
        read_scope: Optional[str] = None,
        access_context: Optional[MemoryAccessContext] = None,
    ) -> Dict[str, Any]:
        section_policy = {
            "session_restore": True,
            "precise_recall": True,
            "dynamic": True,
            **(include_sections or {}),
        }
        section_intensity = {
            "session_restore": "normal",
            "precise_recall": "normal",
            "dynamic": "normal",
            **(section_intensity or {}),
        }
        context = self._resolve_access_context(
            user_id=user_id,
            read_scope=read_scope,
            access_context=access_context,
        )
        important_memories = await self.get_important_memories(
            user_id=user_id,
            min_priority=1,
            limit=12,
            read_scope=context.read_scope,
            access_context=context,
        )
        ordinary_memories = await self._load_accessible_ordinary_memories(context)
        session_restore = []
        if section_policy.get("session_restore", True):
            session_restore = await self._build_session_restore_entries(
                user_id=str(user_id),
                context=context,
            )
        precise_recall = []
        if section_policy.get("precise_recall", True):
            precise_recall = await self._build_precise_recall_entries(
                user_id=str(user_id),
                query=query,
                context=context,
            )

        user_important = [
            self._memory_to_prompt_entry(memory, section="user_important", requester_user_id=str(user_id))
            for memory in important_memories
            if self.access_policy.classify_for_prompt(memory.metadata, memory.owner_user_id, str(user_id)) == "private"
        ]
        addressing = [
            self._memory_to_prompt_entry(memory, section="addressing", requester_user_id=str(user_id))
            for memory in [*important_memories, *ordinary_memories]
            if self.access_policy.classify_for_prompt(
                getattr(memory, "metadata", {}), getattr(memory, "owner_user_id", ""), str(user_id)
            )
            == "addressing"
        ]
        shared_memories = [
            self._memory_to_prompt_entry(memory, section="shared", requester_user_id=str(user_id))
            for memory in [*important_memories, *ordinary_memories]
            if self.access_policy.classify_for_prompt(
                getattr(memory, "metadata", {}), getattr(memory, "owner_user_id", ""), str(user_id)
            )
            == "shared"
        ]
        dynamic_memories: List[Dict[str, Any]] = []
        if section_policy.get("dynamic", True):
            dynamic_memories = (
                await self._build_dynamic_memory_entries(
                    query=query,
                    user_id=str(user_id),
                    context=context,
                    important_memories=important_memories,
                    ordinary_memories=ordinary_memories,
                )
            )[: max(1, int(self.prompt_budgets["dynamic"] / 80))]

        user_important = self.access_policy.dedupe_entries(user_important)
        addressing = self.access_policy.dedupe_entries(addressing)
        shared_memories = self.access_policy.dedupe_entries(shared_memories)
        session_restore = self.access_policy.dedupe_entries(session_restore)
        precise_recall = self.access_policy.dedupe_entries(precise_recall)
        dynamic_memories = self.access_policy.dedupe_entries(dynamic_memories)

        sections = {
            "user_important": self._trim_entries(user_important, self.prompt_budgets["user_important"]),
            "addressing": self._trim_entries(addressing, self.prompt_budgets["addressing"]),
            "shared": self._trim_entries(shared_memories, self.prompt_budgets["shared"]),
            "session_restore": self._trim_entries(session_restore, self._budget_for_intensity("session_restore", section_intensity["session_restore"])),
            "precise_recall": self._trim_entries(precise_recall, self._budget_for_intensity("precise_recall", section_intensity["precise_recall"])),
            "dynamic": self._trim_entries(dynamic_memories, self._budget_for_intensity("dynamic", section_intensity["dynamic"])),
        }

        scene_hits = len(sections["addressing"]) + len(sections["shared"])
        if scene_hits:
            self._inc_memory_scene_rule_hits(scene_hits)
        self._inc_memory_read(shared=bool(sections["shared"]))

        history_messages = await self._load_history_messages(dynamic_memories=sections["dynamic"])
        prompt_text = self._build_prompt_text(sections)
        return {
            "sections": sections,
            "prompt_text": prompt_text,
            "history_messages": history_messages,
            "session_restore": sections["session_restore"],
            "precise_recall": sections["precise_recall"],
            "dynamic_memories": sections["dynamic"],
        }

    def _budget_for_intensity(self, section: str, intensity: str) -> int:
        base = int(self.prompt_budgets.get(section, 0) or 0)
        level = str(intensity or "normal").strip().lower()
        multiplier = {
            "off": 0.0,
            "light": 0.6,
            "normal": 1.0,
            "high": 1.4,
        }.get(level, 1.0)
        return max(0, int(base * multiplier))

    async def _build_session_restore_entries(
        self,
        *,
        user_id: str,
        context: MemoryAccessContext,
    ) -> List[Dict[str, Any]]:
        if not self.session_restore_service:
            return []
        return await self.session_restore_service.build_restore_entries(
            user_id=user_id,
            message_type=context.message_type,
            group_id=context.group_id,
        )

    async def _build_precise_recall_entries(
        self,
        *,
        user_id: str,
        query: str,
        context: MemoryAccessContext,
    ) -> List[Dict[str, Any]]:
        if not self.conversation_recall_service:
            return []
        return await self.conversation_recall_service.build_recall_entries(
            user_id=user_id,
            query=query,
            message_type=context.message_type,
            group_id=context.group_id,
        )

    async def search_important_memories(
        self,
        user_id: str,
        query: str,
        limit: int = 5,
        read_scope: Optional[str] = None,
        access_context: Optional[MemoryAccessContext] = None,
    ) -> List[Dict[str, Any]]:
        context = self._resolve_access_context(
            user_id=user_id,
            read_scope=read_scope,
            access_context=access_context,
        )
        matched_memories: List[Dict[str, Any]] = []
        denied = 0

        for owner_user_id in self.get_scope_user_ids(user_id, context.read_scope):
            matched = await self.important_memory_store.search_memories(
                user_id=owner_user_id,
                query=query,
                top_k=limit,
            )
            for memory in matched:
                if self._should_skip_memory(memory.metadata):
                    continue
                if not self.access_policy.is_accessible(
                    owner_user_id=str(memory.owner_user_id or owner_user_id),
                    metadata=memory.metadata,
                    context=context,
                ):
                    denied += 1
                    continue
                matched_memories.append(
                    {
                        "content": memory.content,
                        "source": memory.source,
                        "priority": memory.priority,
                        "score": memory.score,
                        "memory_type": "important",
                        "memory_owner": str(memory.owner_user_id or owner_user_id),
                        "metadata": dict(memory.metadata or {}),
                    }
                )

        if denied:
            self._inc_memory_access_denied(denied)
        matched_memories.sort(
            key=lambda item: (
                item.get("score", 0.0),
                item.get("priority", 0),
                item.get("content", ""),
            ),
            reverse=True,
        )
        self._inc_memory_read(shared=any(self.access_policy.is_shared(item.get("metadata")) for item in matched_memories))
        return matched_memories[:limit]

    async def get_important_memories(
        self,
        user_id: str,
        min_priority: int = 1,
        limit: int = 10,
        read_scope: Optional[str] = None,
        access_context: Optional[MemoryAccessContext] = None,
    ) -> List[ImportantMemoryItem]:
        context = self._resolve_access_context(
            user_id=user_id,
            read_scope=read_scope,
            access_context=access_context,
        )
        memories: List[ImportantMemoryItem] = []
        denied = 0

        for owner_user_id in self.get_scope_user_ids(user_id, context.read_scope):
            owner_memories = await self.important_memory_store.get_memories(
                user_id=owner_user_id,
                min_priority=min_priority,
            )
            for memory in owner_memories:
                owner = str(memory.owner_user_id or owner_user_id)
                if self._should_skip_memory(memory.metadata):
                    continue
                if self.access_policy.is_accessible(
                    owner_user_id=owner,
                    metadata=memory.metadata,
                    context=context,
                ):
                    memory.owner_user_id = owner
                    memories.append(memory)
                else:
                    denied += 1

        if denied:
            self._inc_memory_access_denied(denied)
        memories.sort(key=lambda item: (item.priority, item.created_at), reverse=True)
        self._inc_memory_read(shared=any(self.access_policy.is_shared(item.metadata) for item in memories))
        return memories[:limit]

    async def format_important_memories_for_prompt(
        self,
        user_id: str,
        limit: int = 5,
        read_scope: Optional[str] = None,
        access_context: Optional[MemoryAccessContext] = None,
    ) -> str:
        memories = await self.get_important_memories(
            user_id,
            limit=limit,
            read_scope=read_scope,
            access_context=access_context,
        )
        if not memories:
            return ""

        lines = ["=== 重要事实（请务必记住） ==="]
        for index, memory in enumerate(memories, 1):
            owner_user_id = getattr(memory, "owner_user_id", "")
            if owner_user_id and owner_user_id != str(user_id):
                lines.append(f"{index}. [来源用户 {owner_user_id}] {memory.content}")
            else:
                lines.append(f"{index}. {memory.content}")
        lines.append("")
        return "\n".join(lines)

    async def _search_memories_for_owner(
        self,
        owner_user_id: str,
        query: str,
        top_k: int = 5,
        use_rerank: Optional[bool] = None,
        access_context: Optional[MemoryAccessContext] = None,
    ) -> List[SearchResult]:
        del use_rerank
        await self.index_coordinator.ensure_fresh(owner_user_id)

        results = await self.retriever.retrieve(
            user_id=owner_user_id,
            query=query,
            top_k=top_k,
            retrieval_context=self._build_retrieval_context(owner_user_id=owner_user_id, access_context=access_context),
        )
        for result in results:
            if result.memory:
                result.memory.owner_user_id = owner_user_id
        return results

    def _build_retrieval_context(
        self,
        *,
        owner_user_id: str,
        access_context: Optional[MemoryAccessContext],
    ) -> RetrievalContext:
        context = self._resolve_access_context(
            user_id=owner_user_id,
            read_scope=access_context.read_scope if access_context else self.memory_read_scope,
            access_context=access_context,
        )
        return RetrievalContext(
            requester_user_id=context.requester_user_id,
            message_type=context.message_type,
            group_id=context.group_id,
            read_scope=context.read_scope,
        )

    def _resolve_access_context(
        self,
        *,
        user_id: str,
        read_scope: Optional[str] = None,
        access_context: Optional[MemoryAccessContext] = None,
    ) -> MemoryAccessContext:
        if access_context is not None:
            return self.access_policy.build_context(
                requester_user_id=access_context.requester_user_id or str(user_id),
                message_type=access_context.message_type,
                group_id=access_context.group_id,
                read_scope=read_scope or access_context.read_scope,
            )
        return self.access_policy.build_context(
            requester_user_id=str(user_id),
            read_scope=read_scope or self.memory_read_scope,
        )

    async def _load_accessible_ordinary_memories(
        self,
        context: MemoryAccessContext,
    ) -> List[MemoryItem]:
        accessible: List[MemoryItem] = []
        denied = 0
        for owner_user_id in self.get_scope_user_ids(context.requester_user_id, context.read_scope):
            owner_memories = await self.storage.get_user_memories(owner_user_id)
            for memory in owner_memories:
                memory.owner_user_id = str(memory.owner_user_id or owner_user_id)
                if self._should_skip_memory(memory.metadata):
                    continue
                if self.access_policy.is_accessible(
                    owner_user_id=memory.owner_user_id,
                    metadata=memory.metadata,
                    context=context,
                ):
                    accessible.append(memory)
                else:
                    denied += 1

        for memory in await self.storage.get_global_memories():
            if self._should_skip_memory(memory.metadata):
                continue
            if self.access_policy.is_accessible(
                owner_user_id=str(memory.owner_user_id or ""),
                metadata=memory.metadata,
                context=context,
            ):
                accessible.append(memory)
            else:
                denied += 1

        if denied:
            self._inc_memory_access_denied(denied)
        return accessible

    async def _build_dynamic_memory_entries(
        self,
        *,
        query: str,
        user_id: str,
        context: MemoryAccessContext,
        important_memories: List[ImportantMemoryItem],
        ordinary_memories: List[MemoryItem],
    ) -> List[Dict[str, Any]]:
        scored: List[Tuple[float, Dict[str, Any]]] = []
        seen_ids: set[str] = set()
        dynamic_limit = max(1, int(getattr(self.retriever.config, "dynamic_memory_limit", 8) or 8))

        retrieved = await self.search_memories(
            user_id=user_id,
            query=query,
            top_k=max(dynamic_limit * 2, 5),
            read_scope=context.read_scope,
            access_context=context,
        )
        for result in retrieved:
            memory = getattr(result, "memory", None)
            if memory is None:
                continue
            memory_id = str(getattr(memory, "id", "") or "")
            if memory_id and memory_id in seen_ids:
                continue
            if self._should_skip_dynamic_memory(memory, query=query):
                continue
            if self.access_policy.is_addressing(getattr(memory, "metadata", {})):
                continue
            entry = self._memory_to_prompt_entry(memory, section="dynamic", requester_user_id=user_id)
            score = self.get_search_result_score(result)
            entry["score"] = score
            entry["bm25_score"] = result.bm25_score
            entry["local_score"] = result.local_score
            entry["rerank_score"] = result.rerank_score
            entry["combined_score"] = result.combined_score
            entry["ranking_stage"] = result.ranking_stage
            scored.append((score, entry))
            if memory_id:
                seen_ids.add(memory_id)
            if len(scored) >= dynamic_limit:
                break

        if not scored:
            for memory in ordinary_memories:
                score = self._score_text(query, memory.content)
                if score <= 0:
                    continue
                if self._should_skip_dynamic_memory(memory, query=query):
                    continue
                if self.access_policy.is_addressing(memory.metadata):
                    continue
                entry = self._memory_to_prompt_entry(memory, section="dynamic", requester_user_id=user_id)
                entry["score"] = score
                scored.append((score, entry))
                if len(scored) >= dynamic_limit:
                    break

        scored.sort(
            key=lambda item: (
                self._dynamic_scene_bucket(item[1], context=context),
                item[0],
                item[1].get("content", ""),
            ),
            reverse=True,
        )
        return self._dedupe_dynamic_entries(scored, context=context, limit=dynamic_limit)

    def _should_skip_dynamic_memory(self, memory: Any, *, query: str) -> bool:
        metadata = dict(getattr(memory, "metadata", {}) or {})
        if self._memory_patch_status(metadata) in {"superseded", "contextualized"}:
            return True
        if str(metadata.get("memory_type", "") or "").strip().lower() == "important":
            return True
        content = str(getattr(memory, "content", "") or "").strip()
        if len(content) < 3:
            return True
        if self._score_text(query, content) < 0.12:
            return True
        return False

    def _dedupe_dynamic_entries(
        self,
        scored_entries: List[Tuple[float, Dict[str, Any]]],
        *,
        context: MemoryAccessContext,
        limit: int,
    ) -> List[Dict[str, Any]]:
        if not getattr(self.retriever.config, "dynamic_dedup_enabled", True):
            return [entry for _, entry in scored_entries[:limit]]

        accepted: List[Dict[str, Any]] = []
        for _, entry in scored_entries:
            if self._is_duplicate_dynamic_entry(entry, accepted):
                continue
            accepted.append(entry)
            if len(accepted) >= limit:
                break
        accepted.sort(
            key=lambda entry: (
                self._dynamic_scene_bucket(entry, context=context),
                float(entry.get("score", 0.0) or 0.0),
                entry.get("content", ""),
            ),
            reverse=True,
        )
        return accepted

    def _dynamic_scene_bucket(self, entry: Dict[str, Any], *, context: MemoryAccessContext) -> int:
        metadata = dict(entry.get("metadata") or {})
        source_message_type = str(metadata.get("source_message_type", "") or "").strip().lower()
        source_group_id = str(metadata.get("source_group_id", metadata.get("group_id", "")) or "").strip()
        owner_user_id = str(entry.get("owner_user_id") or entry.get("memory_owner") or metadata.get("owner_user_id") or "").strip()

        same_group = context.message_type == "group" and bool(context.group_id) and source_group_id == context.group_id
        same_message_type = bool(source_message_type) and source_message_type == context.message_type
        same_owner = bool(context.requester_user_id) and owner_user_id == context.requester_user_id

        if same_group and same_owner:
            return 4
        if same_group:
            return 3
        if same_message_type and same_owner:
            return 2
        if same_message_type:
            return 1
        return 0

    def _is_duplicate_dynamic_entry(
        self,
        entry: Dict[str, Any],
        accepted_entries: List[Dict[str, Any]],
    ) -> bool:
        threshold = float(getattr(self.retriever.config, "dynamic_dedup_similarity_threshold", 0.72) or 0.72)
        content = self._normalize_dynamic_memory_text(entry.get("content", ""))
        if not content:
            return False
        for accepted in accepted_entries:
            accepted_content = self._normalize_dynamic_memory_text(accepted.get("content", ""))
            if not accepted_content:
                continue
            if content == accepted_content:
                return True
            if content in accepted_content or accepted_content in content:
                return True
            if self._dynamic_text_similarity(content, accepted_content) >= threshold:
                return True
        return False

    def _normalize_dynamic_memory_text(self, text: Any) -> str:
        raw_text = str(text or "").lower()
        compact_raw = re.sub(r"\s+", " ", raw_text).strip()
        normalized = re.sub(r"\s+", "", raw_text)
        normalized = re.sub(r"[^\w\u4e00-\u9fff]", "", normalized)
        return normalized or compact_raw

    def _dynamic_text_similarity(self, left: str, right: str) -> float:
        if not left or not right:
            return 0.0
        left_tokens = set(ChineseTokenizer.tokenize(left))
        right_tokens = set(ChineseTokenizer.tokenize(right))
        token_score = self._overlap_coefficient(left_tokens, right_tokens)
        if token_score > 0:
            return token_score

        left_bigrams = self._to_character_bigrams(left)
        right_bigrams = self._to_character_bigrams(right)
        bigram_score = self._overlap_coefficient(left_bigrams, right_bigrams)
        if bigram_score > 0:
            return bigram_score

        if left == right:
            return 1.0
        if left in right or right in left:
            return min(len(left), len(right)) / max(len(left), len(right))
        return 0.0

    def _to_character_bigrams(self, text: str) -> set[str]:
        compact = re.sub(r"\s+", "", str(text or ""))
        if len(compact) < 2:
            return set()
        return {compact[index : index + 2] for index in range(len(compact) - 1)}

    def _overlap_coefficient(self, left: set[str], right: set[str]) -> float:
        if not left or not right:
            return 0.0
        return len(left & right) / min(len(left), len(right))

    def _memory_to_prompt_entry(
        self,
        memory: Any,
        *,
        section: str,
        requester_user_id: str,
    ) -> Dict[str, Any]:
        metadata = dict(getattr(memory, "metadata", {}) or {})
        content = str(metadata.get("summary") or getattr(memory, "content", "")).strip()
        owner_user_id = str(getattr(memory, "owner_user_id", "") or metadata.get("owner_user_id") or "")
        source = str(getattr(memory, "source", "") or metadata.get("source", "")).strip()
        label_owner = owner_user_id and owner_user_id != requester_user_id
        if section == "shared" and label_owner:
            content = f"[来源用户 {owner_user_id}] {content}"
        return {
            "content": content,
            "memory_owner": owner_user_id,
            "owner_user_id": owner_user_id,
            "source": source,
            "bm25_score": getattr(memory, "bm25_score", None),
            "local_score": getattr(memory, "local_score", None),
            "rerank_score": getattr(memory, "rerank_score", None),
            "combined_score": getattr(memory, "combined_score", None),
            "ranking_stage": getattr(memory, "ranking_stage", "storage"),
            "metadata": metadata,
        }

    def _trim_entries(self, entries: List[Dict[str, Any]], budget: int) -> List[Dict[str, Any]]:
        trimmed: List[Dict[str, Any]] = []
        used = 0
        for entry in entries:
            content = str(entry.get("content", "")).strip()
            if not content:
                continue
            length = len(content)
            if trimmed and used + length > budget:
                break
            trimmed.append(entry)
            used += length
        return trimmed

    def _build_prompt_text(self, sections: Dict[str, List[Dict[str, Any]]]) -> str:
        parts = []
        section_specs = [
            ("=== 当前用户重要记忆 ===", sections.get("user_important", [])),
            ("=== 当前场景称呼要求 ===", sections.get("addressing", [])),
            ("=== 当前场景共享规则 / 共享重要记忆 ===", sections.get("shared", [])),
            ("=== 最近相关会话恢复 ===", sections.get("session_restore", [])),
            ("=== 相关旧对话精准定位 ===", sections.get("precise_recall", [])),
            ("=== 动态相关普通记忆 ===", sections.get("dynamic", [])),
        ]
        for title, entries in section_specs:
            if not entries:
                continue
            parts.append(title)
            for index, entry in enumerate(entries, 1):
                parts.append(f"{index}. {entry.get('content', '')}")
            parts.append("")
        return "\n".join(parts).strip()

    async def _load_history_messages(self, *, dynamic_memories: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        if not self.conversation_store:
            return []

        anchor_entry = next(
            (
                entry
                for entry in dynamic_memories
                if self._extract_memory_anchor(entry.get("metadata"))
            ),
            None,
        )
        if anchor_entry is None:
            return []

        metadata = dict(anchor_entry.get("metadata") or {})
        anchor = self._extract_memory_anchor(metadata)
        if anchor is None:
            return []

        owner_user_id = str(anchor_entry.get("owner_user_id") or anchor_entry.get("memory_owner") or metadata.get("owner_user_id") or "").strip()
        if not owner_user_id:
            return []

        try:
            record = await self.conversation_store.load_session(owner_user_id, anchor["session_id"])
        except Exception as exc:
            logger.warning("加载记忆关联对话失败：%s", exc)
            return []
        if record is None:
            return []

        turn_map = {int(turn.turn_id): turn for turn in record.turns}
        start = max(1, anchor["turn_start"] - 3)
        end = anchor["turn_end"] + 3
        history_messages: List[Dict[str, str]] = []
        for turn_id in range(start, end + 1):
            turn = turn_map.get(turn_id)
            if turn is None:
                continue
            user_text = str(turn.user or "").strip()
            assistant_text = str(turn.assistant or "").strip()
            if user_text:
                history_messages.append({"role": "user", "content": user_text})
            if assistant_text:
                history_messages.append({"role": "assistant", "content": assistant_text})
        return history_messages

    def _extract_memory_anchor(self, metadata: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        prepared = dict(metadata or {})
        observations = prepared.get("source_observations")
        if isinstance(observations, list):
            for item in reversed(observations):
                anchor = self._normalize_memory_anchor(item)
                if anchor is not None:
                    return anchor
        return self._normalize_memory_anchor(
            {
                "session_id": prepared.get("source_session_id"),
                "turn_start": prepared.get("source_turn_start"),
                "turn_end": prepared.get("source_turn_end"),
            }
        )

    def _normalize_memory_anchor(self, payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        prepared = dict(payload or {})
        session_id = str(prepared.get("session_id") or "").strip()
        if not session_id:
            return None
        try:
            turn_start = int(prepared.get("turn_start", 0) or 0)
            turn_end = int(prepared.get("turn_end", 0) or 0)
        except (TypeError, ValueError):
            return None
        if turn_start <= 0 or turn_end <= 0 or turn_start > turn_end:
            return None
        return {"session_id": session_id, "turn_start": turn_start, "turn_end": turn_end}

    def _score_text(self, query: str, content: str) -> float:
        normalized_query = re.sub(r"\s+", "", str(query or "").lower())
        normalized_content = re.sub(r"\s+", "", str(content or "").lower())
        if not normalized_query or not normalized_content:
            return 0.0
        if normalized_query in normalized_content or normalized_content in normalized_query:
            return min(len(normalized_query), len(normalized_content)) / max(len(normalized_query), len(normalized_content))
        query_chars = set(normalized_query)
        content_chars = set(normalized_content)
        return len(query_chars & content_chars) / max(len(query_chars), 1)

    def _inc_memory_read(self, *, shared: bool = False) -> None:
        if self.runtime_metrics:
            self.runtime_metrics.inc_memory_read(shared=shared)

    def _inc_memory_access_denied(self, count: int) -> None:
        if self.runtime_metrics:
            self.runtime_metrics.inc_memory_access_denied(count)

    def _inc_memory_scene_rule_hits(self, count: int) -> None:
        if self.runtime_metrics:
            self.runtime_metrics.inc_memory_scene_rule_hits(count)

