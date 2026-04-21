from __future__ import annotations

import unittest

from src.core.models import MessageEvent, TemporalContext
from src.handlers.conversation_planner import ConversationPlanner
from src.handlers.message_context import MessageContext


class ConversationPlannerPromptSignalTests(unittest.TestCase):
    def test_user_prompt_excludes_temporal_buckets(self) -> None:
        planner = ConversationPlanner(ai_client=object())
        event = MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "private",
                "message_id": 30,
                "user_id": 42,
                "self_id": 999,
                "raw_message": "继续",
                "message": [{"type": "text", "data": {"text": "继续"}}],
                "sender": {"nickname": "Private User"},
            }
        )
        context = MessageContext(
            current_sender_label="42（Private User）",
            recent_history_text="最近历史",
            temporal_context=TemporalContext(
                recent_gap_bucket="short_resume",
                conversation_gap_bucket="short_resume",
                session_gap_bucket="long_resume",
                continuity_hint="old_topic_resume",
                summary_text="当前消息和最近一条上下文消息之间已经间隔较久；上一轮已关闭会话的时间分层是 long_resume",
            ),
        )

        prompt = planner._build_user_prompt(
            event,
            user_message="继续",
            recent_messages=[],
            context=context,
        )

        # No code-level temporal buckets in prompt
        self.assertNotIn("最近消息时间分层", prompt)
        self.assertNotIn("连续性标签", prompt)
        self.assertNotIn("上一轮会话时间", prompt)
        # Model judges continuity itself from timestamps in history text
        self.assertIn("最近历史", prompt)

    def test_user_prompt_excludes_observation_signals(self) -> None:
        planner = ConversationPlanner(ai_client=object())
        event = MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "group",
                "message_id": 31,
                "user_id": 42,
                "group_id": 100,
                "self_id": 999,
                "raw_message": "然后呢",
                "message": [{"type": "text", "data": {"text": "然后呢"}}],
                "sender": {"card": "Group User"},
            }
        )
        context = MessageContext(
            current_sender_label="42（Group User）",
            recent_history_text="最近历史",
            planning_signals={
                "is_continuation_candidate": True,
                "follows_assistant_recently": True,
                "message_length_bucket": "short",
            },
        )

        prompt = planner._build_user_prompt(
            event,
            user_message="然后呢",
            recent_messages=[],
            context=context,
        )

        # No code-level signals passed to planner — model decides from timestamped history
        self.assertNotIn("is_continuation_candidate", prompt)
        self.assertNotIn("follows_assistant_recently", prompt)
        self.assertNotIn("运行时观察信号", prompt)

    def test_parse_plan_keeps_reply_reference(self) -> None:
        planner = ConversationPlanner(ai_client=object())
        event = MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "private",
                "message_id": 32,
                "user_id": 42,
                "self_id": 999,
                "raw_message": "早上好",
                "message": [{"type": "text", "data": {"text": "早上好"}}],
            }
        )

        plan = planner._parse_plan(
            '{"action":"reply","reason":"适合直接回应","reply_reference":"简单回一句早安，轻一点，不要马上追问太多。","prompt_plan":{"reply_goal":"answer","continuity_mode":"resume_recent_topic","timeline_detail":"summary","context_profile":"standard","memory_profile":"off","tone_profile":"balanced","initiative":"reactive","expression_profile":"plain","policy":{"include_recent_history":true,"include_person_facts":false,"include_session_restore":false,"include_precise_recall":false,"include_dynamic_memory":false,"include_vision_context":true,"include_reply_scope":true,"include_style_guide":true},"notes":"简单回早安即可。"}}',
            event=event,
            context=None,
        )

        self.assertEqual(plan.reply_reference, "简单回一句早安，轻一点，不要马上追问太多。")


if __name__ == "__main__":
    unittest.main()
