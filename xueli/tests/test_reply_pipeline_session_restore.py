from __future__ import annotations

import asyncio
import sys
import types
import unittest


if "aiohttp" not in sys.modules:
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientError = Exception
    aiohttp.ClientTimeout = object
    aiohttp.ClientSession = object
    sys.modules["aiohttp"] = aiohttp

from src.handlers.message_context import MessageContext
from src.handlers.reply_pipeline import ReplyPipeline


class _FakeHost:
    def _build_assistant_identity_text(self) -> str:
        return "你是雪梨。"

    def _build_assistant_identity_prompt(self) -> str:
        return "assistant"

    def _build_system_prompt(self) -> str:
        return "system"

    @property
    def app_config(self):
        class _FakeConfig:
            personality = type("C", (), {"content": "性格：活泼。"})()
            dialogue_style = type("C", (), {"content": "风格：简短。"})()
            behavior = type("C", (), {"content": "约束：不主动。"})()
        return _FakeConfig()


class ReplyPipelineSessionRestorePromptTests(unittest.TestCase):
    def test_system_prompt_includes_session_restore_block(self) -> None:
        pipeline = ReplyPipeline(_FakeHost())

        prompt = asyncio.run(pipeline.build_response_system_prompt(
            event=None,
            message_context=MessageContext(
                user_message="继续聊这个",
                person_fact_context="1. 用户正在写毕业论文",
                persistent_memory_context="1. 用户喜欢简洁回答",
                session_restore_context="1. 上一轮会话（2轮）：用户刚开始写毕业论文",
                precise_recall_context='1. 第一次提到相关话题（第2轮）：用户说"准备写毕业论文"',
                dynamic_memory_context="",
                is_first_turn=True,
            ),
        ))

        self.assertIn("[人格事实]", prompt)
        self.assertIn("用户刚开始写毕业论文", prompt)
        self.assertIn("[精确召回]", prompt)


if __name__ == "__main__":
    unittest.main()
