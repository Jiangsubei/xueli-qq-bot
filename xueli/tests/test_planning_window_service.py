from __future__ import annotations

import asyncio
import sys
import types
import unittest


if "aiohttp" not in sys.modules:
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientError = Exception
    aiohttp.ClientTimeout = object
    aiohttp.ClientSession = object
    sys.modules["aiohttp"] = aiohttp

from src.core.models import MessageEvent
from src.handlers.message_handler import MessageHandler


class _FakePlanner:
    async def plan(self, **kwargs):
        raise AssertionError("planner should not be called in pure window service tests")


class PlanningWindowServiceTests(unittest.IsolatedAsyncioTestCase):
    def _private_event(self, text: str, *, message_id: int = 10) -> MessageEvent:
        return MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "private",
                "message_id": message_id,
                "user_id": 42,
                "self_id": 999,
                "time": 1000 + message_id,
                "raw_message": text,
                "message": [{"type": "text", "data": {"text": text}}],
                "sender": {"nickname": "Private User"},
            }
        )

    def _group_event(self, text: str, *, at_bot: bool = False, message_id: int = 11) -> MessageEvent:
        message = []
        if at_bot:
            message.append({"type": "at", "data": {"qq": "999"}})
        message.append({"type": "text", "data": {"text": text}})
        return MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 123,
                "message_id": message_id,
                "user_id": 42,
                "self_id": 999,
                "time": 1000 + message_id,
                "raw_message": text,
                "message": message,
                "sender": {"nickname": "Group User"},
            }
        )

    async def test_private_window_dispatches_single_merged_batch(self) -> None:
        handler = MessageHandler(conversation_planner=_FakePlanner())
        handler.private_batch_window_seconds = 0.01

        first_task = asyncio.create_task(
            handler.planning_window_service.submit_private_event(event=self._private_event("第一句", message_id=10))
        )
        await asyncio.sleep(0)
        second_result = await handler.planning_window_service.submit_private_event(
            event=self._private_event("再说一个点", message_id=11)
        )
        first_result = await first_task

        self.assertEqual(second_result.status, "accepted_only")
        self.assertEqual(first_result.status, "dispatch_window")
        self.assertEqual(first_result.window.seq, 1)
        self.assertIn("第一句", first_result.window.merged_user_message)
        self.assertIn("再说一个点", first_result.window.merged_user_message)

    async def test_group_at_message_bypasses_window(self) -> None:
        handler = MessageHandler(conversation_planner=_FakePlanner())
        result = await handler.planning_window_service.submit_group_event(event=self._group_event("看这里", at_bot=True))

        self.assertEqual(result.status, "bypassed")
        self.assertEqual(result.reason, "bypassed")


if __name__ == "__main__":
    unittest.main()
