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

from src.core.models import MessageEvent
from src.core.platform_models import InboundEvent, PlatformCapabilities, SenderRef, SessionRef
from src.handlers.conversation_planner import ConversationPlanner
from src.handlers.message_context import MessageContext


class ConversationPlannerContextPreferenceTests(unittest.TestCase):
    def _event(self) -> MessageEvent:
        return MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "group",
                "message_id": 20,
                "user_id": 42,
                "group_id": 100,
                "self_id": 999,
                "raw_message": "onebot raw text",
                "message": [{"type": "text", "data": {"text": "onebot raw text"}}],
                "sender": {"card": "OneBot 用户"},
            }
        )

    def test_build_user_prompt_prefers_context_and_window_message_data(self) -> None:
        planner = ConversationPlanner(ai_client=object())
        event = self._event()
        context = MessageContext(
            current_sender_label="42（标准事件用户）",
            recent_history_text="标准化历史",
            window_messages=[
                {
                    "is_latest": True,
                    "user_id": "external-user",
                    "speaker_name": "标准事件用户",
                    "raw_text": "标准事件原文",
                    "text_content": "标准事件清洗文本",
                    "display_text": "标准事件展示文本",
                    "has_image": True,
                    "image_count": 2,
                    "message_shape": "text_with_image",
                    "merged_description": "两张图片的摘要",
                    "per_image_descriptions": ["图1", "图2"],
                    "vision_available": True,
                }
            ],
        )

        prompt = planner._build_user_prompt(
            event,
            user_message="标准事件清洗文本",
            recent_messages=[],
            context=context,
        )

        self.assertIn("当前消息来自用户 42（标准事件用户）", prompt)
        self.assertIn("原始文本：标准事件原文", prompt)
        self.assertIn("清洗后文本：标准事件清洗文本", prompt)
        self.assertIn("图片数量：2", prompt)
        self.assertIn("[图片] 两张图片的摘要", prompt)
        self.assertIn("标准化历史", prompt)
        self.assertNotIn("onebot raw text", prompt)
        self.assertNotIn("OneBot 用户", prompt)

    def test_fallback_plan_replies_when_attached_inbound_mentions_self(self) -> None:
        planner = ConversationPlanner(ai_client=object())
        event = self._event()
        setattr(
            event,
            "_inbound_event",
            InboundEvent(
                platform="api",
                adapter="openapi",
                event_type="message",
                message_kind="text",
                session=SessionRef(
                    platform="api",
                    scope="group",
                    conversation_id="api:group:room-1:user-9",
                    user_id="user-9",
                    account_id="999",
                    channel_id="room-1",
                ),
                sender=SenderRef(user_id="user-9", display_name="标准事件用户"),
                text="hello",
                mentioned_user_ids=("999",),
                capabilities=PlatformCapabilities(supports_text=True, supports_groups=True),
            ),
        )

        plan = planner._build_fallback_plan(event, "boom")

        self.assertEqual(plan.action, "reply")
        self.assertEqual(plan.source, "fallback")


if __name__ == "__main__":
    unittest.main()
