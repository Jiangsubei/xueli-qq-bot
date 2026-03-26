import asyncio
import json
import unittest

from tests.test_support import FakeHTTPSessionManager, install_dependency_stubs

install_dependency_stubs()

import aiohttp

from src.services.ai.request_builder import AIRequestBuilder
from src.services.ai.response_parser import AIResponseParser
from src.services.ai.types import AIAPIError
from src.services.ai_client import AIClient


class AIRequestBuilderTests(unittest.TestCase):
    def test_explicit_call_args_override_default_extra_params(self):
        builder = AIRequestBuilder(
            "gpt-test",
            extra_params={"temperature": 0.1, "max_tokens": 128, "top_p": 0.9},
        )

        body = builder.build(
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.7,
            max_tokens=256,
        )

        self.assertEqual(0.7, body["temperature"])
        self.assertEqual(256, body["max_tokens"])
        self.assertEqual(0.9, body["top_p"])


class AIResponseParserTests(unittest.TestCase):
    def test_parse_supports_custom_response_path(self):
        parser = AIResponseParser(
            response_path="data.answer.text",
            default_model="gpt-test",
        )

        result = parser.parse({"data": {"answer": {"text": "hello"}}})

        self.assertEqual("hello", result.content)
        self.assertEqual("gpt-test", result.model)

    def test_parse_falls_back_to_output_text(self):
        parser = AIResponseParser(
            response_path="missing.path",
            default_model="gpt-test",
        )

        result = parser.parse({"output_text": "fallback reply"})

        self.assertEqual("fallback reply", result.content)

    def test_parse_extracts_tool_calls(self):
        parser = AIResponseParser(
            response_path="choices.0.message.content",
            default_model="gpt-test",
        )
        tool_calls = [{"id": "call_1", "function": {"name": "remember_important_memory", "arguments": "{}"}}]

        result = parser.parse(
            {
                "choices": [
                    {
                        "message": {"content": "", "tool_calls": tool_calls},
                        "finish_reason": "tool_calls",
                    }
                ],
                "model": "gpt-live",
            }
        )

        self.assertEqual(tool_calls, result.tool_calls)
        self.assertEqual("tool_calls", result.finish_reason)
        self.assertEqual("gpt-live", result.model)


class AIClientTests(unittest.IsolatedAsyncioTestCase):
    def build_client(self, outcome):
        client = AIClient(
            api_base="https://example.com/v1",
            api_key="sk-test",
            model="gpt-test",
            timeout=12,
        )
        client._session_manager = FakeHTTPSessionManager(outcome)
        return client

    async def test_chat_completion_returns_parsed_response(self):
        client = self.build_client(
            lambda url, payload: (
                200,
                json.dumps(
                    {
                        "choices": [{"message": {"content": "hello world"}, "finish_reason": "stop"}],
                        "model": "gpt-live",
                    }
                ),
            )
        )

        result = await client.chat_completion(messages=[{"role": "user", "content": "hi"}])

        self.assertEqual("hello world", result.content)
        self.assertEqual("gpt-live", result.model)
        self.assertEqual(1, client._session_manager.ensure_calls)

    async def test_chat_completion_maps_http_errors(self):
        client = self.build_client(lambda url, payload: (503, "upstream unavailable"))

        with self.assertRaises(AIAPIError) as ctx:
            await client.chat_completion(messages=[{"role": "user", "content": "hi"}])

        self.assertIn("503", str(ctx.exception))

    async def test_chat_completion_maps_invalid_json(self):
        client = self.build_client(lambda url, payload: (200, "not-json"))

        with self.assertRaises(AIAPIError) as ctx:
            await client.chat_completion(messages=[{"role": "user", "content": "hi"}])

        self.assertIn("API", str(ctx.exception))

    async def test_chat_completion_maps_timeout(self):
        client = self.build_client(lambda url, payload: asyncio.TimeoutError())

        with self.assertRaises(AIAPIError) as ctx:
            await client.chat_completion(messages=[{"role": "user", "content": "hi"}])

        self.assertIn("超时", str(ctx.exception))

    async def test_chat_completion_maps_client_errors(self):
        client = self.build_client(lambda url, payload: aiohttp.ClientError("boom"))

        with self.assertRaises(AIAPIError) as ctx:
            await client.chat_completion(messages=[{"role": "user", "content": "hi"}])

        self.assertIn("HTTP", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
