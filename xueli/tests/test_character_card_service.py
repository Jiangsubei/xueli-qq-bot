from __future__ import annotations

import tempfile
import unittest

from src.core.config import CharacterGrowthConfig
from src.handlers.character_card_service import CharacterCardService


class CharacterCardServiceTests(unittest.TestCase):
    def test_explicit_feedback_and_stable_signals_refresh_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = CharacterCardService(
                temp_dir,
                CharacterGrowthConfig(
                    explicit_feedback_threshold=1,
                    stable_signal_threshold=2,
                    core_trait_threshold=1,
                    tone_preference_threshold=1,
                    behavior_habit_threshold=1,
                ),
            )
            service.record_explicit_feedback("42", "你可以温柔一点，别那么冲")
            service.record_explicit_feedback("42", "回复短一点")
            service.record_interaction_signal("42", "private_continue")
            service.record_interaction_signal("42", "private_continue")

            snapshot = service.refresh_snapshot("42")

            self.assertIn("更注重温和承接", snapshot.core_traits)
            self.assertIn("偏好更短一点", snapshot.tone_preferences)
            self.assertIn("私聊里可以更自然续接", snapshot.tone_preferences)


if __name__ == "__main__":
    unittest.main()
