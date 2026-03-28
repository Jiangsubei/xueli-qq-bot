import asyncio
import shutil
import unittest
import uuid
from pathlib import Path

from tests.test_support import FakeExtractor, install_dependency_stubs, read_json

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
        return MemoryManager(config=config, llm_callback=kwargs.get("llm_callback"))

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
        self.assertEqual(1, len(fake_extractor.extract_calls))
        self.assertEqual("u1", fake_extractor.extract_calls[0][0])
        self.assertTrue(fake_extractor.extract_calls[0][1].startswith("session_u1_private_u1_"))
        await manager.close()

    async def test_same_session_updates_single_session_file(self):
        temp_dir = self.make_temp_dir()
        manager = self.build_manager(temp_dir, conversation_save_interval=5, auto_extract_memory=False)
        await manager.initialize()

        manager.register_dialogue_turn("u1", "??", "???")
        manager.register_dialogue_turn("u1", "???", "??")
        await manager.flush_background_tasks()

        saved_files = list((temp_dir / "conversations" / "u1").glob("*.json"))
        self.assertEqual(1, len(saved_files))
        payload = read_json(saved_files[0])
        self.assertEqual(2, len(payload["turns"]))
        self.assertEqual("private:u1", payload["dialogue_key"])
        self.assertEqual("", payload["closed_at"])
        await manager.close()

    async def test_flush_conversation_session_closes_current_session_before_next_turn(self):
        temp_dir = self.make_temp_dir()
        manager = self.build_manager(temp_dir, conversation_save_interval=5, auto_extract_memory=False)
        await manager.initialize()

        manager.register_dialogue_turn("u1", "???", "??")
        manager.flush_conversation_session(user_id="u1", message_type="private")
        await manager.flush_background_tasks()

        manager.register_dialogue_turn("u1", "???", "??")
        await manager.flush_background_tasks()

        saved_files = sorted((temp_dir / "conversations" / "u1").glob("*.json"))
        self.assertEqual(2, len(saved_files))
        payloads = [read_json(file_path) for file_path in saved_files]
        self.assertEqual([1, 1], sorted(len(payload["turns"]) for payload in payloads))
        self.assertEqual(1, sum(1 for payload in payloads if payload["closed_at"]))
        self.assertEqual(1, sum(1 for payload in payloads if not payload["closed_at"]))
        await manager.close()

    async def test_anchor_context_loads_hit_range_with_neighbor_turns(self):
        temp_dir = self.make_temp_dir()
        manager = self.build_manager(temp_dir, conversation_save_interval=10, auto_extract_memory=False)
        await manager.initialize()

        manager.register_dialogue_turn("u1", "???", "???")
        manager.register_dialogue_turn("u1", "??? ?????", "???")
        manager.register_dialogue_turn("u1", "???", "???")
        manager.register_dialogue_turn("u1", "???", "???")
        await manager.flush_background_tasks()

        session_file = next((temp_dir / "conversations" / "u1").glob("*.json"))
        session_payload = read_json(session_file)
        session_id = session_payload["session_id"]

        await manager.add_memory(
            content="?????",
            user_id="u1",
            source="test",
            metadata={
                "owner_user_id": "u1",
                "source_session_id": session_id,
                "source_dialogue_key": "private:u1",
                "source_turn_start": 2,
                "source_turn_end": 2,
                "source_message_ids": [session_payload["turns"][1]["source_message_id"]],
                "source_message_type": "private",
                "source_group_id": "",
                "group_id": "",
            },
        )
        await manager.rebuild_index("u1")

        result = await manager.search_memories_with_context(user_id="u1", query="?????", top_k=3)

        self.assertTrue(result["history_messages"])
        self.assertEqual(8, len(result["history_messages"]))
        self.assertEqual("???", result["history_messages"][0]["content"])
        self.assertEqual("??? ?????", result["history_messages"][2]["content"])
        self.assertEqual("???", result["history_messages"][-1]["content"])
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
