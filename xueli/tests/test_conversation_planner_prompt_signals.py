from __future__ import annotations

import unittest

from src.core.models import MessageEvent, TemporalContext
from src.handlers.conversation_planner import ConversationPlanner
from src.handlers.message_context import MessageContext


class ConversationPlannerPromptSignalTests(unittest.TestCase):
    def test_user_prompt_exposes_temporal_buckets_as_signals(self) -> None:
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

        self.assertIn("最近消息时间分层：short_resume", prompt)
        self.assertIn("上一轮会话时间分层：long_resume", prompt)
        self.assertIn("连续性信号标签：old_topic_resume", prompt)
        self.assertNotIn("更积极地考虑", prompt)

    def test_user_prompt_exposes_companionship_hints_when_signals_exist(self) -> None:
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
                "continuation_cue_detected": True,
                "follow_up_after_assistant": True,
            },
        )

        prompt = planner._build_user_prompt(
            event,
            user_message="然后呢",
            recent_messages=[],
            context=context,
        )

        self.assertIn("陪伴倾向提醒", prompt)
        self.assertIn("更积极地考虑延续同一话题", prompt)
        self.assertIn("顺着你刚刚的回复继续聊", prompt)


if __name__ == "__main__":
    unittest.main()
