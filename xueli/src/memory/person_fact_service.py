from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.memory.internal.access_policy import MemoryAccessContext, MemoryAccessPolicy, MemoryContentCategory
from src.memory.storage.important_memory_store import ImportantMemoryItem, ImportantMemoryStore
from src.memory.storage.person_fact_store import PersonFactItem, PersonFactStore


@dataclass
class PersonFactService:
    store: PersonFactStore
    important_memory_store: ImportantMemoryStore
    access_policy: MemoryAccessPolicy
    prompt_limit: int = 6

    async def sync_user_facts(self, user_id: str) -> List[PersonFactItem]:
        important_memories = await self.important_memory_store.get_memories(str(user_id), min_priority=1)
        generated = self._build_facts_from_important_memories(str(user_id), important_memories)
        existing = await self.store.get_facts(str(user_id))
        if self._facts_equal(existing, generated):
            return existing
        await self.store.replace_facts(str(user_id), generated)
        return generated

    async def get_prompt_entries(
        self,
        *,
        user_id: str,
        access_context: Optional[MemoryAccessContext] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        facts = await self.sync_user_facts(str(user_id))
        context = access_context or self.access_policy.build_context(requester_user_id=str(user_id))
        entries: List[Dict[str, Any]] = []
        for item in facts:
            metadata = dict(item.metadata or {})
            if not self.access_policy.is_accessible(
                owner_user_id=str(item.owner_user_id or user_id),
                metadata=metadata,
                context=context,
            ):
                continue
            entries.append(
                {
                    "content": item.content,
                    "memory_owner": str(item.owner_user_id or user_id),
                    "owner_user_id": str(item.owner_user_id or user_id),
                    "source": item.source,
                    "metadata": metadata,
                    "fact_kind": item.fact_kind,
                }
            )
            if len(entries) >= int(limit or self.prompt_limit):
                break
        return entries

    async def format_facts_for_prompt(
        self,
        *,
        user_id: str,
        access_context: Optional[MemoryAccessContext] = None,
        limit: Optional[int] = None,
    ) -> str:
        entries = await self.get_prompt_entries(user_id=user_id, access_context=access_context, limit=limit)
        return "\n".join(f"{index}. {entry['content']}" for index, entry in enumerate(entries, start=1))

    def _build_facts_from_important_memories(
        self,
        user_id: str,
        important_memories: List[ImportantMemoryItem],
    ) -> List[PersonFactItem]:
        facts: List[PersonFactItem] = []
        seen: set[tuple[str, str]] = set()
        for memory in important_memories:
            if not self._should_use_as_fact(memory):
                continue
            fact_kind = self._infer_fact_kind(memory)
            content = str(memory.content or "").strip()
            dedupe_key = (fact_kind, self.store._normalize_text(content))
            if not dedupe_key[1] or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            metadata = self.access_policy.normalize_memory_record(
                content=content,
                owner_user_id=user_id,
                metadata=memory.metadata,
                source=memory.source,
            )
            facts.append(
                PersonFactItem(
                    id=self.store.make_fact_id(user_id, fact_kind, content),
                    content=content,
                    fact_kind=fact_kind,
                    created_at=str(memory.created_at or ""),
                    updated_at=str(memory.updated_at or memory.created_at or ""),
                    source=str(memory.source or "important_memory"),
                    owner_user_id=user_id,
                    source_memory_id=str(memory.id or ""),
                    metadata={
                        **metadata,
                        "fact_kind": fact_kind,
                        "memory_type": "person_fact",
                        "decay_exempt": True,
                    },
                )
            )
        facts.sort(key=lambda item: (item.updated_at, item.created_at, item.content), reverse=True)
        return facts

    def _should_use_as_fact(self, memory: ImportantMemoryItem) -> bool:
        content = str(memory.content or "").strip()
        if len(content) < 3:
            return False
        patch_status = str(dict(memory.metadata or {}).get("patch_status") or "").strip().lower()
        if patch_status in {"superseded", "contextualized"}:
            return False
        metadata = self.access_policy.normalize_memory_record(
            content=content,
            owner_user_id=str(memory.owner_user_id or ""),
            metadata=memory.metadata,
            source=memory.source,
        )
        category = str(metadata.get("content_category") or "")
        if category in {
            MemoryContentCategory.GROUP_RULE.value,
            MemoryContentCategory.BOT_RULE.value,
            MemoryContentCategory.PUBLIC_RULE.value,
            MemoryContentCategory.ADDRESSING_PREFERENCE.value,
        }:
            return False
        return not self.access_policy.is_shared(metadata)

    def _infer_fact_kind(self, memory: ImportantMemoryItem) -> str:
        metadata = self.access_policy.normalize_memory_record(
            content=str(memory.content or ""),
            owner_user_id=str(memory.owner_user_id or ""),
            metadata=memory.metadata,
            source=memory.source,
        )
        category = str(metadata.get("content_category") or "")
        mapping = {
            MemoryContentCategory.PERSONAL_PREFERENCE.value: "preference",
            MemoryContentCategory.PERSONAL_BOUNDARY.value: "boundary",
            MemoryContentCategory.PLAN.value: "plan",
            MemoryContentCategory.BACKGROUND.value: "background",
            MemoryContentCategory.PERSONAL_INFO.value: "profile",
        }
        if category in mapping:
            return mapping[category]
        content = str(memory.content or "")
        if any(token in content for token in ["喜欢", "偏好", "爱吃", "习惯"]):
            return "preference"
        if any(token in content for token in ["不要", "别", "不想", "讨厌", "忌讳"]):
            return "boundary"
        if any(token in content for token in ["打算", "准备", "计划", "目标"]):
            return "plan"
        return "profile"

    def _facts_equal(self, left: List[PersonFactItem], right: List[PersonFactItem]) -> bool:
        return [item.to_dict() for item in left] == [item.to_dict() for item in right]
