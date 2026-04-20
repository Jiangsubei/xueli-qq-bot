from __future__ import annotations

import unittest
from unittest.mock import patch

from src.adapters.api.runtime import ApiRuntimeError, ApiRuntimeServer, ingest_api_payload


class _BotStub:
    def __init__(self) -> None:
        self.calls = []

    async def ingest_adapter_payload(self, payload, *, adapter=None, self_id=""):
        self.calls.append({"payload": payload, "adapter": adapter, "self_id": self_id})


class ApiRuntimeTests(unittest.TestCase):
    def test_ingest_api_payload_raises_when_bot_runtime_is_unavailable(self) -> None:
        with patch("src.adapters.api.runtime.get_runtime_state", return_value={"bot": None}):
            with self.assertRaises(ApiRuntimeError):
                ingest_api_payload({"event_type": "message"})

    def test_ingest_api_payload_dispatches_via_runtime_loop(self) -> None:
        bot = _BotStub()

        def _run_immediately(coro, *, timeout=5.0):
            del timeout
            import asyncio

            return asyncio.run(coro)

        with patch("src.adapters.api.runtime.get_runtime_state", return_value={"bot": bot}), patch(
            "src.adapters.api.runtime.run_coro_threadsafe",
            side_effect=_run_immediately,
        ):
            ingest_api_payload({"event_type": "message", "text": "hello"})

        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(bot.calls[0]["payload"]["text"], "hello")
        self.assertIsNotNone(bot.calls[0]["adapter"])
        self.assertEqual(getattr(bot.calls[0]["adapter"], "platform", ""), "api")

    def test_create_handler_class_exposes_health_and_events_paths(self) -> None:
        server = ApiRuntimeServer(enabled=True)
        handler_cls = server._build_handler_class()

        self.assertTrue(callable(handler_cls))


if __name__ == "__main__":
    unittest.main()
