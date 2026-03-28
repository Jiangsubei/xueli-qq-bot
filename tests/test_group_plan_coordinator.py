import unittest

from tests.test_support import RecordingPlanner, build_event

from src.core.config import GroupReplyConfig
from src.core.models import MessageType
from src.handlers.conversation_session_manager import ConversationSessionManager
from src.handlers.group_plan_coordinator import GroupPlanCoordinator


class GroupPlanCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def build_coordinator(self, planner, *, image_analyzer=None, **config_kwargs):
        config = GroupReplyConfig(
            plan_request_interval=0.0,
            plan_request_max_parallel=1,
            plan_context_message_count=config_kwargs.pop("plan_context_message_count", 5),
            **config_kwargs,
        )
        return GroupPlanCoordinator(
            planner=planner,
            session_manager=ConversationSessionManager(),
            image_analyzer=image_analyzer,
            group_reply_config=config,
        )

    async def test_two_messages_trigger_two_planner_calls(self):
        planner = RecordingPlanner()
        coordinator = self.build_coordinator(planner, plan_context_message_count=5)

        first_plan = await coordinator.plan_group_message(
            build_event("first", message_type=MessageType.GROUP.value, message_id=1, user_id=111),
            "first",
        )
        second_plan = await coordinator.plan_group_message(
            build_event("second", message_type=MessageType.GROUP.value, message_id=2, user_id=222),
            "second",
        )

        self.assertEqual("reply", first_plan.action)
        self.assertEqual("reply", second_plan.action)
        self.assertEqual(2, len(planner.calls))
        self.assertEqual(["first"], [item["text_content"] for item in planner.calls[0]["window_messages"]])
        self.assertEqual(["first", "second"], [item["text_content"] for item in planner.calls[1]["window_messages"]])

        await coordinator.close()

    async def test_context_count_zero_only_includes_current_message(self):
        planner = RecordingPlanner()
        coordinator = self.build_coordinator(planner, plan_context_message_count=0)

        await coordinator.plan_group_message(
            build_event("hello", message_type=MessageType.GROUP.value, message_id=10, user_id=111),
            "hello",
        )
        await coordinator.plan_group_message(
            build_event("world", message_type=MessageType.GROUP.value, message_id=11, user_id=222),
            "world",
        )

        self.assertEqual(2, len(planner.calls))
        self.assertEqual(1, len(planner.calls[1]["window_messages"]))
        self.assertEqual("world", planner.calls[1]["window_messages"][0]["text_content"])
        await coordinator.close()

    async def test_assistant_history_is_visible_to_next_plan(self):
        planner = RecordingPlanner()
        coordinator = self.build_coordinator(planner, plan_context_message_count=5)

        await coordinator.plan_group_message(
            build_event("????", message_type=MessageType.GROUP.value, message_id=20, user_id=123),
            "????",
        )
        await coordinator.record_assistant_reply(456, "???????")
        await coordinator.plan_group_message(
            build_event("??", message_type=MessageType.GROUP.value, message_id=21, user_id=456),
            "??",
        )

        second_window = planner.calls[1]["window_messages"]
        self.assertEqual("assistant", second_window[-2]["speaker_role"])
        self.assertEqual("???????", second_window[-2]["text_content"])
        self.assertEqual("??", second_window[-1]["text_content"])
        await coordinator.close()

    async def test_pure_image_without_vision_is_ignored(self):
        planner = RecordingPlanner()
        coordinator = self.build_coordinator(planner, plan_context_message_count=5)

        plan = await coordinator.plan_group_message(
            build_event("", message_type=MessageType.GROUP.value, message_id=30, user_id=333, image_count=1),
            "",
        )

        self.assertEqual("ignore", plan.action)
        self.assertEqual("no_text_content", plan.source)
        self.assertEqual(0, len(planner.calls))
        await coordinator.close()

    async def test_pure_image_with_failed_vision_returns_wait(self):
        planner = RecordingPlanner()

        async def analyzer(event, user_text):
            return {
                "per_image_descriptions": [],
                "merged_description": "",
                "vision_success_count": 0,
                "vision_failure_count": len(event.get_image_segments()),
                "vision_source": "vision_error",
                "vision_error": "timeout",
                "vision_available": False,
            }

        coordinator = self.build_coordinator(planner, image_analyzer=analyzer, plan_context_message_count=5)
        plan = await coordinator.plan_group_message(
            build_event("", message_type=MessageType.GROUP.value, message_id=31, user_id=444, image_count=1),
            "",
        )

        self.assertEqual("wait", plan.action)
        self.assertEqual("vision_fallback", plan.source)
        self.assertEqual(0, len(planner.calls))
        await coordinator.close()


if __name__ == "__main__":
    unittest.main()
