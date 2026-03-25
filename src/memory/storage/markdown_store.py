"""
记忆存储层。

使用 Markdown 持久化记忆，支持透明查看/编辑，以及普通记忆的衰减与归档。
"""
import asyncio
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import aiofiles

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now().isoformat()


def _format_display_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return value or "未知"


def _format_display_number(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)

    if number.is_integer():
        return str(int(number))
    return f"{number:.1f}"


@dataclass
class MemoryItem:
    """单条记忆记录。"""

    id: str
    content: str
    source: str = ""
    created_at: str = ""
    updated_at: str = ""
    tags: Optional[List[str]] = None
    metadata: Optional[Dict] = None

    def __post_init__(self):
        if self.tags is None:
            self.tags = []
        if self.metadata is None:
            self.metadata = {}
        if not self.created_at:
            self.created_at = _now_iso()
        if not self.updated_at:
            self.updated_at = self.created_at

    def to_markdown_block(self) -> str:
        """将记忆序列化为易读的 Markdown 块。"""
        content = self.content.replace("\n", " ").replace("\r", "").strip()

        memory_type = str(self.metadata.get("memory_type", "legacy")).lower()
        if memory_type == "important":
            type_label = "重要记忆"
        elif memory_type == "ordinary":
            type_label = "普通记忆"
        else:
            type_label = "历史记忆"

        lines = [f"- {content}", f"  - 类型: {type_label}"]

        if memory_type == "ordinary":
            importance = self.metadata.get("importance", 3)
            lines.append(f"  - 重要度: {_format_display_number(importance)}")

        mention_count = self.metadata.get("mention_count", 1)
        try:
            mention_count_value = int(float(mention_count))
        except (TypeError, ValueError):
            mention_count_value = 1
        if mention_count_value > 1:
            lines.append(f"  - 提及次数: {mention_count_value}")

        lines.append(f"  - 最近更新: {_format_display_time(self.updated_at)}")

        hidden_payload = {
            "id": self.id,
            "source": self.source,
            "tags": self.tags,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }
        lines.append(
            f"  <!-- {json.dumps(hidden_payload, ensure_ascii=False, separators=(',', ':'))} -->"
        )
        return "\n".join(lines)

    def to_markdown_line(self) -> str:
        """兼容旧调用，返回首行内容。"""
        return self.to_markdown_block()

    @classmethod
    def from_markdown_block(cls, block: str) -> Optional["MemoryItem"]:
        """从 Markdown 块解析记忆项。"""
        lines = [line.rstrip() for line in block.splitlines() if line.strip()]
        if not lines:
            return None

        first_line = lines[0].strip()
        if not first_line.startswith("- "):
            return None

        if re.match(r"-\s*\[(.*?)\]\s*(.+)$", first_line):
            return cls.from_markdown_line(first_line)

        content = first_line[2:].strip()
        hidden_payload: Dict = {}
        visible_meta: Dict[str, str] = {}

        for raw_line in lines[1:]:
            line = raw_line.strip()
            comment_match = re.match(r"<!--\s*(.+?)\s*-->$", line)
            if comment_match:
                try:
                    hidden_payload = json.loads(comment_match.group(1))
                except json.JSONDecodeError:
                    logger.debug("解析记忆隐藏元数据失败: %s", line[:120])
                continue

            meta_match = re.match(r"-\s*([^:]+):\s*(.+)$", line)
            if meta_match:
                key, value = meta_match.groups()
                visible_meta[key.strip()] = value.strip()

        metadata = hidden_payload.get("metadata") or {}
        if "重要度" in visible_meta and "importance" not in metadata:
            try:
                metadata["importance"] = float(visible_meta["重要度"])
            except ValueError:
                pass
        if "提及次数" in visible_meta and "mention_count" not in metadata:
            try:
                metadata["mention_count"] = int(visible_meta["提及次数"])
            except ValueError:
                pass
        if "类型" in visible_meta and "memory_type" not in metadata:
            memory_type_map = {
                "重要记忆": "important",
                "普通记忆": "ordinary",
                "历史记忆": "legacy",
            }
            metadata["memory_type"] = memory_type_map.get(visible_meta["类型"], "legacy")

        created_at = hidden_payload.get("created_at") or _now_iso()
        updated_at = hidden_payload.get("updated_at") or created_at

        return cls(
            id=hidden_payload.get("id", f"mem_{datetime.now().strftime('%Y%m%d%H%M%S')}"),
            content=content,
            source=hidden_payload.get("source", ""),
            created_at=created_at,
            updated_at=updated_at,
            tags=hidden_payload.get("tags") or [],
            metadata=metadata,
        )

    @classmethod
    def from_markdown_line(cls, line: str) -> Optional["MemoryItem"]:
        """兼容旧版单行格式。"""
        line = line.strip()
        if not line.startswith("- "):
            return None

        try:
            match = re.match(r"-\s*\[(.*?)\]\s*(.+)$", line)
            if not match:
                content = line[2:].strip()
                return cls(
                    id=f"mem_{datetime.now().strftime('%Y%m%d%H%M%S')}",
                    content=content,
                    metadata={"mention_count": 1},
                )

            meta_str, content = match.groups()
            parsed_meta: Dict[str, str] = {}
            for part in meta_str.split("|"):
                part = part.strip()
                if "=" in part:
                    key, value = part.split("=", 1)
                    parsed_meta[key.strip()] = value.strip()

            tags = parsed_meta.get("tags", "").split(",") if parsed_meta.get("tags") else []
            metadata: Dict = {}
            if parsed_meta.get("meta"):
                try:
                    metadata = json.loads(parsed_meta["meta"])
                except json.JSONDecodeError:
                    logger.debug("解析旧版记忆元数据失败: %s", line[:120])

            if "mention_count" not in metadata:
                metadata["mention_count"] = 1

            created_at = parsed_meta.get("created") or parsed_meta.get("time") or _now_iso()
            updated_at = parsed_meta.get("updated") or parsed_meta.get("time") or created_at

            return cls(
                id=parsed_meta.get("id", f"mem_{datetime.now().strftime('%Y%m%d%H%M%S')}"),
                content=content.strip(),
                source=parsed_meta.get("src", ""),
                created_at=created_at,
                updated_at=updated_at,
                tags=[tag.strip() for tag in tags if tag.strip()],
                metadata=metadata,
            )
        except Exception as e:
            logger.warning("解析记忆失败: 内容=%s, 错误=%s", line[:80], e)
            return None


class MarkdownMemoryStore:
    """基于 Markdown 的记忆存储。"""

    def __init__(
        self,
        base_path: str = "memories",
        ordinary_decay_enabled: bool = True,
        ordinary_half_life_days: float = 30.0,
        ordinary_forget_threshold: float = 0.5,
    ):
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

        self.users_path = self.base_path / "users"
        self.users_path.mkdir(exist_ok=True)

        self.archive_path = self.base_path / "archive"
        self.archive_path.mkdir(exist_ok=True)
        self.archive_users_path = self.archive_path / "users"
        self.archive_users_path.mkdir(exist_ok=True)

        self.global_file = self.base_path / "global.md"
        self.archive_global_file = self.archive_path / "global.md"
        self.ordinary_decay_enabled = ordinary_decay_enabled
        self.ordinary_half_life_days = max(float(ordinary_half_life_days), 0.1)
        self.ordinary_forget_threshold = float(ordinary_forget_threshold)
        self._locks: Dict[str, asyncio.Lock] = {}

    def _get_user_file(self, user_id: str) -> Path:
        return self.users_path / f"{user_id}.md"

    def _get_archive_user_file(self, user_id: str) -> Path:
        return self.archive_users_path / f"{user_id}.md"

    def _get_file_lock(self, file_path: str) -> asyncio.Lock:
        if file_path not in self._locks:
            self._locks[file_path] = asyncio.Lock()
        return self._locks[file_path]

    def _normalize_content(self, content: str) -> str:
        normalized = content.lower().strip()
        normalized = re.sub(r"[^\w\u4e00-\u9fff]", "", normalized)
        return normalized

    def _is_same_memory(self, existing: str, incoming: str) -> bool:
        left = self._normalize_content(existing)
        right = self._normalize_content(incoming)

        if not left or not right:
            return False
        if left == right:
            return True

        shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
        if len(shorter) < 6:
            return False
        return shorter in longer and (len(shorter) / max(len(longer), 1)) >= 0.7

    def _safe_float(self, value, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _get_memory_kind(self, mem: MemoryItem) -> str:
        return str(mem.metadata.get("memory_type", "legacy")).lower()

    def _get_base_importance(self, mem: MemoryItem) -> float:
        return self._safe_float(mem.metadata.get("importance"), 3.0)

    def _get_effective_importance(self, mem: MemoryItem, now: Optional[datetime] = None) -> float:
        if not self.ordinary_decay_enabled:
            return self._get_base_importance(mem)

        if self._get_memory_kind(mem) != "ordinary":
            return self._get_base_importance(mem)

        if mem.metadata.get("decay_exempt", False):
            return self._get_base_importance(mem)

        reference_time = mem.updated_at or mem.created_at
        try:
            reference_dt = datetime.fromisoformat(reference_time)
        except ValueError:
            reference_dt = datetime.now()

        now_dt = now or datetime.now()
        age_days = max((now_dt - reference_dt).total_seconds() / 86400.0, 0.0)
        base = max(self._get_base_importance(mem), 0.0)
        decay_factor = math.pow(0.5, age_days / self.ordinary_half_life_days)
        return base * decay_factor

    def _should_forget(self, mem: MemoryItem, now: Optional[datetime] = None) -> bool:
        if not self.ordinary_decay_enabled:
            return False
        if self._get_memory_kind(mem) != "ordinary":
            return False
        effective = self._get_effective_importance(mem, now=now)
        return effective < self.ordinary_forget_threshold

    def _partition_memories_by_decay(
        self,
        memories: List[MemoryItem],
    ) -> tuple[List[MemoryItem], List[MemoryItem]]:
        """按衰减状态拆分记忆。"""
        now = datetime.now()
        active: List[MemoryItem] = []
        archived: List[MemoryItem] = []

        for mem in memories:
            if self._should_forget(mem, now=now):
                archived.append(mem)
            else:
                active.append(mem)

        return active, archived

    def _parse_memory_blocks(self, content: str) -> List[str]:
        blocks: List[str] = []
        current: List[str] = []

        for raw_line in content.splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()

            if not stripped or stripped.startswith("#") or stripped.startswith(">"):
                continue

            if line.startswith("- ") and not line.startswith("  - "):
                if current:
                    blocks.append("\n".join(current))
                current = [stripped]
            elif current:
                current.append(stripped)

        if current:
            blocks.append("\n".join(current))

        return blocks

    def _prepare_new_metadata(self, metadata: Optional[Dict]) -> Dict:
        prepared = dict(metadata or {})
        prepared.setdefault("mention_count", 1)
        return prepared

    def _reinforce_existing_memory(
        self,
        mem: MemoryItem,
        incoming_content: str,
        source: str,
        tags: Optional[List[str]],
        metadata: Optional[Dict],
        now_iso: str,
    ):
        incoming_metadata = dict(metadata or {})

        if source and not mem.source:
            mem.source = source
        if tags:
            mem.tags = sorted(set(mem.tags + list(tags)))

        if len(incoming_content.strip()) > len(mem.content.strip()):
            mem.content = incoming_content.strip()

        mem.metadata.update(incoming_metadata)
        mem.updated_at = now_iso

        mention_count = int(mem.metadata.get("mention_count", 1)) + 1
        mem.metadata["mention_count"] = mention_count
        mem.metadata["last_reinforced_at"] = now_iso

        if self._get_memory_kind(mem) == "ordinary" and not mem.metadata.get("decay_exempt", False):
            current_importance = self._get_base_importance(mem)
            incoming_importance = self._safe_float(incoming_metadata.get("importance"), current_importance)
            boosted_importance = min(5.0, max(current_importance, incoming_importance) + 1.0)
            mem.metadata["importance"] = boosted_importance
            logger.info(
                "记忆重复提及，已增强: 内容=%s, 提及次数=%s, 新重要度=%.1f",
                mem.content[:30],
                mention_count,
                boosted_importance,
            )

    async def _read_memories_async(self, file_path: Path) -> List[MemoryItem]:
        if not file_path.exists():
            return []

        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                content = await f.read()
        except Exception as e:
            logger.error("读取记忆文件失败: %s, 错误=%s", file_path, e)
            return []

        memories: List[MemoryItem] = []
        for block in self._parse_memory_blocks(content):
            mem = MemoryItem.from_markdown_block(block)
            if mem:
                memories.append(mem)

        return memories

    async def _write_memories_async(self, file_path: Path, memories: List[MemoryItem]) -> bool:
        lock = self._get_file_lock(str(file_path))

        async with lock:
            try:
                file_path.parent.mkdir(parents=True, exist_ok=True)
                blocks = [mem.to_markdown_block() for mem in memories]

                async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
                    await f.write("\n\n".join(blocks))
                return True
            except Exception as e:
                logger.error("写入记忆文件失败: %s, 错误=%s", file_path, e)
                return False

    async def _remove_file_if_exists(self, file_path: Path):
        if file_path.exists():
            try:
                file_path.unlink()
            except Exception as e:
                logger.warning("删除空归档文件失败: %s, 错误=%s", file_path, e)

    async def _sync_archive_file(self, file_path: Path, archived: List[MemoryItem], label: str):
        """同步导出软归档记忆。"""
        if archived:
            success = await self._write_memories_async(file_path, archived)
            if success:
                logger.info("已同步归档记忆: 目标=%s, 条数=%s", label, len(archived))
        else:
            await self._remove_file_if_exists(file_path)

    async def get_user_memories(self, user_id: str) -> List[MemoryItem]:
        memories = await self._read_memories_async(self._get_user_file(user_id))
        active, archived = self._partition_memories_by_decay(memories)
        await self._sync_archive_file(self._get_archive_user_file(user_id), archived, f"user:{user_id}")
        if archived:
            logger.info("读取用户记忆时跳过已归档内容: user=%s, 条数=%s", user_id, len(archived))
        return active

    async def get_global_memories(self) -> List[MemoryItem]:
        memories = await self._read_memories_async(self.global_file)
        active, archived = self._partition_memories_by_decay(memories)
        await self._sync_archive_file(self.archive_global_file, archived, "global")
        if archived:
            logger.info("读取全局记忆时跳过已归档内容: 条数=%s", len(archived))
        return active

    async def get_archived_user_memories(self, user_id: str) -> List[MemoryItem]:
        """返回用户的软归档记忆，不修改原始文件。"""
        memories = await self._read_memories_async(self._get_user_file(user_id))
        _, archived = self._partition_memories_by_decay(memories)
        await self._sync_archive_file(self._get_archive_user_file(user_id), archived, f"user:{user_id}")
        return archived

    async def get_archived_global_memories(self) -> List[MemoryItem]:
        """返回全局软归档记忆，不修改原始文件。"""
        memories = await self._read_memories_async(self.global_file)
        _, archived = self._partition_memories_by_decay(memories)
        await self._sync_archive_file(self.archive_global_file, archived, "global")
        return archived

    async def get_all_memories(self, user_id: str) -> List[MemoryItem]:
        return await self.get_user_memories(user_id) + await self.get_global_memories()

    async def add_memory(
        self,
        content: str,
        user_id: Optional[str] = None,
        source: str = "",
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict] = None,
    ) -> Optional[MemoryItem]:
        mem_id = f"mem_{datetime.now().strftime('%Y%m%d%H%M%S')}_{hash(content) % 10000:04d}"
        target_file = self._get_user_file(user_id) if user_id else self.global_file
        memories = await self._read_memories_async(target_file)
        now_iso = _now_iso()
        normalized_content = content.strip()

        for existing in memories:
            if self._is_same_memory(existing.content, normalized_content):
                self._reinforce_existing_memory(
                    mem=existing,
                    incoming_content=normalized_content,
                    source=source,
                    tags=tags,
                    metadata=metadata,
                    now_iso=now_iso,
                )
                success = await self._write_memories_async(target_file, memories)
                return existing if success else None

        mem = MemoryItem(
            id=mem_id,
            content=normalized_content,
            source=source,
            created_at=now_iso,
            updated_at=now_iso,
            tags=tags or [],
            metadata=self._prepare_new_metadata(metadata),
        )
        memories.append(mem)
        success = await self._write_memories_async(target_file, memories)

        if success:
            logger.info("新增记忆: user=%s, 内容=%s", user_id or "global", normalized_content[:50])
            return mem
        return None

    async def delete_memory(self, mem_id: str, user_id: Optional[str] = None) -> bool:
        target_file = self._get_user_file(user_id) if user_id else self.global_file
        memories = await self._read_memories_async(target_file)
        new_memories = [m for m in memories if m.id != mem_id]
        if len(new_memories) == len(memories):
            return False
        return await self._write_memories_async(target_file, new_memories)

    async def update_memory(self, mem_id: str, content: str, user_id: Optional[str] = None) -> bool:
        target_file = self._get_user_file(user_id) if user_id else self.global_file
        memories = await self._read_memories_async(target_file)
        for mem in memories:
            if mem.id == mem_id:
                mem.content = content.strip()
                mem.updated_at = _now_iso()
                return await self._write_memories_async(target_file, memories)
        return False

    async def search_memories_by_keyword(self, keyword: str, user_id: Optional[str] = None) -> List[MemoryItem]:
        memories = await self.get_all_memories(user_id) if user_id else await self.get_global_memories()
        keyword_lower = keyword.lower()
        return [mem for mem in memories if keyword_lower in mem.content.lower()]
