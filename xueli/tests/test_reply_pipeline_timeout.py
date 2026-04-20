from __future__ import annotations

import asyncio
import sys
import types
import unittest
from types import MethodType, SimpleNamespace


if "aiohttp" not in sys.modules:
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientError = Exception
    aiohttp.ClientTimeout = object
    aiohttp.ClientSession = object
    sys.modules["aiohttp"] = aiohttp

from src.core.models import Conversation, MessageEvent
from src.handlers.reply_pipeline import PreparedReplyRequest, ReplyPipeline


class _FakeHost:
    def __init__(self) -> None:
        self.app_config = SimpleNamespace(bot_behavior=SimpleNamespace(log_full_prompt=False))
        self.memory_manager = None

    def _get_conversation_key(self, event: MessageEvent) -> str:
        del event
        return "private:42"


class ReplyPipelineTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_reply_returns_timeout_fallback_when_model_times_out(self) -> None:
        pipeline = ReplyPipeline(_FakeHost())
        event = MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "private",
                "message_id": 1,
                "user_id": 42,
                "self_id": 999,
                "raw_message": "继续",
                "message": [{"type": "text", "data": {"text": "继续"}}],
            }
        )

        async def fake_prepare_request(self, *, event, user_message, plan=None, context=None):
            del event, user_message, plan, context
            return PreparedReplyRequest(
                original_user_message="继续",
                model_user_message="继续",
                history_user_message="继续",
                system_prompt="system",
                base64_images=[],
                conversation=Conversation(),
                related_history_messages=[],
                messages=[{"role": "user", "content": "继续"}],
                active_sections=[],
                fallback_response=None,
                message_context=None,
            )

        async def fake_generate_reply(self, *, event, prepared):
            del event, prepared
            raise asyncio.TimeoutError()

        pipeline.prepare_request = MethodType(fake_prepare_request, pipeline)
        pipeline.reply_generation_service.generate_reply = MethodType(fake_generate_reply, pipeline.reply_generation_service)

        result = await pipeline.execute(event=event, user_message="继续")

        self.assertEqual(result.source, "fallback")
        self.assertEqual(result.text, "AI 服务响应超时，请稍后再试。")


if __name__ == "__main__":
    unittest.main()
