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

from src.core.models import MessageEvent, MessageHandlingPlan, MessagePlanAction, PromptPlan
from src.handlers.message_handler import MessageHandler


class _FakePlanner:
    def __init__(self) -> None:
        self.calls = 0
        self.last_user_message = ""

    async def plan(self, **kwargs) -> MessageHandlingPlan:
        self.calls += 1
        self.last_user_message = str(kwargs.get("user_message") or "")
        return MessageHandlingPlan(
            action=MessagePlanAction.REPLY.value,
            reason="planner reply",
            source="planner",
            prompt_plan=PromptPlan(),
        )


class PrivatePlanningTests(unittest.IsolatedAsyncioTestCase):
    def _event(self, text: str) -> MessageEvent:
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

    async def test_private_wait_signal_skips_planner(self) -> None:
        planner = _FakePlanner()
        handler = MessageHandler(conversation_planner=planner)

        plan = await handler.plan_message(self._event("我补充一下"))

        self.assertEqual(plan.action, MessagePlanAction.WAIT.value)
        self.assertEqual(planner.calls, 0)

    async def test_private_message_uses_planner_when_not_waiting(self) -> None:
        planner = _FakePlanner()
        handler = MessageHandler(conversation_planner=planner)

        plan = await handler.plan_message(self._event("帮我看看这个思路"))

        self.assertEqual(plan.action, MessagePlanAction.REPLY.value)
        self.assertEqual(plan.source, "planner")
        self.assertEqual(planner.calls, 1)
        self.assertIsNotNone(plan.prompt_plan)

    async def test_private_batching_merges_fragmented_inputs(self) -> None:
        planner = _FakePlanner()
        handler = MessageHandler(conversation_planner=planner)
        handler.private_batch_window_seconds = 0.01

        first_task = asyncio.create_task(handler.plan_message(self._event("我补充一下")))
        await asyncio.sleep(0)
        second_plan = await handler.plan_message(self._event("帮我整理这个思路"))
        first_plan = await first_task

        self.assertEqual(first_plan.action, MessagePlanAction.WAIT.value)
        self.assertEqual(second_plan.action, MessagePlanAction.REPLY.value)
        self.assertEqual(planner.calls, 1)
        self.assertIn("我补充一下", planner.last_user_message)
        self.assertIn("帮我整理这个思路", planner.last_user_message)
        self.assertIn("merged_user_message", second_plan.reply_context)

    async def test_cleanup_private_pending_inputs_removes_stale_buffers(self) -> None:
        planner = _FakePlanner()
        handler = MessageHandler(conversation_planner=planner)
        handler.private_batch_window_seconds = 0.01
        handler.private_pending_inputs["private:42"] = [{"event_time": 1.0, "text": "old"}]
        handler.private_batch_versions["private:42"] = 3

        handler._cleanup_private_batch_state()

        self.assertNotIn("private:42", handler.private_pending_inputs)
        self.assertNotIn("private:42", handler.private_batch_versions)


if __name__ == "__main__":
    unittest.main()
