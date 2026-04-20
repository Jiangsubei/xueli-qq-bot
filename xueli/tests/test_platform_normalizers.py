from __future__ import annotations

import unittest

from src.core.models import MessageEvent
from src.core.message_trace import get_execution_key
from src.core.platform_models import InboundEvent, PlatformCapabilities, SenderRef, SessionRef
from src.core.platform_normalizers import (
    event_mentions_account,
    get_inbound_reply_to_message_id,
    normalize_onebot_message_event,
)
from src.handlers.conversation_session_manager import ConversationSessionManager


class PlatformNormalizerTests(unittest.TestCase):
    def test_normalize_private_text_message(self) -> None:
        event = MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "private",
                "message_id": 101,
                "user_id": 12345,
                "self_id": 99999,
                "raw_message": "你好",
                "message": [{"type": "text", "data": {"text": "你好"}}],
                "sender": {"nickname": "小明"},
            }
        )

        inbound = normalize_onebot_message_event(event)

        self.assertEqual(inbound.platform, "qq")
        self.assertEqual(inbound.adapter, "napcat")
        self.assertEqual(inbound.session.scope, "private")
        self.assertEqual(inbound.session.key, "private:12345")
        self.assertEqual(inbound.sender.display_name, "小明")
        self.assertEqual(inbound.text, "你好")
        self.assertEqual(inbound.message_kind, "text")
        self.assertEqual(inbound.message_id, "101")
        self.assertFalse(inbound.has_attachments)

    def test_normalize_group_mixed_message_with_reply_and_image(self) -> None:
        event = MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "group",
                "message_id": 202,
                "user_id": 12345,
                "group_id": 54321,
                "self_id": 99999,
                "raw_message": "[CQ:reply,id=77][CQ:at,qq=99999]看图",
                "message": [
                    {"type": "reply", "data": {"id": "77"}},
                    {"type": "at", "data": {"qq": "99999"}},
                    {"type": "text", "data": {"text": "看图"}},
                    {"type": "image", "data": {"file": "img-1", "url": "https://example.com/a.png"}},
                ],
                "sender": {"card": "群名片"},
            }
        )

        inbound = normalize_onebot_message_event(event)

        self.assertEqual(inbound.session.scope, "group")
        self.assertEqual(inbound.session.channel_id, "54321")
        self.assertEqual(inbound.session.key, "group:54321:12345")
        self.assertEqual(inbound.reply_to_message_id, "77")
        self.assertEqual(inbound.mentioned_user_ids, ("99999",))
        self.assertEqual(inbound.message_kind, "mixed")
        self.assertEqual(len(inbound.attachments), 1)
        self.assertEqual(inbound.attachments[0].kind, "image")
        self.assertEqual(inbound.attachments[0].attachment_id, "img-1")
        self.assertEqual(inbound.attachments[0].url, "https://example.com/a.png")

    def test_session_manager_uses_standardized_key_without_changing_behavior(self) -> None:
        event = MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "group",
                "message_id": 303,
                "user_id": 13579,
                "group_id": 24680,
                "self_id": 99999,
                "raw_message": "hello",
                "message": [{"type": "text", "data": {"text": "hello"}}],
            }
        )

        manager = ConversationSessionManager()
        self.assertEqual(manager.get_key(event), "qq:group:24680:13579")

    def test_inbound_helpers_read_reply_and_mentions_from_attached_event(self) -> None:
        event = MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "group",
                "message_id": "api-msg-1",
                "user_id": "external-user",
                "group_id": "room-1",
                "self_id": "assistant-1",
                "raw_message": "hello",
                "message": [{"type": "text", "data": {"text": "hello"}}],
            }
        )
        inbound = normalize_onebot_message_event(
            MessageEvent.from_dict(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "message_id": 999,
                    "user_id": 123,
                    "group_id": 456,
                    "self_id": 789,
                    "message": [
                        {"type": "reply", "data": {"id": "77"}},
                        {"type": "at", "data": {"qq": "789"}},
                    ],
                }
            )
        )
        inbound = inbound.__class__(
            **{
                **inbound.__dict__,
                "platform": "api",
                "adapter": "openapi",
                "session": inbound.session.__class__(platform="api", scope="group", conversation_id="api:group:room-1:user-1", account_id="assistant-1", channel_id="room-1", user_id="user-1"),
                "reply_to_message_id": "external-parent",
                "mentioned_user_ids": ("assistant-1",),
            }
        )
        setattr(event, "_inbound_event", inbound)

        self.assertEqual(get_inbound_reply_to_message_id(event), "external-parent")
        self.assertTrue(event_mentions_account(event))

    def test_execution_key_uses_platform_qualified_group_scope(self) -> None:
        event = MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "group",
                "message_id": "api-group-1",
                "user_id": "external-user",
                "group_id": "room-1",
                "self_id": "assistant-1",
                "raw_message": "hello",
                "message": [{"type": "text", "data": {"text": "hello"}}],
            }
        )
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
                    conversation_id="api:group:room-1:external-user",
                    user_id="external-user",
                    account_id="assistant-1",
                    channel_id="room-1",
                ),
                sender=SenderRef(user_id="external-user", display_name="API User"),
                text="hello",
                capabilities=PlatformCapabilities(supports_text=True, supports_groups=True),
            ),
        )

        self.assertEqual(get_execution_key(event), "api:group:room-1")


if __name__ == "__main__":
    unittest.main()
