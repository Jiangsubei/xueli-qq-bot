from __future__ import annotations

import unittest

from src.core.models import CharacterCardSnapshot, PromptPlan, SoftUncertaintySignal, TemporalContext
from src.handlers.reply_style_policy import ReplyStylePolicy


class ReplyStylePolicyTests(unittest.TestCase):
    def test_comfort_goal_prefers_warm_guidance(self) -> None:
        policy = ReplyStylePolicy()
        guide = policy.build(
            prompt_plan=PromptPlan(reply_goal="comfort", tone_profile="warm", expression_profile="companion"),
            temporal_context=TemporalContext(),
            chat_mode="private",
            planner_reason="对方状态不太好",
            planning_signals={"care_cue_detected": True},
        )

        self.assertIn("先轻轻接住", guide.warmth_guidance)
        self.assertIn("模板化卖萌", " ".join(guide.anti_patterns))

    def test_recall_goal_mentions_natural_memory_tone(self) -> None:
        policy = ReplyStylePolicy()
        guide = policy.build(
            prompt_plan=PromptPlan(reply_goal="recall", tone_profile="deep"),
            temporal_context=TemporalContext(continuity_hint="old_topic_resume"),
            chat_mode="private",
        )

        self.assertIn("自然想起", guide.tone_guidance)

    def test_soft_uncertainty_and_character_snapshot_adjust_expression(self) -> None:
        policy = ReplyStylePolicy()
        guide = policy.build(
            prompt_plan=PromptPlan(reply_goal="continue", tone_profile="balanced"),
            temporal_context=TemporalContext(),
            chat_mode="private",
            soft_uncertainty_signals=[
                SoftUncertaintySignal(signal_id="sig-1", user_id="42", summary="用户偏好可能发生变化", confidence=0.91)
            ],
            character_card_snapshot=CharacterCardSnapshot(
                user_id="42",
                tone_preferences=["偏好更短一点"],
                behavior_habits=["少一点主动追问"],
            ),
        )

        self.assertIn("留有余地", guide.tone_guidance)
        self.assertIn("偏好更短一点", guide.expression_guidance)
        self.assertIn("谨慎", guide.initiative_guidance)
        self.assertTrue(guide.opening_style)
        self.assertTrue(guide.sentence_shape)
        self.assertTrue(guide.followup_shape)
        self.assertTrue(guide.allowed_colloquialism)


if __name__ == "__main__":
    unittest.main()
