from __future__ import annotations

import asyncio
import sys
import types
import unittest
from types import SimpleNamespace


if "aiohttp" not in sys.modules:
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientError = Exception
    aiohttp.ClientTimeout = object
    aiohttp.ClientSession = object
    sys.modules["aiohttp"] = aiohttp

from src.core.models import Conversation, MessageEvent, PromptSectionPolicy, PromptPlan
from src.handlers.reply_pipeline import ReplyPipeline


class _FakeMemoryManager:
    def __init__(self) -> None:
        self.person_fact_calls = 0
        self.last_include_conversations = None
        self.last_include_sections = None
        self.last_section_intensity = None
        self.last_top_k = None

    async def format_person_facts_for_prompt(self, **kwargs):
        self.person_fact_calls += 1
        return "1. person fact"

    async def get_important_memories(self, **kwargs):
        return []

    async def search_memories_with_context(self, **kwargs):
        self.last_include_conversations = kwargs.get("include_conversations")
        self.last_include_sections = dict(kwargs.get("include_sections") or {})
        self.last_section_intensity = dict(kwargs.get("section_intensity") or {})
        self.last_top_k = kwargs.get("top_k")
        return {
            "memories": [{"content": "dynamic memory"}],
            "history_messages": [{"role": "assistant", "content": "history"}],
            "session_restore": [{"content": "session restore"}],
            "precise_recall": [{"content": "precise recall"}],
        }

    def build_access_context(self, **kwargs):
        return SimpleNamespace(read_scope="user", message_type=kwargs.get("message_type"), group_id=kwargs.get("group_id"))


class _FakeHost:
    def __init__(self, memory_manager):
        self.memory_manager = memory_manager
        self.app_config = SimpleNamespace(memory=SimpleNamespace(read_scope="user"), bot_behavior=SimpleNamespace(log_full_prompt=False))


class ReplyPipelineMemoryLayerPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_load_memory_context_honors_prompt_plan_layer_flags(self) -> None:
        memory_manager = _FakeMemoryManager()
        pipeline = ReplyPipeline(_FakeHost(memory_manager))
        event = MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "private",
                "message_id": 1,
                "user_id": 42,
                "self_id": 999,
                "raw_message": "继续",
                "message": [{"type": "text", "data": {"text": "继续"}}],
            }
        )
        prompt_plan = PromptPlan(
            memory_profile="off",
            policy=PromptSectionPolicy(
                include_recent_history=False,
                include_person_facts=False,
                include_session_restore=False,
                include_precise_recall=False,
                include_dynamic_memory=False,
                include_vision_context=False,
                include_reply_scope=False,
                include_style_guide=False,
            )
        )

        person_fact_context, _, session_restore_context, precise_recall_context, dynamic_memory_context, history_messages, _ = await pipeline.load_memory_context(
            event=event,
            user_message="继续",
            conversation=Conversation(),
            prompt_plan=prompt_plan,
        )

        self.assertEqual(person_fact_context, "")
        self.assertEqual(session_restore_context, "")
        self.assertEqual(precise_recall_context, "")
        self.assertEqual(dynamic_memory_context, "")
        self.assertEqual(history_messages, [])
        self.assertEqual(memory_manager.person_fact_calls, 0)
        self.assertFalse(memory_manager.last_include_conversations)
        self.assertEqual(memory_manager.last_include_sections, {
            "session_restore": False,
            "precise_recall": False,
            "dynamic": False,
        })
        self.assertEqual(memory_manager.last_section_intensity, {
            "session_restore": "off",
            "precise_recall": "off",
            "dynamic": "off",
        })

    async def test_context_profile_changes_memory_top_k(self) -> None:
        memory_manager = _FakeMemoryManager()
        pipeline = ReplyPipeline(_FakeHost(memory_manager))
        event = MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "private",
                "message_id": 1,
                "user_id": 42,
                "self_id": 999,
                "raw_message": "继续",
                "message": [{"type": "text", "data": {"text": "继续"}}],
            }
        )
        prompt_plan = PromptPlan(context_profile="full")

        await pipeline.load_memory_context(
            event=event,
            user_message="继续",
            conversation=Conversation(),
            prompt_plan=prompt_plan,
        )

        self.assertEqual(memory_manager.last_top_k, 7)


if __name__ == "__main__":
    unittest.main()
