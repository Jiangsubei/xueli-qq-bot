import os
import shutil
import unittest
import uuid

from src.memory.extraction.memory_extractor import ExtractionConfig, MemoryExtractor
from src.memory.memory_manager import MemoryManager, MemoryManagerConfig
from src.memory.retrieval.two_stage_retriever import RetrievalConfig
from src.memory.storage.important_memory_store import ImportantMemoryStore
from src.memory.storage.markdown_store import MarkdownMemoryStore


class MemoryPriorityRuleTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_max_importance_memory_is_promoted_to_important(self):
        temp_dir = self._make_temp_dir()
        try:
            memory_store = MarkdownMemoryStore(base_path=temp_dir)
            important_store = ImportantMemoryStore(base_path=f"{temp_dir}/important")

            async def fake_llm_callback(system_prompt, messages):
                return "[NORMAL:5] 用户42: 我长期喜欢黑咖啡"

            extractor = MemoryExtractor(
                memory_store=memory_store,
                llm_callback=fake_llm_callback,
                config=ExtractionConfig(extract_every_n_turns=1, max_dialogue_length=5),
                important_memory_store=important_store,
            )

            extractor.add_dialogue_turn("42", "我长期喜欢黑咖啡", "好的")
            await extractor.extract_memories("42")

            extractor.add_dialogue_turn("42", "我长期喜欢黑咖啡", "记住了")
            await extractor.extract_memories("42")

            important_memories = await important_store.get_memories("42")

            self.assertEqual(len(important_memories), 1)
            self.assertEqual(important_memories[0].content, "我长期喜欢黑咖啡")
            self.assertGreaterEqual(important_memories[0].priority, 4)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def test_search_prefers_important_memories_before_ordinary_flow(self):
        temp_dir = self._make_temp_dir()
        try:
            manager = MemoryManager(
                config=MemoryManagerConfig(
                    storage_base_path=temp_dir,
                    retrieval_config=RetrievalConfig(rerank_enabled=False),
                    auto_build_index=False,
                    auto_extract_memory=False,
                )
            )

            await manager.add_important_memory(
                user_id="u1",
                content="不要叫我全名",
                source="manual",
                priority=5,
            )
            await manager.add_memory(
                user_id="u1",
                content="我喜欢黑咖啡",
                source="manual",
                metadata={"memory_type": "ordinary", "importance": 3},
            )

            search_result = await manager.search_memories_with_context(
                user_id="u1",
                query="别叫我全名",
                top_k=3,
                include_conversations=True,
            )

            self.assertEqual(len(search_result["memories"]), 1)
            self.assertEqual(search_result["memories"][0]["content"], "不要叫我全名")
            self.assertEqual(search_result["memories"][0]["memory_type"], "important")
            self.assertEqual(search_result["history_messages"], [])
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _make_temp_dir(self):
        base_dir = os.path.join(os.getcwd(), "tmp_test_memory_rules")
        os.makedirs(base_dir, exist_ok=True)
        temp_dir = os.path.join(base_dir, uuid.uuid4().hex)
        os.makedirs(temp_dir, exist_ok=True)
        return temp_dir


if __name__ == "__main__":
    unittest.main()
