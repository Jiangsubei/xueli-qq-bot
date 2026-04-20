from __future__ import annotations

import re
import unittest

from src.adapters.api.adapter import ApiAdapter
from src.core.dispatcher import EventDispatcher
from src.core.platform_bridge import build_message_event_from_inbound
from src.core.platform_normalizers import get_attached_inbound_event
from src.handlers.conversation_session_manager import ConversationSessionManager
from src.handlers.message_handler import MessageHandler


class _ReplyPipelineStub:
    def _build_recent_history_text(self, **kwargs):
        del kwargs
        return ""

    def _extract_reusable_vision_analysis(self, **kwargs):
        del kwargs
        return {}


class ApiIngressBridgeTests(unittest.IsolatedAsyncioTestCase):
    def _build_handler(self) -> MessageHandler:
        handler = MessageHandler.__new__(MessageHandler)
        handler.session_manager = ConversationSessionManager()
        handler.at_pattern = re.compile(r"\[CQ:at,qq=\d+\]")
        handler.reply_pipeline = _ReplyPipelineStub()
        handler.vision_enabled = lambda: False
        handler._get_assistant_name = lambda: "雪梨"
        handler._sync_active_conversations_metric = lambda count=None: None
        return handler

    def _normalized_api_event(self):
        adapter = ApiAdapter()
        inbound_event = adapter.normalize_inbound_payload(
            {
                "event_type": "message",
                "message_id": "api-msg-3",
                "text": "hello from api bridge",
                "session": {
                    "scope": "private",
                    "conversation_id": "api:session:bridge-1",
                    "user_id": "external-user",
                },
                "sender": {
                    "user_id": "external-user",
                    "display_name": "桥接用户",
                },
            }
        )
        self.assertIsNotNone(inbound_event)
        return inbound_event

    def test_build_message_event_from_inbound_preserves_attached_event(self) -> None:
        inbound_event = self._normalized_api_event()

        event = build_message_event_from_inbound(inbound_event, self_id="assistant-1")

        self.assertEqual(event.message_type, "private")
        self.assertEqual(event.user_id, "external-user")
        self.assertEqual(event.self_id, "assistant-1")
        self.assertEqual(event.extract_text(), "hello from api bridge")
        self.assertIs(get_attached_inbound_event(event), inbound_event)

    async def test_dispatch_inbound_event_enters_existing_dispatcher_chain(self) -> None:
        inbound_event = self._normalized_api_event()
        dispatcher = EventDispatcher()
        seen = {}

        @dispatcher.on_message
        async def capture_message(event):
            seen["event"] = event
            seen["inbound"] = get_attached_inbound_event(event)

        await dispatcher.dispatch_inbound_event(inbound_event, self_id="assistant-1")

        self.assertEqual(seen["event"].extract_text(), "hello from api bridge")
        self.assertIs(seen["inbound"], inbound_event)

    async def test_message_handler_build_context_works_with_bridged_api_event(self) -> None:
        inbound_event = self._normalized_api_event()
        event = build_message_event_from_inbound(inbound_event, self_id="assistant-1")
        handler = self._build_handler()

        context = await handler.build_message_context(event, include_memory=False)

        self.assertEqual(context.user_message, "hello from api bridge")
        self.assertEqual(context.conversation_key, "api:session:bridge-1")
        self.assertEqual(context.current_sender_label, "external-user（桥接用户）")


if __name__ == "__main__":
    unittest.main()
