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

if "aiohttp" not in sys.modules:
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientError = Exception
    aiohttp.ClientTimeout = object
    aiohttp.ClientSession = object
    sys.modules["aiohttp"] = aiohttp

from src.memory.chat_summary_service import ChatSummaryService
from src.memory.session_restore_service import SessionRestoreService
from src.memory.storage.conversation_store import ConversationStore


class SessionRestoreServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_persists_and_restores_same_dialogue_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ConversationStore(base_path=str(Path(temp_dir) / "conversations"))
            store.add_turn(user_id="user-1", user_message="我最近在准备考研英语", assistant_message="可以先拆成阅读和单词", message_type="private")
            store.add_turn(user_id="user-1", user_message="这周先背单词和做两套阅读", assistant_message="这样安排比较稳", message_type="private")

            session_id = store.close_session(user_id="user-1", message_type="private")
            record = await store.save_conversation(user_id="user-1", session_id=session_id, force=True)

            summary_service = ChatSummaryService(conversation_store=store)
            updated = await summary_service.refresh_session_summary(user_id="user-1", record=record)
            self.assertIsNotNone(updated)

            loaded = await store.load_session("user-1", session_id)
            self.assertIsNotNone(loaded)
            self.assertIn("考研英语", loaded.metadata.get("session_summary", ""))

            restore_service = SessionRestoreService(conversation_store=store, summary_service=summary_service)
            entries = await restore_service.build_restore_entries(user_id="user-1", message_type="private")

            self.assertEqual(len(entries), 1)
            self.assertIn("上一轮会话", entries[0]["content"])
            self.assertIn("考研英语", entries[0]["content"])

    async def test_only_restores_matching_group_dialogue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ConversationStore(base_path=str(Path(temp_dir) / "conversations"))
            summary_service = ChatSummaryService(conversation_store=store)
            restore_service = SessionRestoreService(conversation_store=store, summary_service=summary_service)

            store.add_turn(user_id="user-1", user_message="群1的话题", assistant_message="收到", message_type="group", group_id="g1")
            group_one_session = store.close_session(user_id="user-1", message_type="group", group_id="g1")
            group_one_record = await store.save_conversation(user_id="user-1", session_id=group_one_session, force=True)
            await summary_service.refresh_session_summary(user_id="user-1", record=group_one_record)

            store.add_turn(user_id="user-1", user_message="群2的话题", assistant_message="收到", message_type="group", group_id="g2")
            group_two_session = store.close_session(user_id="user-1", message_type="group", group_id="g2")
            group_two_record = await store.save_conversation(user_id="user-1", session_id=group_two_session, force=True)
            await summary_service.refresh_session_summary(user_id="user-1", record=group_two_record)

            entries = await restore_service.build_restore_entries(user_id="user-1", message_type="group", group_id="g1")

            self.assertEqual(len(entries), 1)
            self.assertIn("群1的话题", entries[0]["content"])
            self.assertNotIn("群2的话题", entries[0]["content"])


if __name__ == "__main__":
    unittest.main()
