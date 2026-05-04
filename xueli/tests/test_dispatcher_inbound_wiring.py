from __future__ import annotations

import unittest

from src.core.dispatcher import EventDispatcher
from src.core.platform_normalizers import get_attached_inbound_event


class DispatcherInboundWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_attaches_generic_inbound_event_without_adapter(self) -> None:
        dispatcher = EventDispatcher(platform="qq", adapter_name="napcat")
        seen = {}

        @dispatcher.register_preprocessor
        def capture_preprocessor(ctx):
            seen["preprocessor_inbound"] = ctx.inbound_event

        @dispatcher.on_message
        async def capture_message(event):
            seen["event"] = event
            seen["handler_inbound"] = get_attached_inbound_event(event)

        await dispatcher.dispatch(
            {
                "post_type": "message",
                "message_type": "group",
                "message_id": 501,
                "user_id": 12345,
                "group_id": 54321,
                "self_id": 99999,
                "raw_message": "hello",
                "message": [{"type": "text", "data": {"text": "hello"}}],
                "sender": {"card": "群名片"},
            }
        )

        self.assertIsNotNone(seen.get("preprocessor_inbound"))
        self.assertIsNotNone(seen.get("handler_inbound"))
        self.assertIs(seen["preprocessor_inbound"], seen["handler_inbound"])
        self.assertEqual(seen["handler_inbound"].platform, "qq")
        self.assertEqual(seen["handler_inbound"].adapter, "napcat")
        self.assertIsNotNone(seen["handler_inbound"].session)
        self.assertEqual(seen["handler_inbound"].session.scope, "private")
        self.assertEqual(seen["handler_inbound"].session.conversation_id, "unknown")
        self.assertEqual(seen["handler_inbound"].text, "hello")

    async def test_dispatch_uses_configured_adapter_attacher(self) -> None:
        seen = {}

        def attach_from_adapter(event):
            inbound_event = get_attached_inbound_event(event)
            if inbound_event is not None:
                return inbound_event
            from src.core.platform_models import InboundEvent, PlatformCapabilities, SenderRef, SessionRef

            inbound_event = InboundEvent(
                platform="api",
                adapter="openapi",
                event_type="message",
                message_kind="text",
                session=SessionRef(platform="api", scope="private", conversation_id="api:session:1", user_id="external-user"),
                sender=SenderRef(user_id="external-user", display_name="外部用户"),
                text="from adapter hook",
                capabilities=PlatformCapabilities(supports_text=True),
            )
            setattr(event, "_inbound_event", inbound_event)
            return inbound_event

        dispatcher = EventDispatcher()
        dispatcher.configure_inbound_event_attacher(attach_from_adapter, platform="api", adapter_name="openapi")

        @dispatcher.on_message
        async def capture_message(event):
            seen["handler_inbound"] = get_attached_inbound_event(event)

        await dispatcher.dispatch(
            {
                "post_type": "message",
                "message_type": "private",
                "message_id": 601,
                "user_id": 12345,
                "self_id": 99999,
                "raw_message": "hello",
                "message": [{"type": "text", "data": {"text": "hello"}}],
            }
        )

        self.assertIsNotNone(seen.get("handler_inbound"))
        self.assertEqual(seen["handler_inbound"].platform, "api")
        self.assertEqual(seen["handler_inbound"].adapter, "openapi")
        self.assertEqual(seen["handler_inbound"].text, "from adapter hook")


if __name__ == "__main__":
    unittest.main()
