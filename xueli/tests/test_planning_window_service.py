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
    def _private_event(self, text: str) -> MessageEvent:
        return MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "private",
                "message_id": 10,
                "user_id": 42,
                "self_id": 999,
                "time": 1000,
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

    async def test_private_window_merges_fragmented_inputs(self) -> None:
        handler = MessageHandler(conversation_planner=_FakePlanner())
        handler.private_batch_window_seconds = 0.01

        first_task = asyncio.create_task(handler.planning_window_service.process_private_message(event=self._private_event("第一句")))
        await asyncio.sleep(0)
        second_result = await handler.planning_window_service.process_private_message(event=self._private_event("再说一个点"))
        first_result = await first_task

        self.assertEqual(first_result.window_reason, "用户仍在短时间内连续补充，先等待本轮私聊输入稳定")
        self.assertEqual(second_result.window_reason, "private_window_merged")
        self.assertIn("第一句", second_result.merged_user_message)
        self.assertIn("再说一个点", second_result.merged_user_message)

    async def test_group_proactive_window_waits_then_merges(self) -> None:
        handler = MessageHandler(conversation_planner=_FakePlanner())
        handler.group_proactive_window_seconds = 0.01

        first_task = asyncio.create_task(
            handler.planning_window_service.process_group_message(event=self._group_event("先说一句", message_id=21))
        )
        await asyncio.sleep(0)
        second_result = await handler.planning_window_service.process_group_message(event=self._group_event("再补一句", message_id=22))
        first_result = await first_task

        self.assertEqual(first_result.window_reason, "group_window_wait_for_more")
        self.assertEqual(second_result.window_reason, "group_window_merged")
        self.assertIn("先说一句", second_result.merged_user_message)
        self.assertIn("再补一句", second_result.merged_user_message)

    async def test_group_at_message_bypasses_window(self) -> None:
        handler = MessageHandler(conversation_planner=_FakePlanner())
        result = await handler.planning_window_service.process_group_message(event=self._group_event("看这里", at_bot=True))

        self.assertTrue(result.bypassed)
        self.assertEqual(result.window_reason, "group_window_bypassed")


if __name__ == "__main__":
    unittest.main()
