import asyncio
import unittest
from unittest.mock import patch

from tests.test_support import RecordingPlanner, build_event

from src.core.models import MessageType
from src.handlers import group_plan_coordinator as group_plan_coordinator_module
from src.handlers.conversation_session_manager import ConversationSessionManager
from src.handlers.group_plan_coordinator import GroupPlanCoordinator


class GroupPlanCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_dense_group_messages_are_buffered_into_one_planner_call(self):
        planner = RecordingPlanner()
        coordinator = GroupPlanCoordinator(
            planner=planner,
            session_manager=ConversationSessionManager(),
        )

        with patch.multiple(
            group_plan_coordinator_module.config,
            GROUP_REPLY_BURST_MERGE_ENABLED=True,
            GROUP_REPLY_BURST_WINDOW_SECONDS=0.05,
            GROUP_REPLY_BURST_MIN_MESSAGES=2,
            GROUP_REPLY_BURST_MAX_MESSAGES=5,
            GROUP_REPLY_PLAN_REQUEST_INTERVAL=0,
            GROUP_REPLY_PLAN_MAX_PARALLEL=1,
            create=True,
        ):
            first_task = asyncio.create_task(
                coordinator.plan_group_message(
                    build_event("first", message_type=MessageType.GROUP.value, message_id=1, user_id=111),
                    "first",
                )
            )
            await asyncio.sleep(0.01)
            second_task = asyncio.create_task(
                coordinator.plan_group_message(
                    build_event("second", message_type=MessageType.GROUP.value, message_id=2, user_id=222),
                    "second",
                )
            )

            first_plan, second_plan = await asyncio.gather(first_task, second_task)

        self.assertEqual(1, len(planner.calls))
        self.assertEqual("wait", first_plan.action)
        self.assertEqual("reply", second_plan.action)
        self.assertEqual(2, len(planner.calls[0]["window_messages"]))
        self.assertEqual("first", planner.calls[0]["window_messages"][0]["text"])
        self.assertEqual("second", planner.calls[0]["window_messages"][1]["text"])

        await coordinator.close()

    async def test_planner_receives_image_descriptions_before_burst_flush(self):
        planner = RecordingPlanner()
        analyzer_calls = []

        async def analyzer(event, user_text):
            analyzer_calls.append((event.message_id, user_text))
            return {
                "per_image_descriptions": ["第1张是一只猫", "第2张是聊天截图"],
                "merged_description": "两张图分别是猫和聊天截图",
                "vision_success_count": 2,
                "vision_failure_count": 0,
                "vision_source": "vision",
                "vision_error": "",
                "vision_available": True,
            }

        coordinator = GroupPlanCoordinator(
            planner=planner,
            session_manager=ConversationSessionManager(),
            image_analyzer=analyzer,
        )

        with patch.multiple(
            group_plan_coordinator_module.config,
            GROUP_REPLY_BURST_MERGE_ENABLED=True,
            GROUP_REPLY_BURST_WINDOW_SECONDS=0.05,
            GROUP_REPLY_BURST_MIN_MESSAGES=2,
            GROUP_REPLY_BURST_MAX_MESSAGES=5,
            GROUP_REPLY_PLAN_REQUEST_INTERVAL=0,
            GROUP_REPLY_PLAN_MAX_PARALLEL=1,
            create=True,
        ):
            first_task = asyncio.create_task(
                coordinator.plan_group_message(
                    build_event("看这个", message_type=MessageType.GROUP.value, message_id=10, user_id=111, image_count=2),
                    "看这个",
                )
            )
            await asyncio.sleep(0.01)
            second_task = asyncio.create_task(
                coordinator.plan_group_message(
                    build_event("后续补充", message_type=MessageType.GROUP.value, message_id=11, user_id=222),
                    "后续补充",
                )
            )
            await asyncio.gather(first_task, second_task)

        self.assertEqual([(10, "看这个")], analyzer_calls)
        window_messages = planner.calls[0]["window_messages"]
        self.assertEqual("两张图分别是猫和聊天截图", window_messages[0]["merged_description"])
        self.assertEqual(["第1张是一只猫", "第2张是聊天截图"], window_messages[0]["per_image_descriptions"])
        self.assertIn("图片摘要", window_messages[0]["text"])

        await coordinator.close()

    async def test_text_and_image_without_vision_uses_text_only_context(self):
        planner = RecordingPlanner()
        coordinator = GroupPlanCoordinator(
            planner=planner,
            session_manager=ConversationSessionManager(),
            image_analyzer=None,
        )

        with patch.multiple(
            group_plan_coordinator_module.config,
            GROUP_REPLY_BURST_MERGE_ENABLED=False,
            GROUP_REPLY_BURST_WINDOW_SECONDS=0,
            GROUP_REPLY_BURST_MIN_MESSAGES=2,
            GROUP_REPLY_BURST_MAX_MESSAGES=5,
            GROUP_REPLY_PLAN_REQUEST_INTERVAL=0,
            GROUP_REPLY_PLAN_MAX_PARALLEL=1,
            create=True,
        ):
            plan = await coordinator.plan_group_message(
                build_event("看这个", message_type=MessageType.GROUP.value, message_id=12, user_id=111, image_count=1),
                "看这个",
            )

        self.assertEqual("reply", plan.action)
        self.assertEqual(1, len(planner.calls))
        self.assertEqual("看这个", planner.calls[0]["user_message"])
        window_message = planner.calls[0]["window_messages"][0]
        self.assertEqual("看这个", window_message["text"])
        self.assertFalse(window_message["has_image"])
        self.assertFalse(window_message["image_context_enabled"])
        self.assertNotIn("[图片]", window_message["text"])

        await coordinator.close()

    async def test_pure_image_without_vision_is_ignored_without_planner(self):
        planner = RecordingPlanner()
        coordinator = GroupPlanCoordinator(
            planner=planner,
            session_manager=ConversationSessionManager(),
            image_analyzer=None,
        )

        with patch.multiple(
            group_plan_coordinator_module.config,
            GROUP_REPLY_BURST_MERGE_ENABLED=False,
            GROUP_REPLY_BURST_WINDOW_SECONDS=0,
            GROUP_REPLY_BURST_MIN_MESSAGES=2,
            GROUP_REPLY_BURST_MAX_MESSAGES=5,
            GROUP_REPLY_PLAN_REQUEST_INTERVAL=0,
            GROUP_REPLY_PLAN_MAX_PARALLEL=1,
            create=True,
        ):
            plan = await coordinator.plan_group_message(
                build_event("", message_type=MessageType.GROUP.value, message_id=20, user_id=333, image_count=1),
                "",
            )

        self.assertEqual("ignore", plan.action)
        self.assertEqual("no_text_content", plan.source)
        self.assertEqual(0, len(planner.calls))

        await coordinator.close()

    async def test_burst_window_with_only_images_without_vision_is_ignored(self):
        planner = RecordingPlanner()
        coordinator = GroupPlanCoordinator(
            planner=planner,
            session_manager=ConversationSessionManager(),
            image_analyzer=None,
        )

        with patch.multiple(
            group_plan_coordinator_module.config,
            GROUP_REPLY_BURST_MERGE_ENABLED=True,
            GROUP_REPLY_BURST_WINDOW_SECONDS=0.05,
            GROUP_REPLY_BURST_MIN_MESSAGES=2,
            GROUP_REPLY_BURST_MAX_MESSAGES=5,
            GROUP_REPLY_PLAN_REQUEST_INTERVAL=0,
            GROUP_REPLY_PLAN_MAX_PARALLEL=1,
            create=True,
        ):
            first_task = asyncio.create_task(
                coordinator.plan_group_message(
                    build_event("", message_type=MessageType.GROUP.value, message_id=30, user_id=101, image_count=1),
                    "",
                )
            )
            await asyncio.sleep(0.01)
            second_task = asyncio.create_task(
                coordinator.plan_group_message(
                    build_event("", message_type=MessageType.GROUP.value, message_id=31, user_id=102, image_count=1),
                    "",
                )
            )

            first_plan, second_plan = await asyncio.gather(first_task, second_task)

        self.assertEqual("ignore", first_plan.action)
        self.assertEqual("ignore", second_plan.action)
        self.assertEqual(0, len(planner.calls))

        await coordinator.close()

    async def test_latest_pure_image_without_vision_can_still_plan_from_earlier_text(self):
        planner = RecordingPlanner()
        coordinator = GroupPlanCoordinator(
            planner=planner,
            session_manager=ConversationSessionManager(),
            image_analyzer=None,
        )

        with patch.multiple(
            group_plan_coordinator_module.config,
            GROUP_REPLY_BURST_MERGE_ENABLED=True,
            GROUP_REPLY_BURST_WINDOW_SECONDS=0.05,
            GROUP_REPLY_BURST_MIN_MESSAGES=2,
            GROUP_REPLY_BURST_MAX_MESSAGES=5,
            GROUP_REPLY_PLAN_REQUEST_INTERVAL=0,
            GROUP_REPLY_PLAN_MAX_PARALLEL=1,
            create=True,
        ):
            first_task = asyncio.create_task(
                coordinator.plan_group_message(
                    build_event("前面有文字", message_type=MessageType.GROUP.value, message_id=40, user_id=201),
                    "前面有文字",
                )
            )
            await asyncio.sleep(0.01)
            second_task = asyncio.create_task(
                coordinator.plan_group_message(
                    build_event("", message_type=MessageType.GROUP.value, message_id=41, user_id=202, image_count=1),
                    "",
                )
            )

            await asyncio.gather(first_task, second_task)

        self.assertEqual(1, len(planner.calls))
        self.assertEqual("前面有文字", planner.calls[0]["user_message"])

        await coordinator.close()

    async def test_pure_image_with_vision_failure_prefers_wait(self):
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

        coordinator = GroupPlanCoordinator(
            planner=planner,
            session_manager=ConversationSessionManager(),
            image_analyzer=analyzer,
        )

        with patch.multiple(
            group_plan_coordinator_module.config,
            GROUP_REPLY_BURST_MERGE_ENABLED=False,
            GROUP_REPLY_BURST_WINDOW_SECONDS=0,
            GROUP_REPLY_BURST_MIN_MESSAGES=2,
            GROUP_REPLY_BURST_MAX_MESSAGES=5,
            GROUP_REPLY_PLAN_REQUEST_INTERVAL=0,
            GROUP_REPLY_PLAN_MAX_PARALLEL=1,
            create=True,
        ):
            plan = await coordinator.plan_group_message(
                build_event("", message_type=MessageType.GROUP.value, message_id=50, user_id=333, image_count=1),
                "",
            )

        self.assertEqual("wait", plan.action)
        self.assertEqual("vision_fallback", plan.source)
        self.assertEqual(0, len(planner.calls))

        await coordinator.close()


if __name__ == "__main__":
    unittest.main()
