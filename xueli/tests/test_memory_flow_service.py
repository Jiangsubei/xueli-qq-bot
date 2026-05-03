from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from src.handlers.reply_pipeline import PreparedReplyRequest
from src.memory.memory_flow_service import MemoryFlowService


class _MemoryManagerStub:
    def __init__(self) -> None:
        self.turns = []
        self.scheduled = []

    def register_dialogue_turn(self, **kwargs):
        self.turns.append(dict(kwargs))

    def schedule_memory_extraction(self, user_id: str, **kwargs):
        self.scheduled.append({"user_id": user_id, **kwargs})


class MemoryFlowServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_on_reply_generated_enqueues_and_drains(self) -> None:
        manager = _MemoryManagerStub()
        service = MemoryFlowService(manager)
        await service.start()
        prepared = PreparedReplyRequest(
            original_user_message="我们继续聊周末安排",
            model_user_message="我们继续聊周末安排",
            history_user_message="我们继续聊周末安排",
            system_prompt="system",
            base64_images=[],
            conversation=SimpleNamespace(add_message=lambda *args, **kwargs: None),
            related_history_messages=[],
            messages=[],
            active_sections=[],
            message_context=None,
        )
        host = SimpleNamespace(_get_conversation_key=lambda event: "qq:private:42")
        event = SimpleNamespace(user_id=42, group_id=None, message_type="private", message_id=1001)

        service.on_reply_generated(host=host, event=event, prepared=prepared, reply_text="那我们继续定周末安排。")
        await service._drain_queue_for_tests()

        self.assertEqual(len(manager.turns), 1)
        self.assertEqual(len(manager.scheduled), 1)

        await service.close()


if __name__ == "__main__":
    unittest.main()
