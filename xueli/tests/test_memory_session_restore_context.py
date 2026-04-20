from __future__ import annotations

import builtins
import sys
import tempfile
import types
import unittest
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

if "aiohttp" not in sys.modules:
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientError = Exception
    aiohttp.ClientTimeout = object
    aiohttp.ClientSession = object
    sys.modules["aiohttp"] = aiohttp

if "jieba" not in sys.modules:
    jieba = types.ModuleType("jieba")
    jieba.cut = lambda text: str(text or "").split()
    sys.modules["jieba"] = jieba

if "rank_bm25" not in sys.modules:
    rank_bm25 = types.ModuleType("rank_bm25")

    class _BM25Okapi:
        def __init__(self, corpus):
            self._corpus = list(corpus or [])

        def get_scores(self, query_tokens):
            query_set = set(query_tokens or [])
            return [float(len(query_set & set(doc))) for doc in self._corpus]

    rank_bm25.BM25Okapi = _BM25Okapi
    sys.modules["rank_bm25"] = rank_bm25

from src.memory.memory_manager import MemoryManager, MemoryManagerConfig


class MemorySessionRestoreContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_payload_includes_session_restore_section(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = MemoryManager(
                config=MemoryManagerConfig(
                    storage_base_path=str(Path(temp_dir) / "memories"),
                )
            )
            try:
                manager.register_dialogue_turn(
                    user_id="user-1",
                    user_message="上次说过我在写毕业论文",
                    assistant_message="可以先定一个目录",
                    message_type="private",
                )
                session_id = manager.conversation_store.close_session(user_id="user-1", message_type="private")
                record = await manager.conversation_store.save_conversation(
                    user_id="user-1",
                    session_id=session_id,
                    force=True,
                )
                await manager.chat_summary_service.refresh_session_summary(user_id="user-1", record=record)

                payload = await manager.search_memories_with_context(
                    user_id="user-1",
                    query="继续聊毕业论文",
                    include_conversations=True,
                )

                self.assertIn("session_restore", payload)
                self.assertEqual(len(payload["session_restore"]), 1)
                self.assertIn("毕业论文", payload["session_restore"][0]["content"])
                self.assertIn("session_restore", payload["prompt_sections"])
            finally:
                await manager.close()


if __name__ == "__main__":
    unittest.main()
