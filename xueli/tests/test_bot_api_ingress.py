from __future__ import annotations

import unittest

from src.adapters.api.adapter import ApiAdapter
from src.core.runtime import BotRuntime


class _DispatcherStub:
    def __init__(self) -> None:
        self.dispatched_raw = []
        self.dispatched_inbound = []

    async def dispatch(self, payload):
        self.dispatched_raw.append(payload)

    async def dispatch_inbound_event(self, inbound_event, *, raw_data=None, self_id=""):
        self.dispatched_inbound.append(
            {
                "inbound_event": inbound_event,
                "raw_data": raw_data,
                "self_id": self_id,
            }
        )


class _PassthroughAdapter:
    pass


class BotRuntimeApiIngressTests(unittest.IsolatedAsyncioTestCase):
    def _build_bot(self) -> BotRuntime:
        bot = BotRuntime.__new__(BotRuntime)
        bot.dispatcher = _DispatcherStub()
        bot.adapter = None
        bot.connection = None
        bot._adapters_by_name = {}
        bot._adapters_by_platform = {}
        return bot

    async def test_ingest_inbound_event_dispatches_to_dispatcher(self) -> None:
        bot = self._build_bot()
        inbound_event = ApiAdapter().normalize_inbound_payload(
            {
                "event_type": "message",
                "message_id": "api-msg-4",
                "text": "hello from bot ingress",
                "session": {
                    "scope": "private",
                    "conversation_id": "api:session:bot-1",
                    "user_id": "external-user",
                    "account_id": "assistant-1",
                },
                "sender": {
                    "user_id": "external-user",
                    "display_name": "Bot Ingress User",
                },
            }
        )

        await bot.ingest_inbound_event(inbound_event)

        self.assertEqual(len(bot.dispatcher.dispatched_inbound), 1)
        self.assertIs(bot.dispatcher.dispatched_inbound[0]["inbound_event"], inbound_event)
        self.assertEqual(bot.dispatcher.dispatched_inbound[0]["self_id"], "assistant-1")

    async def test_ingest_adapter_payload_uses_adapter_normalizer_when_available(self) -> None:
        bot = self._build_bot()
        adapter = ApiAdapter()

        await bot.ingest_adapter_payload(
            {
                "event_type": "message",
                "message_id": "api-msg-5",
                "text": "hello from payload",
                "session": {
                    "scope": "private",
                    "conversation_id": "api:session:bot-2",
                    "user_id": "external-user",
                },
                "sender": {
                    "user_id": "external-user",
                    "display_name": "Payload User",
                },
            },
            adapter=adapter,
            self_id="assistant-2",
        )

        self.assertEqual(len(bot.dispatcher.dispatched_inbound), 1)
        self.assertEqual(bot.dispatcher.dispatched_inbound[0]["self_id"], "assistant-2")
        self.assertEqual(bot.dispatcher.dispatched_inbound[0]["inbound_event"].text, "hello from payload")
        self.assertEqual(bot.dispatcher.dispatched_raw, [])

    async def test_ingest_adapter_payload_falls_back_to_raw_dispatch(self) -> None:
        bot = self._build_bot()
        payload = {"post_type": "message", "message_type": "private", "message": []}

        await bot.ingest_adapter_payload(payload, adapter=_PassthroughAdapter())

        self.assertEqual(bot.dispatcher.dispatched_raw, [payload])
        self.assertEqual(bot.dispatcher.dispatched_inbound, [])


if __name__ == "__main__":
    unittest.main()
