from __future__ import annotations

import unittest

from src.adapters.api.adapter import ApiAdapter
from src.adapters.registry import create_adapter
from src.core.platform_models import FaceAction, MfaceAction, ReplyAction, SessionRef


class ApiAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.adapter = ApiAdapter()

    def test_normalize_inbound_payload_for_private_text_message(self) -> None:
        inbound_event = self.adapter.normalize_inbound_payload(
            {
                "event_type": "message",
                "message_id": "api-msg-1",
                "text": "hello from api",
                "session": {
                    "scope": "private",
                    "conversation_id": "api:session:1",
                    "user_id": "external-user",
                },
                "sender": {
                    "user_id": "external-user",
                    "display_name": "外部用户",
                },
                "capabilities": {
                    "supports_text": True,
                    "supports_quote_reply": True,
                },
            }
        )

        self.assertIsNotNone(inbound_event)
        self.assertEqual(inbound_event.platform, "api")
        self.assertEqual(inbound_event.adapter, "openapi")
        self.assertEqual(inbound_event.message_kind, "text")
        self.assertEqual(inbound_event.session.key, "api:session:1")
        self.assertEqual(inbound_event.sender.display_name, "外部用户")
        self.assertTrue(inbound_event.capabilities.supports_quote_reply)

    def test_normalize_inbound_payload_for_group_mixed_message(self) -> None:
        inbound_event = self.adapter.normalize_inbound_payload(
            {
                "event_type": "message",
                "message_id": "api-msg-2",
                "text": "look at this",
                "mentioned_user_ids": ["assistant-1"],
                "session": {
                    "scope": "group",
                    "conversation_id": "api:group:room-1:user-9",
                    "channel_id": "room-1",
                    "user_id": "user-9",
                },
                "sender": {
                    "user_id": "user-9",
                    "display_name": "API 群用户",
                },
                "attachments": [
                    {
                        "kind": "image",
                        "attachment_id": "img-1",
                        "url": "https://example.com/image.png",
                        "name": "image.png",
                    }
                ],
                "capabilities": {
                    "supports_text": True,
                    "supports_images": True,
                    "supports_groups": True,
                },
            }
        )

        self.assertIsNotNone(inbound_event)
        self.assertEqual(inbound_event.message_kind, "mixed")
        self.assertEqual(inbound_event.session.channel_id, "room-1")
        self.assertEqual(inbound_event.mentioned_user_ids, ("assistant-1",))
        self.assertEqual(inbound_event.attachments[0].attachment_id, "img-1")
        self.assertTrue(inbound_event.capabilities.supports_groups)

    async def test_send_reply_and_native_sticker_actions_as_api_payloads(self) -> None:
        reply_result = await self.adapter.send_action(
            ReplyAction(
                session=SessionRef(platform="api", scope="private", conversation_id="api:session:1", user_id="external-user"),
                text="hello",
                quote_message_id="api-msg-1",
            )
        )
        face_result = await self.adapter.send_action(
            FaceAction(
                session=SessionRef(platform="api", scope="group", conversation_id="api:group:room-1:user-9", channel_id="room-1", user_id="user-9"),
                face_id="14",
            )
        )
        mface_result = await self.adapter.send_action(
            MfaceAction(
                session=SessionRef(platform="api", scope="group", conversation_id="api:group:room-1:user-9", channel_id="room-1", user_id="user-9"),
                emoji_id="991",
                emoji_package_id="7",
                key="native-key",
                summary="开心",
            )
        )

        self.assertTrue(reply_result)
        self.assertTrue(face_result)
        self.assertTrue(mface_result)
        self.assertEqual(
            self.adapter.sent_payloads[0],
            {
                "action": "reply",
                "session": {
                    "platform": "api",
                    "scope": "private",
                    "conversation_id": "api:session:1",
                    "user_id": "external-user",
                    "account_id": "",
                    "channel_id": "",
                    "metadata": {},
                },
                "message": {
                    "text": "hello",
                    "segments": [],
                    "quote_message_id": "api-msg-1",
                },
                "metadata": {},
            },
        )
        self.assertEqual(
            self.adapter.sent_payloads[1],
            {
                "action": "face",
                "session": {
                    "platform": "api",
                    "scope": "group",
                    "conversation_id": "api:group:room-1:user-9",
                    "user_id": "user-9",
                    "account_id": "",
                    "channel_id": "room-1",
                    "metadata": {},
                },
                "face": {
                    "id": "14",
                },
                "metadata": {},
            },
        )
        self.assertEqual(
            self.adapter.sent_payloads[2],
            {
                "action": "mface",
                "session": {
                    "platform": "api",
                    "scope": "group",
                    "conversation_id": "api:group:room-1:user-9",
                    "user_id": "user-9",
                    "account_id": "",
                    "channel_id": "room-1",
                    "metadata": {},
                },
                "mface": {
                    "emoji_id": "991",
                    "emoji_package_id": "7",
                    "key": "native-key",
                    "summary": "开心",
                },
                "metadata": {},
            },
        )

    def test_create_adapter_supports_api_aliases(self) -> None:
        self.assertIsInstance(create_adapter("api"), ApiAdapter)
        self.assertIsInstance(create_adapter("openapi"), ApiAdapter)


if __name__ == "__main__":
    unittest.main()
