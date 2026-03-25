"""
重要记忆存储。

这些记忆会在新会话开始时优先读取，也会在检索阶段优先匹配。
"""
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiofiles

logger = logging.getLogger(__name__)


class ImportantMemoryItem:
    """重要记忆项。"""

    def __init__(
        self,
        content: str,
        created_at: str = None,
        source: str = "",
        priority: int = 1,
        score: float = 0.0,
    ):
        self.content = content
        self.created_at = created_at or datetime.now().isoformat()
        self.source = source
        self.priority = priority
        self.score = score

    def to_dict(self) -> Dict[str, Any]:
        return {
            "content": self.content,
            "created_at": self.created_at,
            "source": self.source,
            "priority": self.priority,
            "score": self.score,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ImportantMemoryItem":
        return cls(
            content=data.get("content", ""),
            created_at=data.get("created_at"),
            source=data.get("source", ""),
            priority=data.get("priority", 1),
            score=float(data.get("score", 0.0) or 0.0),
        )


class ImportantMemoryStore:
    """使用 Markdown 存储重要记忆。"""

    def __init__(self, base_path: str = "memories/important"):
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _get_user_file(self, user_id: str) -> Path:
        return self.base_path / f"{user_id}.md"

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
                    source_match = re.search(r"source:(.*?)\s*-->", line)

                    created_at = timestamp_match.group(1) if timestamp_match else datetime.now().isoformat()
                    priority = int(priority_match.group(1)) if priority_match else 1

                    content_start = line.find("]", line.find("[P")) + 1 if "[P" in line else 2
                    content_end = line.find("<!--") if "<!--" in line else len(line)
                    memory_content = line[content_start:content_end].strip()
                    source = source_match.group(1).strip() if source_match else "unknown"

                    if memory_content:
                        memories.append(
                            ImportantMemoryItem(
                                content=memory_content,
                                created_at=created_at,
                                source=source,
                                priority=priority,
                            )
                        )
                except Exception as exc:
                    logger.debug("解析重要记忆行失败: %s", exc)

            return memories
        except Exception as exc:
            logger.error("读取重要记忆失败: user=%s, 错误=%s", user_id, exc)
            return []

    async def _write_memories(self, user_id: str, memories: List[ImportantMemoryItem]) -> bool:
        file_path = self._get_user_file(user_id)
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
                    f"- [{display_time}] [P{memory.priority}] {memory.content}<!-- source:{memory.source} -->"
                )

            async with aiofiles.open(file_path, "w", encoding="utf-8") as file:
                await file.write("\n".join(lines))
            return True
        except Exception as exc:
            logger.error("写入重要记忆失败: user=%s, 错误=%s", user_id, exc)
            return False

    async def add_memory(
        self,
        user_id: str,
        content: str,
        source: str = "manual",
        priority: int = 1,
    ) -> Optional[ImportantMemoryItem]:
        """追加一条重要记忆；相同内容会去重并提升优先级。"""
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

                success = await self._write_memories(user_id, memories)
                if success:
                    logger.info(
                        "重要记忆已去重更新: user=%s, 优先级=P%s, 内容=%s",
                        user_id,
                        existing.priority,
                        existing.content[:40],
                    )
                    return existing
                return None

        memory = ImportantMemoryItem(
            content=normalized_content,
            source=source,
            priority=int(priority),
        )
        memories.append(memory)
        memories.sort(key=lambda item: (item.priority, item.created_at), reverse=True)

        success = await self._write_memories(user_id, memories)
        if success:
            logger.info(
                "重要记忆已保存: user=%s, 优先级=P%s, 内容=%s",
                user_id,
                priority,
                normalized_content[:40],
            )
            return memory
        return None

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
        """按相关度搜索重要记忆。"""
        memories = await self.get_memories(user_id, min_priority=min_priority)
        matched: List[ImportantMemoryItem] = []

        for memory in memories:
            score = self._score_match(query, memory.content)
            if score >= min_score:
                matched.append(
                    ImportantMemoryItem(
                        content=memory.content,
                        created_at=memory.created_at,
                        source=memory.source,
                        priority=memory.priority,
                        score=score,
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
        except Exception as exc:
            logger.error("删除重要记忆失败: user=%s, 错误=%s", user_id, exc)
            return False

    async def clear_memories(self, user_id: str) -> bool:
        file_path = self._get_user_file(user_id)
        if not file_path.exists():
            return True

        try:
            file_path.unlink()
            logger.info("重要记忆已清空: user=%s", user_id)
            return True
        except Exception as exc:
            logger.error("清空重要记忆失败: user=%s, 错误=%s", user_id, exc)
            return False
