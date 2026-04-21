from __future__ import annotations

import unittest

from src.core.models import MessageEvent, MessagePlanAction, TemporalContext
from src.handlers.message_context import MessageContext
from src.handlers.prompt_planner import PromptPlanner


class PromptPlannerTests(unittest.TestCase):
    def test_default_prompt_plan_uses_rich_memory_for_old_topic_resume(self) -> None:
        planner = PromptPlanner()
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
        context = MessageContext(
            temporal_context=TemporalContext(
                continuity_hint="old_topic_resume",
            )
        )

        plan = planner.default_prompt_plan(
            event=event,
            action=MessagePlanAction.REPLY.value,
            context=context,
        )

        self.assertEqual(plan.reply_goal, "recall")
        self.assertEqual(plan.continuity_mode, "resume_old_topic")
        self.assertEqual(plan.timeline_detail, "per_message")
        self.assertEqual(plan.memory_profile, "rich")
        self.assertEqual(plan.tone_profile, "deep")
        self.assertEqual(plan.context_profile, "full")
        self.assertTrue(plan.policy.include_session_restore)
        self.assertTrue(plan.policy.include_precise_recall)

    def test_default_prompt_plan_falls_back_to_answer_for_private_greeting_without_signals(self) -> None:
        planner = PromptPlanner()
        event = MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "private",
                "message_id": 2,
                "user_id": 42,
                "self_id": 999,
                "raw_message": "你好呀",
                "message": [{"type": "text", "data": {"text": "你好呀"}}],
            }
        )
        context = MessageContext(
            planning_signals={},
            is_first_turn=True,
            temporal_context=TemporalContext(continuity_hint="unknown"),
        )

        plan = planner.default_prompt_plan(
            event=event,
            action=MessagePlanAction.REPLY.value,
            context=context,
        )

        self.assertEqual(plan.reply_goal, "answer")
        self.assertEqual(plan.continuity_mode, "resume_recent_topic")

    def test_default_prompt_plan_uses_continue_goal_for_follow_up(self) -> None:
        planner = PromptPlanner()
        event = MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "group",
                "message_id": 3,
                "user_id": 42,
                "group_id": 100,
                "self_id": 999,
                "raw_message": "然后呢",
                "message": [{"type": "text", "data": {"text": "然后呢"}}],
            }
        )
        context = MessageContext(planning_signals={"follows_assistant_recently": True})

        plan = planner.default_prompt_plan(
            event=event,
            action=MessagePlanAction.REPLY.value,
            context=context,
        )

        self.assertEqual(plan.reply_goal, "continue")
        self.assertEqual(plan.initiative, "gentle_follow")
        self.assertEqual(plan.expression_profile, "colloquial")

    def test_default_prompt_plan_keeps_private_first_turn_continuity_conservative(self) -> None:
        planner = PromptPlanner()
        event = MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "private",
                "message_id": 4,
                "user_id": 42,
                "self_id": 999,
                "raw_message": "早上好",
                "message": [{"type": "text", "data": {"text": "早上好"}}],
            }
        )
        context = MessageContext(
            is_first_turn=True,
            temporal_context=TemporalContext(continuity_hint="strong_continuation"),
        )

        plan = planner.default_prompt_plan(
            event=event,
            action=MessagePlanAction.REPLY.value,
            context=context,
        )

        self.assertEqual(plan.reply_goal, "answer")
        self.assertEqual(plan.continuity_mode, "resume_recent_topic")

    def test_parse_reply_reference_reads_soft_guidance_text(self) -> None:
        planner = PromptPlanner()
        event = MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "private",
                "message_id": 5,
                "user_id": 42,
                "self_id": 999,
                "raw_message": "早上好",
                "message": [{"type": "text", "data": {"text": "早上好"}}],
            }
        )

        reference = planner.parse_reply_reference(
            {
                "action": "reply",
                "reply_reference": "简单回一句早安，轻一点，不要马上追问太多。",
            },
            event=event,
            action=MessagePlanAction.REPLY.value,
            context=None,
        )

        self.assertEqual(reference, "简单回一句早安，轻一点，不要马上追问太多。")


if __name__ == "__main__":
    unittest.main()
