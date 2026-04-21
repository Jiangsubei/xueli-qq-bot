from __future__ import annotations

import unittest

from src.handlers.conversation_engagement import (
    build_message_observations,
    normalize_engagement_text,
    _is_light_response_candidate,
    _is_continuation_candidate,
    _message_length_bucket,
)


class ConversationEngagementTests(unittest.TestCase):
    def test_normalize_engagement_text_strips_whitespace_and_lower(self) -> None:
        self.assertEqual(normalize_engagement_text("  你好呀  "), "你好呀")
        self.assertEqual(normalize_engagement_text("Hello  World"), "helloworld")

    def test_message_length_bucket_classifies_correctly(self) -> None:
        self.assertEqual(_message_length_bucket("hi"), "ultra_short")
        self.assertEqual(_message_length_bucket("你好"), "ultra_short")
        self.assertEqual(_message_length_bucket("今天天气不错"), "short")
        self.assertEqual(_message_length_bucket("今天天气真是不错，阳光明媚，适合出去走走"), "medium")

    def test_is_light_response_candidate_detects_short_tokens(self) -> None:
        self.assertTrue(_is_light_response_candidate("嗯"))
        self.assertTrue(_is_light_response_candidate("嗯嗯"))
        self.assertTrue(_is_light_response_candidate("好哦"))
        self.assertFalse(_is_light_response_candidate("今天心情不错"))
        self.assertFalse(_is_light_response_candidate(""))

    def test_is_continuation_candidate_detects_continuation_tokens(self) -> None:
        self.assertTrue(_is_continuation_candidate("然后呢"))
        self.assertTrue(_is_continuation_candidate("继续说吧"))
        self.assertTrue(_is_continuation_candidate("结果怎么样了"))
        self.assertFalse(_is_continuation_candidate("你好呀今天怎么样"))
        self.assertFalse(_is_continuation_candidate(""))

    def test_build_message_observations_returns_neutral_fields_only(self) -> None:
        obs = build_message_observations(
            "你好呀",
            current_user_id="123",
            previous_speaker_role="assistant",
            previous_user_id="123",
            recent_gap_bucket="immediate",
            recent_history_count=2,
        )

        self.assertIn("message_length_bucket", obs)
        self.assertIn("is_short_message", obs)
        self.assertIn("is_light_response_candidate", obs)
        self.assertIn("is_continuation_candidate", obs)
        self.assertIn("assistant_replied_recently", obs)
        self.assertIn("follows_assistant_recently", obs)
        self.assertIn("same_user_continuation", obs)
        self.assertIn("recent_history_count", obs)
        self.assertIn("latest_message_length", obs)

        self.assertNotIn("care_cue_detected", obs)
        self.assertNotIn("continuation_cue_detected", obs)

    def test_build_message_observations_short_message_after_assistant(self) -> None:
        obs = build_message_observations(
            "好的",
            current_user_id="123",
            previous_speaker_role="assistant",
            previous_user_id="456",
            recent_gap_bucket="very_recent",
            recent_history_count=1,
        )

        self.assertTrue(obs["assistant_replied_recently"])
        self.assertTrue(obs["follows_assistant_recently"])
        self.assertTrue(obs["is_short_message"])
        self.assertEqual(obs["message_length_bucket"], "ultra_short")

    def test_build_message_observations_no_signal_when_gap_is_long(self) -> None:
        obs = build_message_observations(
            "你好",
            current_user_id="123",
            previous_speaker_role="assistant",
            previous_user_id="456",
            recent_gap_bucket="long_resume",
            recent_history_count=5,
        )

        self.assertFalse(obs["assistant_replied_recently"])
        self.assertFalse(obs["follows_assistant_recently"])

    def test_build_message_observations_same_user_continuation(self) -> None:
        obs = build_message_observations(
            "继续说",
            current_user_id="123",
            previous_speaker_role="user",
            previous_user_id="123",
            recent_gap_bucket="immediate",
            recent_history_count=3,
        )

        self.assertTrue(obs["same_user_continuation"])
        self.assertTrue(obs["is_continuation_candidate"])

    def test_build_message_observations_continuation_candidate(self) -> None:
        obs = build_message_observations(
            "然后呢结果是什么",
            current_user_id="123",
            previous_speaker_role="assistant",
            previous_user_id="456",
            recent_gap_bucket="recent",
            recent_history_count=1,
        )

        self.assertTrue(obs["is_continuation_candidate"])
        self.assertTrue(obs["follows_assistant_recently"])
        self.assertFalse(obs["is_light_response_candidate"])


if __name__ == "__main__":
    unittest.main()
