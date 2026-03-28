import unittest

from tests.test_support import (
    DummyAIClient,
    DummyImageClient,
    DummyMemoryManager,
    DummyPlanner,
    DummyVisionClient,
    DummyVisionResult,
    build_event,
)

from src.core.config import AppConfig, VisionServiceConfig
from src.core.models import MessageHandlingPlan, MessageType
from src.core.runtime_metrics import RuntimeMetrics
from src.handlers.message_handler import MessageHandler
from src.services.ai_client import AIResponse


class AvailableDummyVisionClient(DummyVisionClient):
    def is_available(self):
        return True

    def status(self):
        return "enabled"


class ReplyPipelineTests(unittest.IsolatedAsyncioTestCase):
    def create_handler(
        self,
        *,
        ai_client=None,
        image_client=None,
        vision_client=None,
        memory_manager=None,
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
            app_config=app_config or AppConfig(
                vision_service=VisionServiceConfig(
                    enabled=True,
                    api_base="https://vision.example.com/v1",
                    api_key="sk-vision",
                    model="vision-test",
                )
            ),
        )

    async def test_private_image_message_uses_text_only_after_vision_analysis(self):
        ai_client = DummyAIClient()
        vision_client = AvailableDummyVisionClient(
            results=[
                DummyVisionResult(
                    per_image_descriptions=["第1张是一只猫坐在窗边"],
                    merged_description="一只猫坐在窗边看外面",
                    success_count=1,
                    failure_count=0,
                )
            ]
        )
        handler = self.create_handler(ai_client=ai_client, vision_client=vision_client)
        event = build_event("", image_count=1, message_id=1)

        prepared = await handler.reply_pipeline.prepare_request(event=event, user_message="")

        self.assertIn("图片摘要", prepared.model_user_message)
        self.assertIn("一只猫坐在窗边看外面", prepared.model_user_message)
        self.assertEqual("user", prepared.messages[-1]["role"])
        self.assertIsInstance(prepared.messages[-1]["content"], str)
        self.assertIn("一只猫坐在窗边看外面", prepared.history_user_message)

    async def test_system_prompt_contains_private_session_and_user_id(self):
        handler = self.create_handler()
        event = build_event("hello", user_id=321, message_id=11)

        prepared = await handler.reply_pipeline.prepare_request(event=event, user_message="hello")

        system_prompt = prepared.messages[0]["content"]
        self.assertIn("\u4f1a\u8bdd: private", system_prompt)
        self.assertIn("\u7528\u6237ID: 321", system_prompt)
        self.assertNotIn("\u7fa4ID:", system_prompt)

    async def test_system_prompt_contains_group_session_user_and_group_id(self):
        handler = self.create_handler()
        event = build_event("group hello", message_type=MessageType.GROUP.value, user_id=654, message_id=12)

        prepared = await handler.reply_pipeline.prepare_request(event=event, user_message="group hello")

        system_prompt = prepared.messages[0]["content"]
        self.assertIn("\u4f1a\u8bdd: group", system_prompt)
        self.assertIn("\u7528\u6237ID: 654", system_prompt)
        self.assertIn("\u7fa4ID: 456", system_prompt)

    async def test_private_pure_image_vision_failure_returns_fallback_without_model_call(self):
        ai_client = DummyAIClient()
        vision_client = AvailableDummyVisionClient(
            results=[
                DummyVisionResult(
                    per_image_descriptions=[],
                    merged_description="",
                    success_count=0,
                    failure_count=1,
                    source="vision_error",
                    error="timeout",
                )
            ]
        )
        handler = self.create_handler(ai_client=ai_client, vision_client=vision_client)
        event = build_event("", image_count=1, message_id=2)

        response = await handler.reply_pipeline.execute(event=event, user_message="")

        self.assertEqual([], ai_client.chat_calls)
        self.assertEqual("fallback", response.source)
        self.assertIn("\u770b\u4e0d\u6e05", response.text)

    async def test_private_text_and_image_without_vision_uses_text_only(self):
        ai_client = DummyAIClient(responses=[AIResponse(content="收到")])
        image_client = DummyImageClient(base64_by_file={"image-9-1.jpg": "base64:cat"})
        app_config = AppConfig(vision_service=VisionServiceConfig(enabled=True, model="vision-test"))
        handler = self.create_handler(ai_client=ai_client, image_client=image_client, app_config=app_config)
        event = build_event("看这个", image_count=1, message_id=9)

        prepared = await handler.reply_pipeline.prepare_request(event=event, user_message="看这个")

        self.assertEqual([], image_client.processed_segments)
        self.assertEqual("看这个", prepared.model_user_message)
        self.assertEqual("看这个", prepared.history_user_message)
        self.assertEqual("看这个", prepared.messages[-1]["content"])

    async def test_private_pure_image_without_vision_returns_fallback_and_skips_memory(self):
        ai_client = DummyAIClient()
        memory_manager = DummyMemoryManager()
        app_config = AppConfig(vision_service=VisionServiceConfig(enabled=False))
        handler = self.create_handler(ai_client=ai_client, memory_manager=memory_manager, app_config=app_config)
        event = build_event("", image_count=1, message_id=10)

        response = await handler.reply_pipeline.execute(event=event, user_message="")

        self.assertEqual([], ai_client.chat_calls)
        self.assertEqual([], memory_manager.registered_turns)
        self.assertEqual("fallback", response.source)
        self.assertTrue(response.text.strip())

    async def test_group_reply_reuses_planner_vision_result(self):
        ai_client = DummyAIClient()
        vision_client = AvailableDummyVisionClient()
        metrics = RuntimeMetrics()
        handler = self.create_handler(
            ai_client=ai_client,
            vision_client=vision_client,
            runtime_metrics=metrics,
        )
        event = build_event(
            "这个呢",
            image_count=1,
            message_type=MessageType.GROUP.value,
            message_id=3,
            user_id=456,
        )
        plan = MessageHandlingPlan(
            action="reply",
            reason="planner",
            reply_context={
                "window_messages": [
                    {
                        "message_id": 3,
                        "user_id": "456",
                        "text": "这个呢",
                        "is_latest": True,
                        "has_image": True,
                        "image_count": 1,
                        "per_image_descriptions": ["第1张是聊天记录截图"],
                        "merged_description": "一张聊天记录截图",
                        "vision_available": True,
                        "vision_failure_count": 0,
                        "vision_success_count": 1,
                        "vision_source": "vision",
                        "vision_error": "",
                    }
                ]
            },
        )

        prepared = await handler.reply_pipeline.prepare_request(
            event=event,
            user_message="这个呢",
            plan=plan,
        )

        self.assertEqual([], vision_client.calls)
        self.assertIn("一张聊天记录截图", prepared.model_user_message)
        self.assertEqual(1, metrics.snapshot()["vision_reused_from_plan"])

    async def test_history_uses_enhanced_text_but_memory_registration_keeps_original_text(self):
        ai_client = DummyAIClient(responses=[AIResponse(content="收到")])
        memory_manager = DummyMemoryManager()
        vision_client = AvailableDummyVisionClient(
            results=[
                DummyVisionResult(
                    per_image_descriptions=["第1张是商品照片"],
                    merged_description="一张商品照片",
                    success_count=1,
                    failure_count=0,
                )
            ]
        )
        handler = self.create_handler(
            ai_client=ai_client,
            vision_client=vision_client,
            memory_manager=memory_manager,
        )
        event = build_event("看这个", image_count=1, message_id=4)

        response = await handler.reply_pipeline.execute(event=event, user_message="看这个")

        conversation = handler._get_conversation(handler._get_conversation_key(event))
        self.assertEqual("ai", response.source)
        self.assertEqual("收到", response.text)
        self.assertIn("看这个", conversation.messages[0]["content"])
        self.assertIn("一张商品照片", conversation.messages[0]["content"])
        self.assertEqual("看这个", memory_manager.registered_turns[0]["user_message"])
        self.assertEqual("收到", memory_manager.registered_turns[0]["assistant_message"])
        self.assertEqual([str(event.user_id)], memory_manager.extraction_scheduled_for)


if __name__ == "__main__":
    unittest.main()
