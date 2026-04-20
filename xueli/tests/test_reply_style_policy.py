from __future__ import annotations

import unittest

from src.core.models import PromptPlan, TemporalContext
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


if __name__ == "__main__":
    unittest.main()
