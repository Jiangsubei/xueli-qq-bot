from __future__ import annotations

import time
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

    def test_missing_time_fields_fallback_to_arrival_time(self) -> None:
        """模拟 NapCat 未发 time 字段时，两消息都用 time.time() 作为 fallback 的情况。"""
        now = time.time()
        # 消息1：到达时间 now
        # 消息2：3秒后到达，到达时间 now + 3
        ctx = build_temporal_context(
            current_event_time=now + 3,
            previous_message_time=now,
            conversation_last_time=now,
        )
        # 3秒 gap，私聊 < 60s → immediate → strong_continuation
        self.assertEqual(ctx.recent_gap_bucket, "immediate")
        self.assertEqual(ctx.continuity_hint, "strong_continuation")

    def test_missing_time_fields_30s_gap_still_strong_continuation(self) -> None:
        """30秒 gap，私聊阈值 60s，仍然是 strong_continuation。"""
        now = time.time()
        ctx = build_temporal_context(
            current_event_time=now + 30,
            previous_message_time=now,
            conversation_last_time=now,
        )
        self.assertEqual(ctx.recent_gap_bucket, "immediate")
        self.assertEqual(ctx.continuity_hint, "strong_continuation")

    def test_missing_time_fields_90s_gap_still_strong_continuation(self) -> None:
        """90秒 gap，私聊 very_recent(<600s) → strong_continuation。"""
        now = time.time()
        ctx = build_temporal_context(
            current_event_time=now + 90,
            previous_message_time=now,
            conversation_last_time=now,
        )
        self.assertEqual(ctx.recent_gap_bucket, "very_recent")
        self.assertEqual(ctx.continuity_hint, "strong_continuation")

    def test_missing_time_fields_700s_gap_becomes_soft_continuation(self) -> None:
        """700秒(≈12分钟) gap，私聊 very_recent 上界 600s → recent → soft_continuation。"""
        now = time.time()
        ctx = build_temporal_context(
            current_event_time=now + 700,
            previous_message_time=now,
            conversation_last_time=now,
        )
        self.assertEqual(ctx.recent_gap_bucket, "recent")
        self.assertEqual(ctx.continuity_hint, "soft_continuation")

    def test_zero_previous_time_gives_unknown_bucket(self) -> None:
        """previous_message_time = 0 表示没有历史时间，bucket 应为 unknown。"""
        ctx = build_temporal_context(
            current_event_time=1_000.0,
            previous_message_time=0.0,
            conversation_last_time=0.0,
        )
        self.assertEqual(ctx.recent_gap_bucket, "unknown")
        self.assertEqual(ctx.continuity_hint, "unknown")


if __name__ == "__main__":
    unittest.main()
