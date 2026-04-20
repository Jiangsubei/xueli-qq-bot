from __future__ import annotations

import sys
import types
import unittest
from types import SimpleNamespace


if "aiohttp" not in sys.modules:
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientError = Exception
    aiohttp.ClientTimeout = object
    aiohttp.ClientSession = object
    sys.modules["aiohttp"] = aiohttp

from src.handlers.reply_generation_service import ReplyGenerationService
from src.services.ai.types import AIResponse


class _Host:
    def _get_conversation_key(self, event):
        del event
        return "qq:private:42"


class ReplyGenerationServiceTests(unittest.TestCase):
    def test_parses_json_string_array_into_segments(self) -> None:
        service = ReplyGenerationService(_Host(), pipeline=SimpleNamespace())
        prepared = SimpleNamespace(message_context=SimpleNamespace(trace_id="trace-1"))
        event = SimpleNamespace(message_id=1)

        response = service._normalize_visible_reply(
            response=AIResponse(content='["第一句", "第二句"]'),
            event=event,
            prepared=prepared,
        )

        self.assertEqual(response.segments, ["第一句", "第二句"])
        self.assertEqual(response.content, "第一句\n第二句")

    def test_falls_back_to_single_text_when_response_is_not_array(self) -> None:
        service = ReplyGenerationService(_Host(), pipeline=SimpleNamespace())
        prepared = SimpleNamespace(message_context=SimpleNamespace(trace_id="trace-2"))
        event = SimpleNamespace(message_id=2)

        response = service._normalize_visible_reply(
            response=AIResponse(content="普通文本"),
            event=event,
            prepared=prepared,
        )

        self.assertEqual(response.segments, ["普通文本"])
        self.assertEqual(response.content, "普通文本")


if __name__ == "__main__":
    unittest.main()
