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

from src.core.model_invocation_router import ModelInvocationRouter, ModelInvocationType
from src.core.models import MessageEvent, MessagePlanAction
from src.handlers.conversation_planner import ConversationPlanner


class _SlowAIClient:
    def build_text_message(self, role: str, content: str):
        return {"role": role, "content": content}

    async def chat_completion(self, **kwargs):
        del kwargs
        await asyncio.sleep(0.05)
        return types.SimpleNamespace(content='{"action":"reply","reason":"ok"}')
class ConversationPlannerTimeoutTests(unittest.IsolatedAsyncioTestCase):
    def _event(self) -> MessageEvent:
        return MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "private",
                "message_id": 10,
                "user_id": 42,
                "self_id": 999,
                "time": 1000,
                "raw_message": "帮我看看",
                "message": [{"type": "text", "data": {"text": "帮我看看"}}],
                "sender": {"nickname": "Private User"},
            }
        )

    async def test_timeout_falls_back_to_private_reply_plan(self) -> None:
        router = ModelInvocationRouter(
            purpose_timeouts={ModelInvocationType.GROUP_PLAN: 0.01},
        )
        planner = ConversationPlanner(ai_client=_SlowAIClient(), model_invocation_router=router)

        try:
            plan = await planner.plan(
                event=self._event(),
                user_message="帮我看看",
                recent_messages=[],
                window_messages=[],
                context=None,
            )
        finally:
            await router.close()

        self.assertEqual(plan.action, MessagePlanAction.REPLY.value)
        self.assertEqual(plan.source, "fallback")
        self.assertIsNotNone(plan.prompt_plan)


if __name__ == "__main__":
    unittest.main()
