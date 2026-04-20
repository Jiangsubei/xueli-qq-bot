from __future__ import annotations

import sys
import types
import unittest


if "aiohttp" not in sys.modules:
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientError = Exception
    aiohttp.ClientTimeout = object
    aiohttp.ClientSession = object
    sys.modules["aiohttp"] = aiohttp

from src.handlers.reply_pipeline import ReplyPipeline


class _FakeHost:
    def _build_assistant_identity_prompt(self) -> str:
        return "assistant"

    def _build_system_prompt(self) -> str:
        return "system"


class ReplyPipelineSessionRestorePromptTests(unittest.TestCase):
    def test_system_prompt_includes_session_restore_block(self) -> None:
        pipeline = ReplyPipeline(_FakeHost())

        prompt = pipeline.build_response_system_prompt(
            event=None,
            person_fact_context="1. 用户正在写毕业论文",
            persistent_memory_context="1. 用户喜欢简洁回答",
            session_restore_context="1. 上一轮会话（2轮）：用户刚开始写毕业论文",
            precise_recall_context="1. 第一次提到相关话题（第2轮）：用户说“准备写毕业论文”",
            dynamic_memory_context="",
            is_first_turn=True,
            current_message="继续聊这个",
        )

        self.assertIn("这些是当前用户的长期事实", prompt)
        self.assertIn("这是上一轮相关会话的恢复摘要", prompt)
        self.assertIn("这是和当前话题直接相关的旧对话定位", prompt)
        self.assertIn("用户刚开始写毕业论文", prompt)


if __name__ == "__main__":
    unittest.main()
