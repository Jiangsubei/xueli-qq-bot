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

from src.memory.internal.access_policy import MemoryAccessPolicy
from src.memory.person_fact_service import PersonFactService
from src.memory.storage.important_memory_store import ImportantMemoryStore
from src.memory.storage.person_fact_store import PersonFactStore


class PersonFactServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_syncs_private_important_memories_into_person_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            important_store = ImportantMemoryStore(base_path=str(Path(temp_dir) / "important"))
            fact_store = PersonFactStore(base_path=str(Path(temp_dir) / "facts"))
            service = PersonFactService(store=fact_store, important_memory_store=important_store, access_policy=MemoryAccessPolicy())

            await important_store.add_memory(
                user_id="u1",
                content="用户喜欢别人直接一点地回复",
                source="manual",
                priority=4,
                metadata={"content_category": "personal_preference"},
            )
            await important_store.add_memory(
                user_id="u1",
                content="这个群的周报每周五发",
                source="manual",
                priority=4,
                metadata={"content_category": "group_rule", "shared_authorized": True},
            )

            facts = await service.sync_user_facts("u1")

            self.assertEqual(len(facts), 1)
            self.assertEqual(facts[0].fact_kind, "preference")
            self.assertIn("直接一点", facts[0].content)

            prompt_text = await service.format_facts_for_prompt(user_id="u1")
            self.assertIn("直接一点", prompt_text)
            self.assertNotIn("周报", prompt_text)


if __name__ == "__main__":
    unittest.main()
