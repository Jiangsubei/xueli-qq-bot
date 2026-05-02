from __future__ import annotations

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
from src.handlers.conversation_plan_coordinator import ConversationPlanCoordinator
from src.handlers.conversation_session_manager import ConversationSessionManager
from src.handlers.conversation_window_models import BufferedWindow


class _PlannerStub:
    def __init__(self) -> None:
        self.last_window_messages = []

    async def plan(self, **kwargs):
        self.last_window_messages = list(kwargs.get("window_messages") or [])
        return MessageHandlingPlan(
            action=MessagePlanAction.REPLY.value,
            reason="planner reply",
            source="planner",
            prompt_plan=PromptPlan(),
        )


class BufferedGroupWindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_buffered_group_window_enriches_image_context_before_planning(self) -> None:
        planner = _PlannerStub()

        async def image_analyzer(event, user_text, trace_id=""):
            del event, user_text, trace_id
            return {
                "per_image_descriptions": ["图里是一只猫"],
                "merged_description": "一只猫趴在桌上",
                "vision_available": True,
                "vision_failure_count": 0,
                "vision_success_count": 1,
                "vision_source": "test",
                "vision_error": "",
            }

        coordinator = ConversationPlanCoordinator(
            planner=planner,
            session_manager=ConversationSessionManager(),
            image_analyzer=image_analyzer,
        )

        event = MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 100,
                "message_id": 88,
                "user_id": 42,
                "self_id": 999,
                "time": 1000,
                "raw_message": "你看看这个",
                "message": [
                    {"type": "text", "data": {"text": "你看看这个"}},
                    {"type": "image", "data": {"file": "img.png"}},
                ],
                "sender": {"nickname": "Group User"},
            }
        )
        window = BufferedWindow(
            conversation_key="qq:group:100",
            seq=1,
            chat_mode="group",
            merged_user_message="你看看这个",
            messages=[
                {
                    "message_id": "88",
                    "speaker_role": "user",
                    "speaker_name": "Group User",
                    "user_id": "42",
                    "event_time": 1000.0,
                    "text_content": "你看看这个",
                    "text": "你看看这个",
                    "display_text": "你看看这个",
                    "has_image": True,
                    "raw_has_image": True,
                    "image_count": 1,
                    "raw_image_count": 1,
                    "_event": event,
                }
            ],
            latest_event=event,
        )

        await coordinator.plan_buffered_window(event=event, window=window, trace_id="trace-test")

        latest_message = planner.last_window_messages[-1]
        self.assertEqual(latest_message.get("merged_description"), "一只猫趴在桌上")
        self.assertTrue(latest_message.get("vision_available"))


if __name__ == "__main__":
    unittest.main()
