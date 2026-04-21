from __future__ import annotations

import types
import unittest

from src.adapters.api.adapter import ApiAdapter
from src.core.runtime import BotRuntime
from src.core.models import MessageEvent, MessageSegment
from src.core.pipeline_errors import SendError
from src.core.platform_bridge import build_message_event_from_inbound
from src.core.platform_models import ReplyAction


class _FakeAdapter:
    def __init__(self, *, result: bool = True, platform: str = "qq", adapter_name: str = "fake") -> None:
        self.result = result
        self.platform = platform
        self.adapter_name = adapter_name
        self.actions = []

    async def send_action(self, action):
        self.actions.append(action)
        return self.result


class _MessageHandlerStub:
    def __init__(self) -> None:
        bot_behavior = type(
            "_BotBehavior",
            (),
            {
                "private_quote_reply_enabled": False,
                "log_full_prompt": False,
                "segmented_reply_enabled": True,
                "max_segments": 3,
                "first_segment_delay_min_ms": 0,
                "first_segment_delay_max_ms": 0,
                "followup_delay_min_seconds": 0.0,
                "followup_delay_max_seconds": 0.0,
            },
        )()
        self.app_config = type("_AppConfig", (), {"bot_behavior": bot_behavior})()

    def split_long_message(self, message):
        return [message]

    def split_by_sentence(self, message):
        return [message]

    def resolve_group_at_user(self, event, plan):
        del plan
        return event.user_id if getattr(event, "message_type", "") == "group" else None


class _RuntimeMetricsStub:
    def __init__(self) -> None:
        self.reply_parts_sent = 0

    def inc_messages_replied(self, count: int) -> None:
        self.reply_parts_sent += int(count)


class BotRuntimeAdapterSendPathTests(unittest.IsolatedAsyncioTestCase):
    def _build_bot(
        self,
        *,
        adapter_result: bool = True,
        platform: str = "qq",
        adapter_name: str = "fake",
        configured_platform: str | None = None,
    ) -> tuple[BotRuntime, _FakeAdapter]:
        adapter = _FakeAdapter(result=adapter_result, platform=platform, adapter_name=adapter_name)
        bot = BotRuntime.__new__(BotRuntime)
        bot.adapter = adapter
        bot.connection = adapter
        bot._adapters_by_name = {}
        bot._adapters_by_platform = {}
        bot.config = types.SimpleNamespace(
            app=types.SimpleNamespace(
                adapter_connection=types.SimpleNamespace(
                    platform=configured_platform if configured_platform is not None else platform,
                    adapter=adapter_name,
                )
            )
        )
        bot.message_handler = _MessageHandlerStub()
        bot.runtime_metrics = _RuntimeMetricsStub()
        bot._sync_status_cache = lambda: None
        bot._should_log_message_summary = lambda: False
        bot.register_runtime_adapter(adapter)
        return bot, adapter

    async def test_send_private_msg_uses_reply_action(self) -> None:
        bot, adapter = self._build_bot()

        await bot._send_private_msg(42, "hello")

        action = adapter.actions[-1]
        self.assertIsInstance(action, ReplyAction)
        self.assertEqual(action.session.scope, "private")
        self.assertEqual(action.session.conversation_id, "private:42")
        self.assertEqual(action.session.user_id, "42")
        self.assertEqual(action.text, "hello")

    async def test_send_group_msg_with_at_uses_segments(self) -> None:
        bot, adapter = self._build_bot()

        await bot._send_group_msg(100, "hi", at_user=200)

        action = adapter.actions[-1]
        self.assertIsInstance(action, ReplyAction)
        self.assertEqual(action.session.scope, "group")
        self.assertEqual(action.session.channel_id, "100")
        self.assertEqual(action.session.user_id, "200")
        self.assertEqual(
            action.segments,
            (
                {"type": "at", "data": {"qq": "200"}},
                {"type": "text", "data": {"text": " hi"}},
            ),
        )

    async def test_send_group_segments_uses_reply_action(self) -> None:
        bot, adapter = self._build_bot()

        with self.assertRaises(SendError):
            await bot._send_group_segments(100, [MessageSegment.image("emoji.png")])

    async def test_send_private_msg_uses_platform_neutral_fallback_platform(self) -> None:
        bot, adapter = self._build_bot(platform="api", adapter_name="openapi")

        await bot._send_private_msg("external-user", "hello")

        action = adapter.actions[-1]
        self.assertEqual(action.session.platform, "api")
        self.assertEqual(action.session.scope, "private")
        self.assertEqual(action.session.conversation_id, "private:external-user")

    async def test_send_group_msg_with_at_uses_platform_specific_mention_for_fallback_session(self) -> None:
        bot, adapter = self._build_bot(platform="api", adapter_name="openapi")

        await bot._send_group_msg("room-1", "hi", at_user="user-9")

        action = adapter.actions[-1]
        self.assertEqual(action.session.platform, "api")
        self.assertEqual(
            action.segments,
            (
                {"type": "mention", "data": {"user_id": "user-9"}},
                {"type": "text", "data": {"text": " hi"}},
            ),
        )

    async def test_send_private_msg_uses_configured_platform_when_adapter_platform_missing(self) -> None:
        bot, adapter = self._build_bot(platform="", adapter_name="openapi", configured_platform="api")

        await bot._send_private_msg("external-user", "hello")

        action = adapter.actions[-1]
        self.assertEqual(action.session.platform, "api")

    async def test_send_failure_raises_send_error(self) -> None:
        bot, _adapter = self._build_bot(adapter_result=False)

        with self.assertRaises(SendError):
            await bot._send_private_msg(42, "hello")

    async def test_send_response_preserves_api_private_session(self) -> None:
        bot, _default_adapter = self._build_bot(platform="qq", adapter_name="napcat")
        adapter = _FakeAdapter(platform="api", adapter_name="openapi")
        bot.register_runtime_adapter(adapter)
        inbound_event = ApiAdapter().normalize_inbound_payload(
            {
                "event_type": "message",
                "message_id": "api-msg-private",
                "text": "hello from api",
                "session": {
                    "scope": "private",
                    "conversation_id": "api:session:42",
                    "user_id": "external-user",
                },
                "sender": {
                    "user_id": "external-user",
                    "display_name": "API User",
                },
            }
        )
        event = build_message_event_from_inbound(inbound_event, self_id="assistant-1")

        await bot._send_response(event, "reply from bot")

        action = adapter.actions[-1]
        self.assertEqual(action.session.platform, "api")
        self.assertEqual(action.session.scope, "private")
        self.assertEqual(action.session.conversation_id, "api:session:42")
        self.assertEqual(action.session.user_id, "external-user")
        self.assertEqual(action.text, "reply from bot")

    async def test_send_response_preserves_api_group_session_and_mention_shape(self) -> None:
        bot, _default_adapter = self._build_bot(platform="qq", adapter_name="napcat")
        adapter = _FakeAdapter(platform="api", adapter_name="openapi")
        bot.register_runtime_adapter(adapter)
        inbound_event = ApiAdapter().normalize_inbound_payload(
            {
                "event_type": "message",
                "message_id": "api-msg-group",
                "text": "hello from api group",
                "session": {
                    "scope": "group",
                    "conversation_id": "api:group:room-1:user-9",
                    "channel_id": "room-1",
                    "user_id": "user-9",
                },
                "sender": {
                    "user_id": "user-9",
                    "display_name": "API Group User",
                },
            }
        )
        event = build_message_event_from_inbound(inbound_event, self_id="assistant-1")

        await bot._send_response(event, "reply to group")

        action = adapter.actions[-1]
        self.assertEqual(action.session.platform, "api")
        self.assertEqual(action.session.scope, "group")
        self.assertEqual(action.session.conversation_id, "api:group:room-1:user-9")
        self.assertEqual(action.session.channel_id, "room-1")
        self.assertEqual(
            action.segments,
            (
                {"type": "mention", "data": {"user_id": "user-9"}},
                {"type": "text", "data": {"text": " reply to group"}},
            ),
        )

    async def test_send_response_uses_structured_segments_for_multiple_group_parts(self) -> None:
        bot, adapter = self._build_bot()
        event = MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "group",
                "message_id": 123,
                "user_id": 42,
                "group_id": 100,
                "self_id": 999,
                "raw_message": "hello",
                "message": [{"type": "text", "data": {"text": "hello"}}],
            }
        )

        await bot._send_response(
            event,
            types.SimpleNamespace(text="第一句\n第二句", segments=["第一句", "第二句"]),
        )

        self.assertEqual(len(adapter.actions), 2)
        self.assertEqual(
            adapter.actions[0].segments,
            (
                {"type": "at", "data": {"qq": "42"}},
                {"type": "text", "data": {"text": " 第一句"}},
            ),
        )
        self.assertEqual(adapter.actions[1].text, "第二句")


if __name__ == "__main__":
    unittest.main()
