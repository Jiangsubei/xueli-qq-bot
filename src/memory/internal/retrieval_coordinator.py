from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from src.memory.retrieval.bm25_index import SearchResult
from src.memory.retrieval.two_stage_retriever import TwoStageRetriever
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
        runtime_metrics: Optional[Any] = None,
        prompt_budgets: Optional[Dict[str, int]] = None,
    ) -> None:
        self.storage = storage
        self.important_memory_store = important_memory_store
        self.conversation_store = conversation_store
        self.retriever = retriever
        self.index_coordinator = index_coordinator
        self.memory_read_scope = memory_read_scope
        self.access_policy = access_policy
        self.runtime_metrics = runtime_metrics
        self.prompt_budgets = {
            "user_important": 360,
            "addressing": 180,
            "shared": 260,
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
        if result.rerank_score is not None:
            return float(result.rerank_score)
        return float(result.bm25_score or 0.0)

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
                )
            )

        filtered: List[SearchResult] = []
        denied = 0
        for result in aggregated:
            memory = getattr(result, "memory", None)
            if not memory:
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
        read_scope: Optional[str] = None,
        access_context: Optional[MemoryAccessContext] = None,
    ) -> Dict[str, Any]:
        prompt_context = await self.build_prompt_context(
            user_id=user_id,
            query=query,
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
        }

    async def build_prompt_context(
        self,
        *,
        user_id: str,
        query: str,
        read_scope: Optional[str] = None,
        access_context: Optional[MemoryAccessContext] = None,
    ) -> Dict[str, Any]:
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
        dynamic_memories = self._build_dynamic_memory_entries(
            query=query,
            user_id=str(user_id),
            important_memories=important_memories,
            ordinary_memories=ordinary_memories,
        )[: max(1, int(self.prompt_budgets["dynamic"] / 80))]

        user_important = self.access_policy.dedupe_entries(user_important)
        addressing = self.access_policy.dedupe_entries(addressing)
        shared_memories = self.access_policy.dedupe_entries(shared_memories)
        dynamic_memories = self.access_policy.dedupe_entries(dynamic_memories)

        sections = {
            "user_important": self._trim_entries(user_important, self.prompt_budgets["user_important"]),
            "addressing": self._trim_entries(addressing, self.prompt_budgets["addressing"]),
            "shared": self._trim_entries(shared_memories, self.prompt_budgets["shared"]),
            "dynamic": self._trim_entries(dynamic_memories, self.prompt_budgets["dynamic"]),
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
            "dynamic_memories": sections["dynamic"],
        }

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
    ) -> List[SearchResult]:
        await self.index_coordinator.ensure_fresh(owner_user_id)

        results = await self.retriever.retrieve(
            user_id=owner_user_id,
            query=query,
            top_k=top_k,
        )
        for result in results:
            if result.memory:
                result.memory.owner_user_id = owner_user_id
        return results

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
                if self.access_policy.is_accessible(
                    owner_user_id=memory.owner_user_id,
                    metadata=memory.metadata,
                    context=context,
                ):
                    accessible.append(memory)
                else:
                    denied += 1

        for memory in await self.storage.get_global_memories():
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

    def _build_dynamic_memory_entries(
        self,
        *,
        query: str,
        user_id: str,
        important_memories: List[ImportantMemoryItem],
        ordinary_memories: List[MemoryItem],
    ) -> List[Dict[str, Any]]:
        scored: List[Tuple[float, Dict[str, Any]]] = []
        for memory in ordinary_memories:
            score = self._score_text(query, memory.content)
            if score <= 0:
                continue
            if self.access_policy.is_addressing(memory.metadata):
                continue
            entry = self._memory_to_prompt_entry(memory, section="dynamic", requester_user_id=user_id)
            entry["score"] = score
            scored.append((score, entry))

        for memory in important_memories:
            score = self._score_text(query, memory.content)
            if score <= 0.45:
                continue
            if self.access_policy.is_addressing(memory.metadata):
                continue
            entry = self._memory_to_prompt_entry(memory, section="dynamic", requester_user_id=user_id)
            entry["score"] = max(score, 0.8)
            scored.append((entry["score"], entry))

        scored.sort(key=lambda item: (item[0], item[1].get("content", "")), reverse=True)
        return [entry for _, entry in scored]

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
            logger.warning("Failed to load anchored session context: %s", exc)
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
        session_id = str(prepared.get("source_session_id") or "").strip()
        if not session_id:
            return None
        try:
            turn_start = int(prepared.get("source_turn_start", 0) or 0)
            turn_end = int(prepared.get("source_turn_end", 0) or 0)
        except (TypeError, ValueError):
            return None
        if turn_start <= 0 or turn_end <= 0 or turn_start > turn_end:
            return None
        return {
            "session_id": session_id,
            "turn_start": turn_start,
            "turn_end": turn_end,
        }

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
