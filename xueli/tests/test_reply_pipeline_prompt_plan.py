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

from src.core.models import PromptSectionPolicy, PromptPlan, TemporalContext
from src.handlers.reply_pipeline import ReplyPipeline


class _FakeHost:
    def _build_assistant_identity_prompt(self) -> str:
        return "assistant"

    def _build_system_prompt(self) -> str:
        return "system"


class ReplyPipelinePromptPlanTests(unittest.TestCase):
    def test_prompt_plan_controls_temporal_and_reply_scope_layers(self) -> None:
        pipeline = ReplyPipeline(_FakeHost())
        prompt_plan = PromptPlan(
            reply_goal="recall",
            continuity_mode="resume_old_topic",
            timeline_detail="per_message",
            tone_profile="deep",
            context_profile="full",
            expression_profile="colloquial",
            policy=PromptSectionPolicy(
                include_recent_history=False,
                include_person_facts=True,
                include_session_restore=False,
                include_precise_recall=False,
                include_dynamic_memory=False,
                include_vision_context=False,
                include_reply_scope=False,
                include_style_guide=True,
            ),
        )
        temporal_context = TemporalContext(
            recent_gap_bucket="short_resume",
            continuity_hint="old_topic_resume",
            summary_text="当前消息和最近上下文之间间隔较长，更像是在隔了一段时间后重提旧话题。",
        )

        prompt = pipeline.build_response_system_prompt(
            event=None,
            person_fact_context="1. 用户长期在准备考研",
            persistent_memory_context="",
            session_restore_context="1. 上一轮会话摘要",
            precise_recall_context="1. 更早旧对话定位",
            dynamic_memory_context="1. 动态记忆",
            is_first_turn=False,
            recent_history_text="最近历史",
            current_message="继续聊这个",
            temporal_context=temporal_context,
            prompt_plan=prompt_plan,
        )

        self.assertIn("回复目标：recall", prompt)
        self.assertIn("连续性策略", prompt)
        self.assertIn("resume_old_topic", prompt)
        self.assertIn("最终回复风格", prompt)
        self.assertIn("输出格式要求", prompt)
        self.assertIn("JSON 字符串数组", prompt)
        self.assertIn("自然想起之前聊过的事", prompt)
        self.assertIn("适度展开", prompt)
        self.assertIn("用户长期在准备考研", prompt)
        self.assertIn("时间线信息", prompt)
        self.assertNotIn("上一轮会话摘要", prompt)
        self.assertNotIn("更早旧对话定位", prompt)
        self.assertNotIn("动态记忆", prompt)
        self.assertNotIn("回复范围：", prompt)


if __name__ == "__main__":
    unittest.main()
