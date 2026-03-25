"""
对话记录存储。

按批次保存原始对话记录，便于后续追踪和检索。
"""
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiofiles

logger = logging.getLogger(__name__)


class ConversationRecord:
    """对话记录。"""

    def __init__(
        self,
        record_id: str,
        user_id: str,
        turns: List[Dict[str, str]],
        created_at: str = None,
        metadata: Dict = None,
    ):
        self.record_id = record_id
        self.user_id = user_id
        self.turns = turns
        self.created_at = created_at or datetime.now().isoformat()
        self.metadata = metadata or {}

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典。"""
        return {
            "record_id": self.record_id,
            "user_id": self.user_id,
            "turns": self.turns,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConversationRecord":
        """从字典恢复对象。"""
        return cls(
            record_id=data.get("record_id", ""),
            user_id=data.get("user_id", ""),
            turns=data.get("turns", []),
            created_at=data.get("created_at"),
            metadata=data.get("metadata", {}),
        )


class ConversationStore:
    """对话记录存储器。"""

    def __init__(self, base_path: str = "memories/conversations", save_interval: int = 10):
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        self._buffer: Dict[str, List[Dict[str, str]]] = {}
        self._lock = asyncio.Lock()
        self._save_interval = save_interval

    def _get_user_dir(self, user_id: str) -> Path:
        """获取用户对话目录。"""
        user_dir = self.base_path / user_id
        user_dir.mkdir(exist_ok=True)
        return user_dir

    def add_turn(self, user_id: str, user_message: str, assistant_message: str) -> int:
        """向缓冲区添加一轮对话。"""
        if user_id not in self._buffer:
            self._buffer[user_id] = []

        self._buffer[user_id].append(
            {
                "user": user_message,
                "assistant": assistant_message,
                "timestamp": datetime.now().isoformat(),
            }
        )
        return len(self._buffer[user_id])

    async def save_conversation(self, user_id: str, force: bool = False) -> Optional[ConversationRecord]:
        """保存缓冲区中的对话。"""
        async with self._lock:
            buffer = self._buffer.get(user_id, [])
            if not buffer:
                return None

            if not force and len(buffer) < self._save_interval:
                return None

            record_id = f"conv_{user_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
            record = ConversationRecord(
                record_id=record_id,
                user_id=user_id,
                turns=buffer.copy(),
                metadata={
                    "turn_count": len(buffer),
                    "saved_at": datetime.now().isoformat(),
                },
            )

            user_dir = self._get_user_dir(user_id)
            file_path = user_dir / f"{record_id}.json"

            try:
                async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
                    await f.write(json.dumps(record.to_dict(), ensure_ascii=False, indent=2))

                logger.info(
                    "对话已保存: user=%s, 轮数=%s, 文件=%s",
                    user_id,
                    len(buffer),
                    file_path.name,
                )

                self._buffer[user_id] = []
                return record
            except Exception as e:
                logger.error("保存对话失败: user=%s, 错误=%s", user_id, e)
                return None

    async def get_conversations(self, user_id: str, limit: int = 10) -> List[ConversationRecord]:
        """获取用户最近的对话记录。"""
        user_dir = self.base_path / user_id
        if not user_dir.exists():
            return []

        records = []
        try:
            files = sorted(
                user_dir.glob("*.json"),
                key=lambda file_path: file_path.stat().st_mtime,
                reverse=True,
            )

            for file_path in files[:limit]:
                try:
                    async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                        content = await f.read()
                        data = json.loads(content)
                        records.append(ConversationRecord.from_dict(data))
                except Exception as e:
                    logger.warning("读取对话失败: file=%s, 错误=%s", file_path.name, e)
        except Exception as e:
            logger.error("获取对话列表失败: user=%s, 错误=%s", user_id, e)

        return records

    async def search_conversations(self, user_id: str, keyword: str, limit: int = 5) -> List[ConversationRecord]:
        """搜索包含关键词的对话记录。"""
        all_records = await self.get_conversations(user_id, limit=100)
        matched = []

        keyword_lower = keyword.lower()
        for record in all_records:
            for turn in record.turns:
                if (
                    keyword_lower in turn.get("user", "").lower()
                    or keyword_lower in turn.get("assistant", "").lower()
                ):
                    matched.append(record)
                    break

        return matched[:limit]
