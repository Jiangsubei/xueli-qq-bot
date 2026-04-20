from __future__ import annotations

import unittest

from src.core.models import PromptPlan, TemporalContext
from src.handlers.conversation_timeline_formatter import ConversationTimelineFormatter


class ConversationTimelineFormatterTests(unittest.TestCase):
    def test_per_message_mode_renders_timeline_lines(self) -> None:
        formatter = ConversationTimelineFormatter()
        output = formatter.render_recent_history(
            window_messages=[
                {
                    "speaker_role": "user",
                    "speaker_name": "小王",
                    "user_id": "42",
                    "display_text": "我们周末去哪",
                    "event_time": 1713600000,
                    "is_latest": False,
                },
                {
                    "speaker_role": "assistant",
                    "speaker_name": "雪梨",
                    "display_text": "先看看天气",
                    "event_time": 1713600060,
                    "is_latest": False,
                },
            ],
            prompt_plan=PromptPlan(timeline_detail="per_message"),
            temporal_context=TemporalContext(summary_text="刚刚还在连续对话"),
            chat_mode="group",
        )

        self.assertIn("最近对话时间线", output)
        self.assertIn("小王(42)", output)
        self.assertIn("时间线观察", output)


if __name__ == "__main__":
    unittest.main()
