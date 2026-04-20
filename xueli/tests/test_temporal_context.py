from __future__ import annotations

import unittest

from src.handlers.temporal_context import build_temporal_context


class TemporalContextTests(unittest.TestCase):
    def test_build_temporal_context_marks_recent_continuation(self) -> None:
        ctx = build_temporal_context(
            current_event_time=1_000.0,
            previous_message_time=950.0,
            conversation_last_time=950.0,
            history_event_times=[900.0, 950.0, 1_000.0],
        )

        self.assertEqual(ctx.recent_gap_bucket, "immediate")
        self.assertEqual(ctx.continuity_hint, "strong_continuation")
        self.assertIn("几乎连在一起", ctx.summary_text)

    def test_build_temporal_context_marks_old_topic_resume(self) -> None:
        ctx = build_temporal_context(
            current_event_time=900_000.0,
            previous_message_time=100_000.0,
            conversation_last_time=100_000.0,
        )

        self.assertEqual(ctx.recent_gap_bucket, "stale")
        self.assertEqual(ctx.continuity_hint, "old_topic_resume")
        self.assertIn("间隔较久", ctx.summary_text)

    def test_summary_is_observational_not_instructional(self) -> None:
        ctx = build_temporal_context(
            current_event_time=10_000.0,
            previous_message_time=9_000.0,
            conversation_last_time=9_000.0,
            history_event_times=[8_800.0, 9_000.0, 10_000.0],
        )

        self.assertNotIn("更像是", ctx.summary_text)
        self.assertNotIn("应当", ctx.summary_text)

    def test_group_thresholds_are_more_sensitive_than_private(self) -> None:
        private_ctx = build_temporal_context(
            current_event_time=1_000.0,
            previous_message_time=1_000.0 - 240.0,
            chat_mode="private",
        )
        group_ctx = build_temporal_context(
            current_event_time=1_000.0,
            previous_message_time=1_000.0 - 240.0,
            chat_mode="group",
        )

        self.assertEqual(private_ctx.recent_gap_bucket, "very_recent")
        self.assertEqual(group_ctx.recent_gap_bucket, "recent")


if __name__ == "__main__":
    unittest.main()
