from __future__ import annotations

import unittest

from src.adapters.napcat.adapter import NapCatAdapter
from src.core.models import MessageEvent
from src.core.platform_models import FaceAction, MfaceAction, ReplyAction, SessionRef


class _FakeConnection:
    def __init__(self) -> None:
        self.sent_payloads = []
        self.ran = False
        self.disconnected = False
        self._connected = True

    async def run(self) -> None:
        self.ran = True

    async def disconnect(self) -> None:
        self.disconnected = True
        self._connected = False

    async def send(self, data):
        self.sent_payloads.append(data)
        return True


async def _noop_message(_payload):
    return None


async def _noop_signal():
    return None


class NapCatAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.connection = _FakeConnection()
        self.adapter = NapCatAdapter(
            host="127.0.0.1",
            port=8095,
            on_message=_noop_message,
            on_connect=_noop_signal,
            on_disconnect=_noop_signal,
            connection=self.connection,
        )

    async def test_send_private_reply_action(self) -> None:
        action = ReplyAction(
            session=SessionRef(platform="qq", scope="private", conversation_id="private:42", user_id="42"),
            text="hello",
        )

        result = await self.adapter.send_action(action)

        self.assertTrue(result)
        self.assertEqual(
            self.connection.sent_payloads[-1],
            {"action": "send_private_msg", "params": {"user_id": 42, "message": "hello"}},
        )

    async def test_send_group_segments_reply_action(self) -> None:
        action = ReplyAction(
            session=SessionRef(platform="qq", scope="group", conversation_id="group:100:200", channel_id="100", user_id="200"),
            segments=({"type": "at", "data": {"qq": "200"}}, {"type": "text", "data": {"text": " hi"}}),
        )

        await self.adapter.send_action(action)

        self.assertEqual(
            self.connection.sent_payloads[-1],
            {
                "action": "send_group_msg",
                "params": {
                    "group_id": 100,
                    "message": [
                        {"type": "at", "data": {"qq": "200"}},
                        {"type": "text", "data": {"text": " hi"}},
                    ],
                },
            },
        )

    async def test_send_group_face_action(self) -> None:
        action = FaceAction(
            session=SessionRef(platform="qq", scope="group", conversation_id="group:100:0", channel_id="100"),
            face_id="14",
        )

        await self.adapter.send_action(action)

        self.assertEqual(
            self.connection.sent_payloads[-1],
            {
                "action": "send_group_msg",
                "params": {
                    "group_id": 100,
                    "message": [
                        {"type": "face", "data": {"id": "14"}},
                    ],
                },
            },
        )

    async def test_send_group_mface_action(self) -> None:
        action = MfaceAction(
            session=SessionRef(platform="qq", scope="group", conversation_id="group:100:0", channel_id="100"),
            emoji_id="991",
            emoji_package_id="7",
            key="native-key",
            summary="开心",
        )

        await self.adapter.send_action(action)

        self.assertEqual(
            self.connection.sent_payloads[-1],
            {
                "action": "send_group_msg",
                "params": {
                    "group_id": 100,
                    "message": [
                        {
                            "type": "mface",
                            "data": {
                                "emoji_id": "991",
                                "emoji_package_id": "7",
                                "key": "native-key",
                                "summary": "开心",
                            },
                        },
                    ],
                },
            },
        )

    async def test_run_disconnect_and_ready_delegate_to_connection(self) -> None:
        self.assertTrue(self.adapter.is_ready())
        await self.adapter.run()
        await self.adapter.disconnect()
        self.assertTrue(self.connection.ran)
        self.assertTrue(self.connection.disconnected)
        self.assertFalse(self.adapter.is_ready())

    def test_attach_inbound_event_normalizes_onebot_message(self) -> None:
        event = MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "group",
                "message_id": 777,
                "user_id": 12345,
                "group_id": 54321,
                "self_id": 99999,
                "raw_message": "hello",
                "message": [{"type": "text", "data": {"text": "hello"}}],
            }
        )

        inbound_event = self.adapter.attach_inbound_event(event)

        self.assertIsNotNone(inbound_event)
        self.assertEqual(inbound_event.platform, "qq")
        self.assertEqual(inbound_event.adapter, "napcat")
        self.assertEqual(inbound_event.session.key, "group:54321:12345")


if __name__ == "__main__":
    unittest.main()
