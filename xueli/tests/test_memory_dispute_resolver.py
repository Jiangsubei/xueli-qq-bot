from __future__ import annotations

import unittest

from src.core.config import MemoryDisputeConfig
from src.memory.extraction.memory_extractor import MemoryReflectionResult
from src.memory.memory_dispute_resolver import MemoryDisputeResolver


class MemoryDisputeResolverTests(unittest.TestCase):
    def test_high_confidence_conflict_maps_to_high_confidence(self) -> None:
        resolver = MemoryDisputeResolver(MemoryDisputeConfig())
        decision = resolver.resolve(
            MemoryReflectionResult(
                has_conflict=True,
                action="prefer_new",
                confidence=0.9,
                summary="用户偏好发生变化",
                reason="新证据更明确",
            )
        )

        self.assertEqual(decision.level, "high_confidence")
        self.assertEqual(decision.action, "prefer_new")

    def test_low_confidence_or_missing_action_is_ignored(self) -> None:
        resolver = MemoryDisputeResolver(MemoryDisputeConfig())
        decision = resolver.resolve(
            MemoryReflectionResult(
                has_conflict=True,
                action="",
                confidence=0.2,
            )
        )

        self.assertEqual(decision.level, "ignore")


if __name__ == "__main__":
    unittest.main()
