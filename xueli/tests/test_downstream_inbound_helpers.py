from __future__ import annotations

import unittest

from src.core.models import MessageEvent
from src.handlers.conversation_plan_coordinator import ConversationPlanCoordinator
from src.handlers.conversation_session_manager import ConversationSessionManager
from src.handlers.reply_pipeline import ReplyPipeline


class _PlannerStub:
    pass


class _ReplyPipelineHostStub:
    def _get_sender_display_name(self, event):
        del event
        return "标准事件用户"

    def _has_image_input(self, event):
        del event
        return True


class DownstreamInboundHelperTests(unittest.IsolatedAsyncioTestCase):
    def _group_event(self) -> MessageEvent:
        return MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "group",
                "message_id": 10,
                "user_id": 42,
                "group_id": 100,
                "self_id": 999,
                "raw_message": "onebot text",
                "message": [{"type": "text", "data": {"text": "onebot text"}}],
                "sender": {"card": "OneBot 用户"},
            }
        )

    def test_reply_pipeline_prefers_host_sender_and_image_helpers(self) -> None:
        pipeline = ReplyPipeline(_ReplyPipelineHostStub())
        event = self._group_event()

        self.assertEqual(pipeline._current_user_label(event), "42（标准事件用户）")
        self.assertTrue(pipeline._event_has_image(event))

    async def test_conversation_plan_coordinator_prefers_injected_event_helpers(self) -> None:
        coordinator = ConversationPlanCoordinator(
            planner=_PlannerStub(),
            session_manager=ConversationSessionManager(),
            event_text_getter=lambda event: "标准事件原文",
            sender_display_name_getter=lambda event: "标准事件用户",
            has_image_getter=lambda event: True,
            image_count_getter=lambda event: 2,
            image_file_ids_getter=lambda event: ["img-a", "img-b"],
        )
        event = self._group_event()

        info = coordinator.build_planner_message_info(event, "clean text")
        current = await coordinator._build_current_message(event=event, user_message="clean text")

        self.assertEqual(info["raw_text"], "标准事件原文")
        self.assertTrue(info["raw_has_image"])
        self.assertEqual(info["raw_image_count"], 2)
        self.assertEqual(info["image_file_ids"], ["img-a", "img-b"])
        self.assertEqual(current["speaker_name"], "标准事件用户")


if __name__ == "__main__":
    unittest.main()
