from __future__ import annotations

import re
import unittest

from src.core.models import MessageEvent
from src.core.platform_models import InboundEvent, PlatformCapabilities, SenderRef, SessionRef
from src.handlers.conversation_session_manager import ConversationSessionManager
from src.handlers.message_handler import MessageHandler


class _ReplyPipelineStub:
    def _build_recent_history_text(self, **kwargs):
        del kwargs
        return ""

    def _extract_reusable_vision_analysis(self, **kwargs):
        del kwargs
        return {}


class MessageHandlerInboundEventTests(unittest.IsolatedAsyncioTestCase):
    def _build_handler(self) -> MessageHandler:
        handler = MessageHandler.__new__(MessageHandler)
        handler.session_manager = ConversationSessionManager()
        handler.at_pattern = re.compile(r"\[CQ:at,qq=\d+\]")
        handler.reply_pipeline = _ReplyPipelineStub()
        group_reply = type("_GroupReplyConfig", (), {"only_reply_when_at": True})()
        handler.app_config = type("_AppConfig", (), {"group_reply": group_reply})()
        handler.vision_enabled = lambda: False
        handler._get_assistant_name = lambda: "雪梨"
        handler._sync_active_conversations_metric = lambda count=None: None
        handler._group_planner_available = lambda: False
        return handler

    def _attach_custom_inbound(self, event: MessageEvent) -> InboundEvent:
        inbound = InboundEvent(
            platform="api",
            adapter="openapi",
            event_type="message",
            message_kind="text",
            session=SessionRef(platform="api", scope="private", conversation_id="api:session:1", user_id="external-user"),
            sender=SenderRef(user_id="external-user", display_name="外部用户"),
            text="来自接口的文本",
            capabilities=PlatformCapabilities(supports_text=True),
        )
        setattr(event, "_inbound_event", inbound)
        return inbound

    def test_extract_user_message_prefers_attached_inbound_text(self) -> None:
        handler = self._build_handler()
        event = MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "private",
                "message_id": 1,
                "user_id": 42,
                "self_id": 100,
                "raw_message": "onebot text",
                "message": [{"type": "text", "data": {"text": "onebot text"}}],
            }
        )
        self._attach_custom_inbound(event)

        self.assertEqual(handler.extract_user_message(event), "来自接口的文本")

    async def test_build_message_context_prefers_inbound_session_and_sender(self) -> None:
        handler = self._build_handler()
        event = MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "private",
                "message_id": 2,
                "user_id": 42,
                "self_id": 100,
                "raw_message": "onebot text",
                "message": [{"type": "text", "data": {"text": "onebot text"}}],
                "sender": {"nickname": "OneBot 用户"},
            }
        )
        inbound = self._attach_custom_inbound(event)

        context = await handler.build_message_context(event, include_memory=False)

        self.assertEqual(context.user_message, "来自接口的文本")
        self.assertEqual(context.conversation_key, inbound.session.key)
        self.assertEqual(context.current_sender_label, "42（外部用户）")

    def test_should_process_group_message_when_attached_inbound_mentions_self(self) -> None:
        handler = self._build_handler()
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
        inbound = self._attach_custom_inbound(event)
        inbound = InboundEvent(
            platform=inbound.platform,
            adapter=inbound.adapter,
            event_type=inbound.event_type,
            message_kind=inbound.message_kind,
            session=SessionRef(
                platform="api",
                scope="group",
                conversation_id="api:group:room-1:external-user",
                user_id="external-user",
                account_id="assistant-1",
                channel_id="room-1",
            ),
            sender=inbound.sender,
            text=inbound.text,
            message_id=inbound.message_id,
            reply_to_message_id=inbound.reply_to_message_id,
            segments=inbound.segments,
            attachments=inbound.attachments,
            mentioned_user_ids=("assistant-1",),
            capabilities=inbound.capabilities,
            metadata=inbound.metadata,
            raw_event=inbound.raw_event,
        )
        setattr(event, "_inbound_event", inbound)

        self.assertTrue(handler.should_process(event))

    def test_build_temporal_context_uses_restored_previous_session_time(self) -> None:
        handler = self._build_handler()
        event = MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "private",
                "message_id": 3,
                "user_id": 42,
                "self_id": 100,
                "time": 10_000,
                "raw_message": "继续",
                "message": [{"type": "text", "data": {"text": "继续"}}],
            }
        )
        conversation = handler._get_conversation(handler._get_conversation_key(event))
        conversation.restored_session_pending = True
        conversation.restored_previous_session_time = 8_000.0
        conversation.add_message("user", "旧消息", timestamp=9_000.0, restored=True)

        temporal_context = handler._build_temporal_context(event=event, conversation=conversation)

        self.assertEqual(temporal_context.previous_message_time, 9_000.0)
        self.assertEqual(temporal_context.previous_session_time, 8_000.0)
        self.assertEqual(temporal_context.session_gap_bucket, "recent")


if __name__ == "__main__":
    unittest.main()
