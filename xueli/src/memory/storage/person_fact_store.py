from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import aiofiles

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now().isoformat()


@dataclass
class PersonFactItem:
    content: str
    fact_kind: str = "profile"
    id: str = ""
    created_at: str = ""
    updated_at: str = ""
    source: str = ""
    owner_user_id: str = ""
    source_memory_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = _now_iso()
        if not self.updated_at:
            self.updated_at = self.created_at
        if self.metadata is None:
            self.metadata = {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "fact_kind": self.fact_kind,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source": self.source,
            "owner_user_id": self.owner_user_id,
            "source_memory_id": self.source_memory_id,
            "metadata": dict(self.metadata or {}),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PersonFactItem":
        return cls(
            id=str(data.get("id") or ""),
            content=str(data.get("content") or ""),
            fact_kind=str(data.get("fact_kind") or "profile"),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            source=str(data.get("source") or ""),
            owner_user_id=str(data.get("owner_user_id") or ""),
            source_memory_id=str(data.get("source_memory_id") or ""),
            metadata=dict(data.get("metadata") or {}),
        )


class PersonFactStore:
    def __init__(self, base_path: str = "memories/person_facts") -> None:
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        self._locks: Dict[str, asyncio.Lock] = {}

    def _get_user_file(self, user_id: str) -> Path:
        return self.base_path / f"{user_id}.json"

    def _get_file_lock(self, file_path: str) -> asyncio.Lock:
        if file_path not in self._locks:
            self._locks[file_path] = asyncio.Lock()
        return self._locks[file_path]

    def get_user_ids(self) -> List[str]:
        return sorted(path.stem for path in self.base_path.glob("*.json"))

    async def get_facts(self, user_id: str) -> List[PersonFactItem]:
        file_path = self._get_user_file(user_id)
        if not file_path.exists():
            return []
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as handle:
                payload = json.loads(await handle.read())
            items = [PersonFactItem.from_dict(item) for item in list(payload.get("facts") or [])]
            items.sort(key=lambda item: (item.updated_at, item.created_at, item.content), reverse=True)
            return items
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("读取人物事实失败：用户=%s，错误=%s", user_id, exc)
            return []

    async def replace_facts(self, user_id: str, facts: List[PersonFactItem]) -> bool:
        file_path = self._get_user_file(user_id)
        lock = self._get_file_lock(str(file_path))
        payload = {
            "user_id": str(user_id),
            "updated_at": _now_iso(),
            "facts": [item.to_dict() for item in facts],
        }
        async with lock:
            try:
                tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
                async with aiofiles.open(tmp_path, "w", encoding="utf-8") as handle:
                    await handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
                os.replace(tmp_path, file_path)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("写入人物事实失败：用户=%s，错误=%s", user_id, exc)
                return False

    async def clear_facts(self, user_id: str) -> bool:
        file_path = self._get_user_file(user_id)
        if not file_path.exists():
            return True
        try:
            file_path.unlink()
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("清空人物事实失败：用户=%s，错误=%s", user_id, exc)
            return False

    def make_fact_id(self, user_id: str, fact_kind: str, content: str) -> str:
        normalized = self._normalize_text(content)
        return f"fact_{user_id}_{fact_kind}_{normalized[:48] or 'empty'}"

    def _normalize_text(self, text: str) -> str:
        compact = re.sub(r"\s+", "", str(text or "").strip().lower())
        return re.sub(r"[^\w\u4e00-\u9fff]", "", compact)
