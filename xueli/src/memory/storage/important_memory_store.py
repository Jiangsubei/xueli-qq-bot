"""
Important memory storage.

These memories are persisted in Markdown for transparent inspection while
keeping richer metadata in an inline JSON comment for migration and policy use.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha1
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiofiles

logger = logging.getLogger(__name__)


@dataclass
class ImportantMemoryItem:
    """A single important memory record."""

    content: str
    id: str = ""
    created_at: str = ""
    updated_at: str = ""
    source: str = ""
    priority: int = 1
    score: float = 0.0
    owner_user_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if not self.updated_at:
            self.updated_at = self.created_at
        if self.metadata is None:
            self.metadata = {}
        if not self.id:
            self.id = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source": self.source,
            "priority": self.priority,
            "score": self.score,
            "owner_user_id": self.owner_user_id,
            "metadata": dict(self.metadata or {}),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ImportantMemoryItem":
        return cls(
            id=data.get("id", ""),
            content=data.get("content", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            source=data.get("source", ""),
            priority=int(data.get("priority", 1) or 1),
            score=float(data.get("score", 0.0) or 0.0),
            owner_user_id=data.get("owner_user_id", ""),
            metadata=dict(data.get("metadata") or {}),
        )


class ImportantMemoryStore:
    """Store important memories as Markdown lines with JSON metadata."""

    def __init__(self, base_path: str = "memories/important"):
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        self._locks: Dict[str, asyncio.Lock] = {}

    def _get_user_file(self, user_id: str) -> Path:
        if user_id.startswith("group:"):
            group_part = user_id[6:]
            return self.base_path / "group" / f"{group_part}.md"
        return self.base_path / f"{user_id}.md"

    def _get_file_lock(self, file_path: str) -> asyncio.Lock:
        if file_path not in self._locks:
            self._locks[file_path] = asyncio.Lock()
        return self._locks[file_path]

    def get_user_ids(self) -> List[str]:
        user_ids = [file_path.stem for file_path in self.base_path.glob("*.md")]
        group_dir = self.base_path / "group"
        if group_dir.exists():
            for file_path in group_dir.glob("*.md"):
                user_ids.append(f"group:{file_path.stem}")
        return sorted(user_ids)

    def _normalize_text(self, text: str) -> str:
        normalized = (text or "").lower().strip()
        return re.sub(r"[^\w\u4e00-\u9fff]", "", normalized)

    def _is_same_memory(self, left: str, right: str) -> bool:
        normalized_left = self._normalize_text(left)
        normalized_right = self._normalize_text(right)

        if not normalized_left or not normalized_right:
            return False
        if normalized_left == normalized_right:
            return True

        shorter, longer = (
            (normalized_left, normalized_right)
            if len(normalized_left) <= len(normalized_right)
            else (normalized_right, normalized_left)
        )
        if len(shorter) < 4:
            return False
        return shorter in longer and (len(shorter) / max(len(longer), 1)) >= 0.75

    def _score_match(self, query: str, content: str) -> float:
        normalized_query = self._normalize_text(query)
        normalized_content = self._normalize_text(content)

        if not normalized_query or not normalized_content:
            return 0.0
        if normalized_query == normalized_content:
            return 1.0

        substring_score = 0.0
        if normalized_query in normalized_content or normalized_content in normalized_query:
            shorter = min(len(normalized_query), len(normalized_content))
            longer = max(len(normalized_query), len(normalized_content))
            substring_score = shorter / max(longer, 1)

        query_chars = set(normalized_query)
        content_chars = set(normalized_content)
        overlap_score = len(query_chars & content_chars) / max(len(content_chars), 1)

        return max(substring_score, overlap_score)

    def _build_payload(self, memory: ImportantMemoryItem) -> str:
        return json.dumps(
            {
                "id": memory.id,
                "source": memory.source,
                "updated_at": memory.updated_at,
                "metadata": dict(memory.metadata or {}),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _parse_payload(self, raw_comment: str) -> tuple[str, str, Dict[str, Any], str]:
        text = str(raw_comment or "").strip()
        if not text:
            return "", "unknown", {}, ""
        if text.startswith("{"):
            try:
                payload = json.loads(text)
                return (
                    str(payload.get("id") or ""),
                    str(payload.get("source") or "unknown"),
                    dict(payload.get("metadata") or {}),
                    str(payload.get("updated_at") or ""),
                )
            except json.JSONDecodeError:
                logger.debug("解析重要记忆负载 JSON 失败")
        source_match = re.search(r"source:(.*?)$", text)
        source = source_match.group(1).strip() if source_match else "unknown"
        return "", source, {}, ""

    def _ensure_memory_id(self, *, owner_user_id: str, content: str, created_at: str) -> str:
        base = f"{owner_user_id}|{created_at}|{content}".encode("utf-8", errors="ignore")
        return f"imp_{sha1(base).hexdigest()[:16]}"

    async def _read_memories(self, user_id: str) -> List[ImportantMemoryItem]:
        file_path = self._get_user_file(user_id)
        if not file_path.exists():
            return []

        memories: List[ImportantMemoryItem] = []
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as file:
                content = await file.read()

            for line in content.splitlines():
                line = line.strip()
                if not line.startswith("- "):
                    continue

                try:
                    timestamp_match = re.search(r"\[(.*?)\]", line)
                    priority_match = re.search(r"\[P(\d+)\]", line)
                    comment_match = re.search(r"<!--\s*(.+?)\s*-->$", line)

                    created_at = timestamp_match.group(1) if timestamp_match else datetime.now().isoformat()
                    priority = int(priority_match.group(1)) if priority_match else 1
                    content_start = line.find("]", line.find("[P")) + 1 if "[P" in line else 2
                    content_end = line.find("<!--") if "<!--" in line else len(line)
                    memory_content = line[content_start:content_end].strip()

                    memory_id = ""
                    source = "unknown"
                    metadata: Dict[str, Any] = {}
                    updated_at = created_at
                    if comment_match:
                        memory_id, source, metadata, updated_at = self._parse_payload(comment_match.group(1))
                    memory_id = memory_id or self._ensure_memory_id(
                        owner_user_id=user_id,
                        content=memory_content,
                        created_at=created_at,
                    )

                    if memory_content:
                        memories.append(
                            ImportantMemoryItem(
                                id=memory_id,
                                content=memory_content,
                                created_at=created_at,
                                updated_at=updated_at or created_at,
                                source=source,
                                priority=priority,
                                owner_user_id=user_id,
                                metadata=metadata,
                            )
                        )
                except Exception as exc:
                    logger.debug("[存储] 解析重要记忆行失败")

            return memories
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[存储] 读取重要记忆失败")
            return []

    async def _write_memories(self, user_id: str, memories: List[ImportantMemoryItem]) -> bool:
        file_path = self._get_user_file(user_id)
        lock = self._get_file_lock(str(file_path))
        async with lock:
            try:
                file_path.parent.mkdir(parents=True, exist_ok=True)
                lines = []
                for memory in memories:
                    display_time = memory.created_at
                    try:
                        display_time = datetime.fromisoformat(memory.created_at).strftime("%Y-%m-%d %H:%M")
                    except ValueError:
                        pass
                    lines.append(
                        f"- [{display_time}] [P{memory.priority}] {memory.content}<!-- {self._build_payload(memory)} -->"
                    )

                tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
                async with aiofiles.open(tmp_path, "w", encoding="utf-8") as file:
                    await file.write("\n".join(lines))
                await asyncio.to_thread(os.replace, str(tmp_path), str(file_path))
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[存储] 写入重要记忆失败")
                return False

    async def add_memory(
        self,
        user_id: str,
        content: str,
        source: str = "manual",
        priority: int = 1,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[ImportantMemoryItem]:
        if not content or not content.strip():
            return None

        normalized_content = content.strip()
        memories = await self._read_memories(user_id)

        for existing in memories:
            if self._is_same_memory(existing.content, normalized_content):
                existing.priority = max(existing.priority, int(priority))
                if len(normalized_content) > len(existing.content):
                    existing.content = normalized_content
                if source and (existing.source == "unknown" or not existing.source):
                    existing.source = source
                if metadata:
                    existing.metadata.update(dict(metadata))
                existing.updated_at = datetime.now().isoformat()

                success = await self._write_memories(user_id, memories)
                if success:
                    return existing
                return None

        now_iso = datetime.now().isoformat()
        memory = ImportantMemoryItem(
            id=self._ensure_memory_id(
                owner_user_id=user_id,
                content=normalized_content,
                created_at=now_iso,
            ),
            content=normalized_content,
            created_at=now_iso,
            source=source,
            priority=int(priority),
            owner_user_id=user_id,
            metadata=dict(metadata or {}),
        )
        memories.append(memory)
        memories.sort(key=lambda item: (item.priority, item.created_at), reverse=True)

        success = await self._write_memories(user_id, memories)
        return memory if success else None

    async def get_memories(
        self,
        user_id: str,
        min_priority: int = 1,
    ) -> List[ImportantMemoryItem]:
        memories = await self._read_memories(user_id)
        filtered = [memory for memory in memories if memory.priority >= min_priority]
        filtered.sort(key=lambda item: (item.priority, item.created_at), reverse=True)
        return filtered

    async def search_memories(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        min_priority: int = 1,
        min_score: float = 0.35,
    ) -> List[ImportantMemoryItem]:
        memories = await self.get_memories(user_id, min_priority=min_priority)
        matched: List[ImportantMemoryItem] = []

        for memory in memories:
            score = self._score_match(query, memory.content)
            if score >= min_score:
                matched.append(
                    ImportantMemoryItem(
                        id=memory.id,
                        content=memory.content,
                        created_at=memory.created_at,
                        updated_at=memory.updated_at,
                        source=memory.source,
                        priority=memory.priority,
                        score=score,
                        owner_user_id=user_id,
                        metadata=dict(memory.metadata or {}),
                    )
                )

        matched.sort(key=lambda item: (item.score, item.priority, item.created_at), reverse=True)
        return matched[:top_k]

    async def delete_memory(self, user_id: str, content_substring: str) -> bool:
        file_path = self._get_user_file(user_id)
        if not file_path.exists():
            return False

        try:
            memories = await self._read_memories(user_id)
            new_memories = [memory for memory in memories if content_substring not in memory.content]
            if len(new_memories) == len(memories):
                return False
            return await self._write_memories(user_id, new_memories)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[存储] 删除重要记忆失败")
            return False

    async def update_memory(self, user_id: str, memory_id: str, content: str) -> bool:
        normalized_content = str(content or "").strip()
        if not normalized_content:
            return False
        memories = await self._read_memories(user_id)
        for memory in memories:
            if memory.id != memory_id:
                continue
            memory.content = normalized_content
            memory.updated_at = datetime.now().isoformat()
            return await self._write_memories(user_id, memories)
        return False

    async def delete_memory_by_id(self, user_id: str, memory_id: str) -> bool:
        memories = await self._read_memories(user_id)
        new_memories = [memory for memory in memories if memory.id != memory_id]
        if len(new_memories) == len(memories):
            return False
        return await self._write_memories(user_id, new_memories)

    async def clear_memories(self, user_id: str) -> bool:
        file_path = self._get_user_file(user_id)
        if not file_path.exists():
            return True

        try:
            file_path.unlink()
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[存储] 清空重要记忆失败")
            return False

    async def replace_memories(self, user_id: str, memories: List[ImportantMemoryItem]) -> bool:
        return await self._write_memories(user_id, memories)

    async def mark_recalled(self, user_id: str, memory_ids: List[str]) -> int:
        """标记重要记忆被召回使用。

        更新 last_recalled_at 和 mention_count。
        返回实际更新的条目数。
        """
        memories = await self._read_memories(user_id)
        if not memories:
            return 0

        now_iso = datetime.now().isoformat()
        updated = 0

        for memory in memories:
            if memory.id not in memory_ids:
                continue
            memory.updated_at = now_iso
            memory.metadata["last_recalled_at"] = now_iso
            mention_count = int(memory.metadata.get("mention_count", 1) or 1) + 1
            memory.metadata["mention_count"] = mention_count
            updated += 1

        if updated == 0:
            return 0

        success = await self._write_memories(user_id, memories)
        return updated if success else 0
