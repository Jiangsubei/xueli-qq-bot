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

    async def test_private_window_bypasses_to_planner(self) -> None:
        handler = MessageHandler(conversation_planner=_FakePlanner())

        result = await handler.planning_window_service.submit_private_event(
            event=self._private_event("第一句", message_id=10)
        )

        self.assertEqual(result.status, "bypassed")

    async def test_group_at_message_bypasses_window(self) -> None:
        handler = MessageHandler(conversation_planner=_FakePlanner())
        result = await handler.planning_window_service.submit_event(event=self._group_event("看这里", at_bot=True))

        self.assertEqual(result.status, "bypassed")


if __name__ == "__main__":
    unittest.main()
