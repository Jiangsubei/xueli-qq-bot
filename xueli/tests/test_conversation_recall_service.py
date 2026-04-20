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

from src.memory.conversation_recall_service import ConversationRecallService
from src.memory.storage.conversation_store import ConversationStore


class ConversationRecallServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_builds_first_and_latest_recall_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ConversationStore(base_path=str(Path(temp_dir) / "conversations"))
            store.add_turn(user_id="u1", user_message="我准备写毕业论文开题", assistant_message="先定题目", message_type="private")
            first_session = store.close_session(user_id="u1", message_type="private")
            await store.save_conversation(user_id="u1", session_id=first_session, force=True)

            store.add_turn(user_id="u1", user_message="毕业论文文献综述我还没写完", assistant_message="可以先列参考文献", message_type="private")
            second_session = store.close_session(user_id="u1", message_type="private")
            await store.save_conversation(user_id="u1", session_id=second_session, force=True)

            service = ConversationRecallService(conversation_store=store)
            entries = await service.build_recall_entries(user_id="u1", query="继续聊毕业论文", message_type="private")

            self.assertEqual(len(entries), 2)
            self.assertIn("第一次提到相关话题", entries[0]["content"])
            self.assertIn("最近一次提到相关话题", entries[1]["content"])
            self.assertIn("毕业论文", entries[0]["content"])
            self.assertIn("毕业论文", entries[1]["content"])


if __name__ == "__main__":
    unittest.main()
