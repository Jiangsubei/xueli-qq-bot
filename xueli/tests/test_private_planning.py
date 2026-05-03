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
    def _event(self, text: str, *, message_id: int = 10) -> MessageEvent:
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

    async def test_private_message_uses_planner_after_window_closes(self) -> None:
        planner = _FakePlanner()
        handler = MessageHandler(conversation_planner=planner)

        plan = await handler.plan_message(self._event("帮我看看这个思路"))

        self.assertEqual(plan.action, MessagePlanAction.REPLY.value)
        self.assertEqual(plan.source, "planner")
        self.assertEqual(planner.calls, 1)
        self.assertIsNotNone(plan.prompt_plan)

    async def test_private_bypasses_window_direct_to_planner(self) -> None:
        planner = _FakePlanner()
        handler = MessageHandler(conversation_planner=planner)

        first_plan = await handler.plan_message(self._event("我补充一下", message_id=10))
        second_plan = await handler.plan_message(self._event("帮我整理这个思路", message_id=11))

        self.assertEqual(first_plan.action, MessagePlanAction.REPLY.value)
        self.assertEqual(second_plan.action, MessagePlanAction.REPLY.value)
        self.assertEqual(planner.calls, 2)

    async def test_cleanup_private_window_state_removes_idle_conversations(self) -> None:
        planner = _FakePlanner()
        handler = MessageHandler(conversation_planner=planner)
        scheduler = handler.planning_window_service.scheduler
        await scheduler.submit_event(
            conversation_key="private:42",
            chat_mode="private",
            event="hello",
            window_seconds=0.0,
            queue_expire_seconds=0.01,
            message_builder=lambda event: {"text_content": str(event), "text": str(event), "event_time": 1.0},
            merge_builder=lambda items: "\n".join(str(item.get("text_content") or "") for item in items),
        )
        await scheduler.mark_window_complete("private:42", 1)
        await scheduler.cleanup(active_keys=[], idle_seconds=0.0)

        self.assertNotIn("private:42", scheduler.get_states())


if __name__ == "__main__":
    unittest.main()
