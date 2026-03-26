import asyncio
import shutil
import unittest
import uuid
from pathlib import Path

from tests.test_support import FakeExtractor, install_dependency_stubs

install_dependency_stubs()

from src.memory.memory_manager import MemoryManager, MemoryManagerConfig
from src.memory.retrieval.two_stage_retriever import RetrievalConfig
from src.memory.storage.markdown_store import MemoryItem

TMP_ROOT = Path("tests") / "_tmp"


class MemoryManagerTests(unittest.IsolatedAsyncioTestCase):
    def make_temp_dir(self):
        temp_dir = TMP_ROOT / f"memory_{uuid.uuid4().hex}"
        temp_dir.mkdir(parents=True, exist_ok=False)
        self.addCleanup(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        return temp_dir

    def build_manager(self, temp_dir, **kwargs):
        config = MemoryManagerConfig(
            storage_base_path=str(temp_dir),
            memory_read_scope=kwargs.get("memory_read_scope", "user"),
            retrieval_config=RetrievalConfig(
                bm25_top_k=kwargs.get("bm25_top_k", 10),
                rerank_enabled=False,
                rerank_top_k=kwargs.get("rerank_top_k", 5),
            ),
            conversation_save_interval=kwargs.get("conversation_save_interval", 1),
            auto_extract_memory=kwargs.get("auto_extract_memory", True),
            auto_build_index=True,
        )
        return MemoryManager(config=config)

    async def test_initialize_add_memory_rebuild_and_search(self):
        temp_dir = self.make_temp_dir()
        manager = self.build_manager(temp_dir)
        await manager.initialize()
        await manager.add_memory(
            content="likes cats and coffee",
            user_id="u1",
            source="test",
        )
        await manager.rebuild_index("u1")

        result = await manager.search_memories_with_context(
            user_id="u1",
            query="cats",
            top_k=3,
        )

        self.assertTrue(result["memories"])
        self.assertIn("cats", result["memories"][0]["content"])
        await manager.close()

    async def test_add_important_memory_formats_prompt(self):
        temp_dir = self.make_temp_dir()
        manager = self.build_manager(temp_dir)
        await manager.initialize()
        await manager.add_important_memory(
            user_id="u1",
            content="Call me Captain",
            priority=5,
        )

        prompt = await manager.format_important_memories_for_prompt("u1")

        self.assertIn("Call me Captain", prompt)
        await manager.close()

    async def test_flush_background_tasks_saves_conversation_and_extracts_memory(self):
        temp_dir = self.make_temp_dir()
        manager = self.build_manager(temp_dir, conversation_save_interval=1, auto_extract_memory=True)
        await manager.initialize()

        fake_extractor = FakeExtractor(
            should_extract=True,
            returned_memories=[MemoryItem(id="mem1", content="likes ramen")],
        )
        manager.extractor = fake_extractor
        manager.background_coordinator.extractor = fake_extractor

        manager.register_dialogue_turn(
            user_id="u1",
            user_message="remember I like ramen",
            assistant_message="noted",
        )
        manager.schedule_memory_extraction("u1")
        await manager.flush_background_tasks()

        saved_files = list((temp_dir / "conversations" / "u1").glob("*.json"))
        self.assertTrue(saved_files)
        self.assertEqual(["u1"], fake_extractor.extract_calls)
        await manager.close()

    async def test_close_cancels_background_tasks(self):
        temp_dir = self.make_temp_dir()
        manager = self.build_manager(temp_dir)
        await manager.initialize()

        task = manager.task_manager.create_task(asyncio.sleep(60), name="memory-test-sleep")
        await manager.close()

        self.assertTrue(task.cancelled() or task.done())
        self.assertEqual(0, manager.task_manager.count())


if __name__ == "__main__":
    unittest.main()
