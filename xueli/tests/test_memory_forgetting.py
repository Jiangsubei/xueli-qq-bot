from __future__ import annotations

import builtins
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path


if "aiofiles" not in sys.modules:
    aiofiles = types.ModuleType("aiofiles")

    class _AsyncFile:
        def __init__(self, file_path: str, mode: str, encoding: str | None = None):
            self._handle = builtins.open(file_path, mode, encoding=encoding)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            self._handle.close()

        async def read(self):
            return self._handle.read()

        async def write(self, data):
            self._handle.write(data)
            return len(data)

    def _open(file_path: str, mode: str = "r", encoding: str | None = None):
        return _AsyncFile(file_path, mode, encoding=encoding)

    aiofiles.open = _open
    sys.modules["aiofiles"] = aiofiles

from src.memory.storage.markdown_store import MarkdownMemoryStore, MemoryItem


class MemoryForgettingTests(unittest.TestCase):
    def test_reinforced_ordinary_memory_survives_longer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MarkdownMemoryStore(base_path=str(Path(temp_dir) / "memories"), ordinary_half_life_days=10, ordinary_forget_threshold=0.5)
            old_time = (datetime.now() - timedelta(days=40)).isoformat()

            weak_memory = MemoryItem(
                id="m1",
                content="用户最近在看论文",
                created_at=old_time,
                updated_at=old_time,
                metadata={"memory_type": "ordinary", "importance": 1, "mention_count": 1},
            )
            reinforced_memory = MemoryItem(
                id="m2",
                content="用户最近一直在写毕业论文",
                created_at=old_time,
                updated_at=old_time,
                metadata={
                    "memory_type": "ordinary",
                    "importance": 1,
                    "mention_count": 5,
                    "source_observations": [{"session_id": "s1", "turn_start": 1, "turn_end": 1}],
                    "last_reinforced_at": (datetime.now() - timedelta(days=5)).isoformat(),
                },
            )

            self.assertTrue(store._should_forget(weak_memory))
            self.assertFalse(store._should_forget(reinforced_memory))


if __name__ == "__main__":
    unittest.main()
