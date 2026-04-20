from __future__ import annotations

import unittest

from src.memory.internal.access_policy import MemoryAccessPolicy
from src.memory.storage.markdown_store import MemoryItem
from src.webui.console.services import _serialize_memory_item


class ConsoleMemoryPayloadTests(unittest.TestCase):
    def test_serialize_memory_item_includes_reflection_and_evolution_fields(self) -> None:
        item = MemoryItem(
            id="mem-1",
            content="用户现在不想喝咖啡",
            source="extraction",
            metadata={
                "summary": "用户以前表达过喜欢咖啡，但最近表示当前不想喝咖啡，更像阶段性状态。",
                "source_session_id": "session-1",
                "source_dialogue_key": "private:u1",
                "source_turn_start": 3,
                "source_turn_end": 3,
                "source_message_type": "private",
                "source_message_ids": ["msg-3"],
                "patch_status": "active_patch",
                "patch_action": "keep_both_prefer_recent",
                "patch_conflict_type": "temporary_state",
                "patch_reason": "近期表达更应优先理解",
                "patch_confidence": 0.83,
                "patch_target_memory_ids": ["mem-old"],
                "reflection": {
                    "has_conflict": True,
                    "conflict_type": "temporary_state",
                    "action": "keep_both_prefer_recent",
                    "summary": "用户以前表达过喜欢咖啡，但最近表示当前不想喝咖啡，更像阶段性状态。",
                    "reason": "两条记忆围绕同一主题但一个明显带有近期状态。",
                    "confidence": 0.83,
                    "reflected_at": "2026-04-19T12:00:00",
                },
                "source_observations": [
                    {
                        "session_id": "session-1",
                        "dialogue_key": "private:u1",
                        "turn_start": 3,
                        "turn_end": 3,
                        "message_type": "private",
                        "group_id": "",
                        "recorded_at": "2026-04-19T11:58:00",
                    }
                ],
            },
            owner_user_id="u1",
        )

        payload = _serialize_memory_item(item, kind="ordinary", access_policy=MemoryAccessPolicy())

        self.assertEqual(payload["summary"], "用户以前表达过喜欢咖啡，但最近表示当前不想喝咖啡，更像阶段性状态。")
        self.assertEqual(payload["source_session_id"], "session-1")
        self.assertEqual(payload["source_turn_start"], 3)
        self.assertEqual(payload["patch_status"], "active_patch")
        self.assertEqual(payload["patch_action"], "keep_both_prefer_recent")
        self.assertEqual(payload["patch_target_memory_ids"], ["mem-old"])
        self.assertIn("reflection", payload)
        self.assertEqual(len(payload["evolution"]), 2)
        self.assertEqual(payload["evolution"][0]["kind"], "observation")
        self.assertEqual(payload["evolution"][1]["kind"], "reflection")


if __name__ == "__main__":
    unittest.main()
