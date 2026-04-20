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

from src.memory.extraction.memory_extractor import ExtractionConfig, MemoryExtractor
from src.memory.memory_manager import MemoryManager, MemoryManagerConfig
from src.memory.storage.important_memory_store import ImportantMemoryStore
from src.memory.storage.markdown_store import MarkdownMemoryStore


class MemoryReflectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_conflicting_memory_triggers_reflection_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MarkdownMemoryStore(base_path=str(Path(temp_dir) / "memories"))
            important_store = ImportantMemoryStore(base_path=str(Path(temp_dir) / "important"))
            await store.add_memory(
                content="用户喜欢咖啡",
                user_id="u1",
                source="manual",
                metadata={"memory_type": "ordinary", "importance": 3},
            )

            llm_calls: list[str] = []

            async def llm_callback(system_prompt: str, messages: list[dict[str, str]]):
                del messages
                llm_calls.append(system_prompt)
                if "整理出值得记住" in system_prompt:
                    return "[NORMAL:4][T1] 用户u1: 用户现在不想喝咖啡"
                if "冷静的记忆反思器" in system_prompt:
                    return (
                        '{"has_conflict": true, "conflict_type": "temporary_state", '
                        '"action": "keep_both_prefer_recent", '
                        '"summary": "用户以前表达过喜欢咖啡，但最近表示当前不想喝咖啡，更像阶段性状态，应优先理解最近状态。", '
                        '"reason": "新旧记忆围绕同一主题且立场相反，但新表达带有明显当下状态色彩。", '
                        '"confidence": 0.84}'
                    )
                raise AssertionError("unexpected llm call")

            extractor = MemoryExtractor(
                memory_store=store,
                llm_callback=llm_callback,
                config=ExtractionConfig(),
                important_memory_store=important_store,
            )
            extractor.add_dialogue_turn(
                user_id="u1",
                user_message="我现在不想喝咖啡",
                assistant_message="那今天换点别的。",
                session_id="session-1",
                turn_id=1,
                dialogue_key="private:u1",
            )

            saved = await extractor.extract_memories("u1", session_id="session-1")

            self.assertEqual(len(saved), 1)
            metadata = dict(saved[0].metadata or {})
            self.assertIn("summary", metadata)
            self.assertIn("最近表示当前不想喝咖啡", metadata["summary"])
            self.assertIn("reflection", metadata)
            self.assertTrue(metadata["reflection"]["has_conflict"])
            self.assertEqual(metadata.get("patch_status"), "active_patch")
            self.assertEqual(metadata.get("patch_action"), "keep_both_prefer_recent")
            self.assertGreaterEqual(len(metadata["reflection"].get("evidence") or []), 2)
            stored = await store._read_memories_async(store._get_user_file("u1"), owner_user_id="u1")
            old_memory = next(item for item in stored if item.content == "用户喜欢咖啡")
            self.assertEqual(old_memory.metadata.get("patch_status"), "superseded")
            self.assertEqual(old_memory.metadata.get("patch_successor_memory_id"), saved[0].id)
            self.assertEqual(len(llm_calls), 2)

    async def test_non_conflicting_memory_skips_reflection_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MarkdownMemoryStore(base_path=str(Path(temp_dir) / "memories"))
            await store.add_memory(
                content="用户喜欢咖啡",
                user_id="u1",
                source="manual",
                metadata={"memory_type": "ordinary", "importance": 3},
            )

            reflection_calls = 0

            async def llm_callback(system_prompt: str, messages: list[dict[str, str]]):
                del messages
                nonlocal reflection_calls
                if "整理出值得记住" in system_prompt:
                    return "[NORMAL:4][T1] 用户u1: 用户最近在准备毕业论文"
                if "冷静的记忆反思器" in system_prompt:
                    reflection_calls += 1
                    return '{"has_conflict": false, "conflict_type": "none", "action": "keep_both", "summary": "", "reason": "", "confidence": 0.0}'
                raise AssertionError("unexpected llm call")

            extractor = MemoryExtractor(
                memory_store=store,
                llm_callback=llm_callback,
                config=ExtractionConfig(),
            )
            extractor.add_dialogue_turn(
                user_id="u1",
                user_message="我最近在准备毕业论文",
                assistant_message="那我们先拆任务。",
                session_id="session-2",
                turn_id=1,
                dialogue_key="private:u1",
            )

            saved = await extractor.extract_memories("u1", session_id="session-2")

            self.assertEqual(len(saved), 1)
            metadata = dict(saved[0].metadata or {})
            self.assertNotIn("reflection", metadata)
            self.assertEqual(reflection_calls, 0)

    async def test_retrieval_prefers_new_patch_and_skips_superseded_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base_path = str(Path(temp_dir) / "memories")
            manager = MemoryManager(config=MemoryManagerConfig(storage_base_path=base_path))
            await manager.initialize()
            try:
                await manager.storage.add_memory(
                    content="用户喜欢咖啡",
                    user_id="u1",
                    source="manual",
                    metadata={"memory_type": "ordinary", "importance": 3},
                )

                async def llm_callback(system_prompt: str, messages: list[dict[str, str]]):
                    del messages
                    if "整理出值得记住" in system_prompt:
                        return "[NORMAL:4][T1] 用户u1: 用户现在不想喝咖啡"
                    if "冷静的记忆反思器" in system_prompt:
                        return (
                            '{"has_conflict": true, "conflict_type": "temporary_state", '
                            '"action": "keep_both_prefer_recent", '
                            '"summary": "用户以前表达过喜欢咖啡，但最近表示当前不想喝咖啡，更像阶段性状态，应优先理解最近状态。", '
                            '"reason": "围绕同一主题，近期表达更应优先。", '
                            '"confidence": 0.88}'
                        )
                    raise AssertionError("unexpected llm call")

                extractor = MemoryExtractor(
                    memory_store=manager.storage,
                    llm_callback=llm_callback,
                    config=ExtractionConfig(),
                    important_memory_store=manager.important_memory_store,
                )
                extractor.add_dialogue_turn(
                    user_id="u1",
                    user_message="我现在不想喝咖啡",
                    assistant_message="那今天就别喝了。",
                    session_id="session-3",
                    turn_id=1,
                    dialogue_key="private:u1",
                )
                await extractor.extract_memories("u1", session_id="session-3")
                manager.mark_index_dirty("u1")

                payload = await manager.search_memories_with_context(
                    user_id="u1",
                    query="咖啡",
                    include_conversations=False,
                )

                memories = list(payload.get("memories") or [])
                self.assertEqual(len(memories), 1)
                self.assertIn("最近表示当前不想喝咖啡", memories[0]["content"])
                self.assertNotIn("用户喜欢咖啡", memories[0]["content"])
            finally:
                await manager.close()


if __name__ == "__main__":
    unittest.main()
