import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from tests.test_support import (
    DummyAIClient,
    DummyImageClient,
    DummyMemoryManager,
    DummyPlanner,
    DummyVisionClient,
    DummyVisionResult,
    build_event,
)

from src.core.config import AppConfig, EmojiConfig, GroupReplyConfig, GroupReplyDecisionConfig, VisionServiceConfig
from src.core.models import Conversation, MessageHandlingPlan, MessageSegment
from src.core.runtime_metrics import RuntimeMetrics
from src.handlers import message_handler as message_handler_module
from src.handlers.message_handler import MessageHandler
from src.handlers.reply_pipeline import ReplyResult
from src.services.ai_client import AIResponse

TMP_ROOT = Path("tests") / "_tmp"


class AvailableDummyVisionClient(DummyVisionClient):
    def is_available(self):
        return True

    def status(self):
        return "enabled"


class MessageHandlerTests(unittest.IsolatedAsyncioTestCase):
    def make_storage_dir(self):
        temp_dir = TMP_ROOT / f"handler_emoji_{uuid.uuid4().hex}"
        temp_dir.mkdir(parents=True, exist_ok=False)
        self.addCleanup(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        return temp_dir

    def create_handler(
        self,
        memory_manager=None,
        *,
        ai_client=None,
        image_client=None,
        vision_client=None,
        runtime_metrics=None,
        app_config=None,
    ):
        return MessageHandler(
            ai_client=ai_client or DummyAIClient(),
            image_client=image_client or DummyImageClient(),
            vision_client=vision_client,
            memory_manager=memory_manager,
            group_reply_planner=DummyPlanner(),
            runtime_metrics=runtime_metrics,
            app_config=app_config,
        )

    def test_build_assistant_identity_prompt_includes_name_and_alias(self):
        handler = self.create_handler()
        with (
            patch.object(message_handler_module.config, "get_assistant_name", return_value="Aki"),
            patch.object(message_handler_module.config, "get_assistant_alias", return_value="Autumn"),
        ):
            prompt = handler._build_assistant_identity_prompt()

        self.assertIn("Aki", prompt)
        self.assertIn("Autumn", prompt)

    async def test_load_memory_context_first_turn_only_loads_important_memories(self):
        memory_manager = DummyMemoryManager()
        handler = self.create_handler(memory_manager=memory_manager)
        conversation = Conversation()
        event = build_event("remember that I like cats")

        with patch.object(message_handler_module.config, "get_memory_read_scope", return_value="global"):
            memory_context, related_history_messages, is_first_turn = await handler._load_memory_context(
                event=event,
                user_message="remember that I like cats",
                conversation=conversation,
            )

        self.assertTrue(is_first_turn)
        self.assertIn("42", memory_context)
        self.assertIn("likes cats", memory_context)
        self.assertEqual([], related_history_messages)
        self.assertFalse(memory_manager.search_called)

    async def test_get_ai_response_returns_command_source_for_builtin_commands(self):
        handler = self.create_handler()
        event = build_event("/status")

        result = await handler.get_ai_response(event)

        self.assertIsInstance(result, ReplyResult)
        self.assertEqual("command", result.source)
        self.assertTrue(result.text.strip())

    async def test_get_ai_response_returns_ai_source_for_normal_reply(self):
        handler = self.create_handler(ai_client=DummyAIClient(responses=[AIResponse(content="??")]), app_config=AppConfig())
        event = build_event("??")

        result = await handler.get_ai_response(event)

        self.assertEqual("ai", result.source)
        self.assertEqual("??", result.text)

    async def test_analyze_event_images_detects_and_persists_stickers(self):
        storage_dir = self.make_storage_dir()
        metrics = RuntimeMetrics()
        vision_client = AvailableDummyVisionClient(
            results=[
                DummyVisionResult(
                    per_image_descriptions=["?????????"],
                    merged_description="?????????",
                    success_count=1,
                    failure_count=0,
                    sticker_flags=[True],
                    sticker_confidences=[0.97],
                    sticker_reasons=["?? reaction image"],
                )
            ]
        )
        image_client = DummyImageClient(base64_by_file={"image-9-1.jpg": "aW1nMQ=="})
        app_config = AppConfig(
            vision_service=VisionServiceConfig(
                enabled=True,
                api_base="https://vision.example.com/v1",
                api_key="sk-vision",
                model="vision-test",
            ),
            emoji=EmojiConfig(
                enabled=True,
                storage_path=str(storage_dir),
                idle_seconds_before_classify=3600.0,
                classification_interval_seconds=0.01,
            ),
        )
        handler = self.create_handler(
            image_client=image_client,
            vision_client=vision_client,
            runtime_metrics=metrics,
            app_config=app_config,
        )
        await handler.initialize()
        event = build_event("??", image_count=1, message_id=9)

        result = await handler.analyze_event_images(event, "??")

        self.assertEqual(1, result["sticker_count"])
        self.assertEqual([True], result["sticker_flags"])
        snapshot = metrics.snapshot()
        self.assertEqual(1, snapshot["emoji_detected"])
        self.assertEqual(1, snapshot["emoji_total"])
        self.assertEqual(1, snapshot["emoji_pending_classification"])
        await handler.close()

    async def test_plan_emoji_follow_up_skips_non_ai_reply_sources(self):
        handler = self.create_handler()
        event = build_event("??", message_type="group")
        plan = MessageHandlingPlan(action="reply", reason="test")

        selection = await handler.plan_emoji_follow_up(
            event,
            ReplyResult(text="/status", source="command"),
            plan=plan,
        )

        self.assertIsNone(selection)

    def test_vision_enabled_false_when_service_is_unconfigured(self):
        app_config = AppConfig(vision_service=VisionServiceConfig(enabled=True, model="vision-test"))
        handler = self.create_handler(app_config=app_config)

        self.assertFalse(handler.vision_enabled())
        self.assertEqual("unconfigured", handler.vision_status())

    async def test_group_message_without_planner_model_only_replies_when_at(self):
        app_config = AppConfig(
            group_reply=GroupReplyConfig(only_reply_when_at=False, interest_reply_enabled=True),
            group_reply_decision=GroupReplyDecisionConfig(api_base=None, api_key=None, model=None),
        )
        handler = self.create_handler(app_config=app_config)

        normal_event = build_event("???", message_type="group")
        normal_plan = await handler.plan_message(normal_event)
        self.assertEqual("ignore", normal_plan.action)
        self.assertEqual("rule", normal_plan.source)
        self.assertFalse(handler.should_process(normal_event))

        at_event = build_event("??", message_type="group")
        at_event.message.insert(0, MessageSegment.at(at_event.self_id))
        at_plan = await handler.plan_message(at_event)
        self.assertEqual("reply", at_plan.action)
        self.assertEqual("rule", at_plan.source)
        self.assertTrue(handler.should_process(at_event))

    async def test_group_repeat_echo_replies_once_without_model_planner(self):
        app_config = AppConfig(
            group_reply=GroupReplyConfig(
                only_reply_when_at=False,
                interest_reply_enabled=True,
                repeat_echo_enabled=True,
                repeat_echo_window_seconds=20.0,
                repeat_echo_min_count=2,
                repeat_echo_cooldown_seconds=90.0,
            ),
            group_reply_decision=GroupReplyDecisionConfig(api_base=None, api_key=None, model=None),
        )
        handler = self.create_handler(app_config=app_config)

        first_event = build_event("??", message_type="group", user_id=1001)
        first_plan = await handler.plan_message(first_event)
        self.assertEqual("ignore", first_plan.action)

        second_event = build_event("??", message_type="group", user_id=1002)
        second_plan = await handler.plan_message(second_event)
        self.assertEqual("reply", second_plan.action)
        self.assertEqual("repeat_echo", second_plan.source)

        reply = await handler.get_ai_response(second_event, plan=second_plan)
        self.assertEqual("??", reply.text)
        self.assertEqual("repeat_echo", reply.source)

    def test_resolve_group_at_user_for_explicit_at(self):
        handler = self.create_handler(app_config=AppConfig(group_reply=GroupReplyConfig(at_user_when_proactive_reply=False)))
        event = build_event("??", message_type="group", user_id=2024)
        plan = MessageHandlingPlan(action="reply", reason="at", reply_context={"reply_mode": "at"})

        self.assertEqual(2024, handler.resolve_group_at_user(event, plan))

    def test_resolve_group_at_user_for_proactive_reply_depends_on_config(self):
        event = build_event("??", message_type="group", user_id=2025)
        proactive_plan = MessageHandlingPlan(action="reply", reason="planner", reply_context={"reply_mode": "proactive"})

        handler_without_at = self.create_handler(app_config=AppConfig(group_reply=GroupReplyConfig(at_user_when_proactive_reply=False)))
        handler_with_at = self.create_handler(app_config=AppConfig(group_reply=GroupReplyConfig(at_user_when_proactive_reply=True)))

        self.assertIsNone(handler_without_at.resolve_group_at_user(event, proactive_plan))
        self.assertEqual(2025, handler_with_at.resolve_group_at_user(event, proactive_plan))

    def test_resolve_group_at_user_for_repeat_echo_never_mentions_user(self):
        handler = self.create_handler(app_config=AppConfig(group_reply=GroupReplyConfig(at_user_when_proactive_reply=True)))
        event = build_event("??", message_type="group", user_id=3030)
        plan = MessageHandlingPlan(action="reply", reason="repeat", reply_context={"reply_mode": "repeat_echo"})

        self.assertIsNone(handler.resolve_group_at_user(event, plan))

    async def test_reset_command_flushes_current_memory_session(self):
        memory_manager = DummyMemoryManager()
        handler = self.create_handler(memory_manager=memory_manager)
        event = build_event("/reset", message_type="group", user_id=4040)

        result = handler.command_handler.handle("/reset", event)

        self.assertTrue(result)
        self.assertEqual(
            [{"user_id": "4040", "message_type": "group", "group_id": "456"}],
            memory_manager.flush_calls,
        )


if __name__ == "__main__":
    unittest.main()
