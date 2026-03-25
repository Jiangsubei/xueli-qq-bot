import json
import unittest

from src.core.models import MessageEvent, MessageSegment, MessageType
from src.handlers.message_handler import MessageHandler
from src.services.ai_client import AIResponse


class FakeAIClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def build_text_message(self, role, content):
        return {"role": role, "content": content}

    def build_multimodal_message(self, role, text, images):
        return {"role": role, "content": [{"type": "text", "text": text}], "images": images}

    async def chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeMemoryManager:
    def __init__(self):
        self.saved_important = []
        self.turns = []

    async def get_important_memories(self, user_id, limit=5):
        return []

    async def search_memories_with_context(self, user_id, query, top_k=3, include_conversations=True):
        return {
            "memories": [],
            "conversations": [],
            "context_text": "",
            "history_messages": [],
        }

    async def add_important_memory(self, user_id, content, source="manual", priority=1):
        item = {
            "user_id": user_id,
            "content": content,
            "source": source,
            "priority": priority,
        }
        self.saved_important.append(item)
        return item

    def register_dialogue_turn(self, user_id, user_message, assistant_message):
        self.turns.append((user_id, user_message, assistant_message))

    async def maybe_extract_memories(self, user_id):
        return []


class MessageHandlerToolCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_can_call_remember_important_memory_tool(self):
        tool_call = {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "remember_important_memory",
                "arguments": json.dumps(
                    {"content": "用户对花生过敏", "priority": 5},
                    ensure_ascii=False,
                ),
            },
        }
        fake_ai_client = FakeAIClient(
            [
                AIResponse(content="", finish_reason="tool_calls", tool_calls=[tool_call]),
                AIResponse(content="好，我记住了。", finish_reason="stop", tool_calls=[]),
            ]
        )
        fake_memory_manager = FakeMemoryManager()
        handler = MessageHandler(ai_client=fake_ai_client, memory_manager=fake_memory_manager)

        event = MessageEvent(
            post_type="message",
            message_type=MessageType.PRIVATE.value,
            user_id=123,
            self_id=456,
            message=[MessageSegment.text("记住我对花生过敏")],
            raw_message="记住我对花生过敏",
        )

        reply = await handler.get_ai_response(event)

        self.assertEqual(reply, "好，我记住了。")
        self.assertEqual(len(fake_memory_manager.saved_important), 1)
        self.assertEqual(fake_memory_manager.saved_important[0]["content"], "用户对花生过敏")
        self.assertEqual(fake_memory_manager.saved_important[0]["source"], "tool_call")
        self.assertEqual(fake_memory_manager.saved_important[0]["priority"], 5)
        self.assertEqual(len(fake_ai_client.calls), 2)
        self.assertIn("tools", fake_ai_client.calls[0])
        self.assertEqual(fake_ai_client.calls[0]["tool_choice"], "auto")
        first_system_prompt = fake_ai_client.calls[0]["messages"][0]["content"]
        self.assertIn("应优先调用该工具", first_system_prompt)
        self.assertIn("而不是只在回复里口头答应", first_system_prompt)
        self.assertIn("先调用工具完成保存", first_system_prompt)

        second_call_messages = fake_ai_client.calls[1]["messages"]
        self.assertTrue(any(message.get("role") == "tool" for message in second_call_messages))
        self.assertTrue(
            any(
                message.get("role") == "assistant" and message.get("tool_calls")
                for message in second_call_messages
            )
        )


if __name__ == "__main__":
    unittest.main()
