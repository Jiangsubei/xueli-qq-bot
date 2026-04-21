from __future__ import annotations

import unittest

from src.core.models import MessageEvent, PromptPlan, TemporalContext
from src.handlers.message_context import MessageContext
from src.handlers.reply_prompt_renderer import ReplyPromptRenderer


class _Host:
    def _build_assistant_identity_prompt(self) -> str:
        return "你是雪梨。"

    def _build_system_prompt(self) -> str:
        return "自然聊天。"


class ReplyPromptTemporalReferenceTests(unittest.TestCase):
    def test_continuity_section_exposes_session_gap_and_planner_reference(self) -> None:
        renderer = ReplyPromptRenderer(_Host())
        event = MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "private",
                "message_id": 88,
                "user_id": 42,
                "self_id": 999,
                "raw_message": "早上好",
                "message": [{"type": "text", "data": {"text": "早上好"}}],
            }
        )
        context = MessageContext(
            current_sender_label="42（测试用户）",
            reply_reference="简单回一句早安，轻一点，不要马上追问太多。",
            temporal_context=TemporalContext(
                recent_gap_bucket="immediate",
                conversation_gap_bucket="immediate",
                session_gap_bucket="long_resume",
                continuity_hint="resume_after_break",
                summary_text="最近一条历史消息时间分层是 immediate；上一轮已关闭会话的时间分层是 long_resume",
            ),
        )

        rendered = renderer.render(
            event=event,
            message_context=context,
            prompt_plan=PromptPlan(),
            current_message="早上好",
            planner_reason="适合直接回应",
        )

        self.assertIn("最近消息时间分层：immediate", rendered.system_prompt)
        self.assertIn("上一轮会话时间分层：long_resume", rendered.system_prompt)
        self.assertIn("连续性标签：resume_after_break", rendered.system_prompt)
        self.assertIn("上一轮会话时间语义：", rendered.system_prompt)
        self.assertIn("重新接上", rendered.system_prompt)
        self.assertIn("规划参考：", rendered.system_prompt)
        self.assertIn("简单回一句早安", rendered.system_prompt)
        self.assertIn("不要照抄", rendered.system_prompt)


if __name__ == "__main__":
    unittest.main()
