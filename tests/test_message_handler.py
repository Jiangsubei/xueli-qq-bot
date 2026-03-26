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

from src.core.config import AppConfig, EmojiConfig, VisionServiceConfig
from src.core.models import Conversation, MessageHandlingPlan
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
        handler = self.create_handler(ai_client=DummyAIClient(responses=[AIResponse(content="收到")]), app_config=AppConfig())
        event = build_event("你好")

        result = await handler.get_ai_response(event)

        self.assertEqual("ai", result.source)
        self.assertEqual("收到", result.text)

    async def test_analyze_event_images_detects_and_persists_stickers(self):
        storage_dir = self.make_storage_dir()
        metrics = RuntimeMetrics()
        vision_client = AvailableDummyVisionClient(
            results=[
                DummyVisionResult(
                    per_image_descriptions=["一张生气小猫表情包"],
                    merged_description="一张生气小猫表情包",
                    success_count=1,
                    failure_count=0,
                    sticker_flags=[True],
                    sticker_confidences=[0.97],
                    sticker_reasons=["典型 reaction image"],
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
        event = build_event("看图", image_count=1, message_id=9)

        result = await handler.analyze_event_images(event, "看图")

        self.assertEqual(1, result["sticker_count"])
        self.assertEqual([True], result["sticker_flags"])
        snapshot = metrics.snapshot()
        self.assertEqual(1, snapshot["emoji_detected"])
        self.assertEqual(1, snapshot["emoji_total"])
        self.assertEqual(1, snapshot["emoji_pending_classification"])
        await handler.close()

    async def test_plan_emoji_follow_up_skips_non_ai_reply_sources(self):
        handler = self.create_handler()
        event = build_event("你好", message_type="group")
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


if __name__ == "__main__":
    unittest.main()

