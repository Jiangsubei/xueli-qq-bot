import unittest

from tests.test_support import DummyAIClient, install_dependency_stubs

install_dependency_stubs()

from src.core.config import AppConfig, VisionServiceConfig
from src.services.ai_client import AIAPIError, AIResponse
from src.services.vision_client import VisionClient


class VisionClientTests(unittest.IsolatedAsyncioTestCase):
    def build_enabled_config(self):
        return AppConfig(
            vision_service=VisionServiceConfig(
                enabled=True,
                api_base="https://vision.example.com/v1",
                api_key="sk-vision",
                model="vision-test",
            )
        )

    async def test_analyze_images_parses_structured_json_response(self):
        ai_client = DummyAIClient(
            responses=[
                AIResponse(
                    content='{"images": [{"description": "第1张是一只猫猫表情包", "is_sticker": true, "sticker_confidence": 0.97, "sticker_reason": "夸张表情和配字"}, {"description": "第2张是一张聊天截图", "is_sticker": false, "sticker_confidence": 0.08, "sticker_reason": "普通截图"}], "merged_description": "两张图分别是猫猫表情包和聊天截图"}'
                )
            ]
        )
        client = VisionClient(ai_client=ai_client, app_config=self.build_enabled_config())

        result = await client.analyze_images(base64_images=["img1", "img2"], user_text="帮我看看")

        self.assertEqual(["第1张是一只猫猫表情包", "第2张是一张聊天截图"], result.per_image_descriptions)
        self.assertEqual("两张图分别是猫猫表情包和聊天截图", result.merged_description)
        self.assertEqual([True, False], result.sticker_flags)
        self.assertAlmostEqual(0.97, result.get_sticker_confidence(0))

    async def test_analyze_images_falls_back_to_plain_text_response(self):
        ai_client = DummyAIClient(responses=[AIResponse(content="一张夜晚街景照片，街边有灯光和行人")])
        client = VisionClient(ai_client=ai_client, app_config=self.build_enabled_config())

        result = await client.analyze_images(base64_images=["img1"], user_text="")

        self.assertEqual("一张夜晚街景照片，街边有灯光和行人", result.merged_description)
        self.assertEqual(["第1张: 一张夜晚街景照片，街边有灯光和行人"], result.per_image_descriptions)
        self.assertEqual([False], result.sticker_flags)

    async def test_classify_sticker_emotion_returns_reply_profile(self):
        ai_client = DummyAIClient(
            responses=[
                AIResponse(
                    content='{"primary_emotion": "开心", "confidence": 0.91, "all_emotions": ["开心", "喜欢"], "reply_tones": ["庆祝"], "reply_intents": ["庆祝-开心"], "reason": "笑脸和庆祝动作"}'
                )
            ]
        )
        client = VisionClient(ai_client=ai_client, app_config=self.build_enabled_config())

        result = await client.classify_sticker_emotion(
            image_base64="img1",
            emotion_labels=["开心", "喜欢", "无语"],
        )

        self.assertEqual("开心", result["primary_emotion"])
        self.assertEqual(["开心", "喜欢"], result["all_emotions"])
        self.assertEqual(["庆祝"], result["reply_tones"])
        self.assertEqual(["庆祝-开心"], result["reply_intents"])
        self.assertAlmostEqual(0.91, result["confidence"])

    async def test_analyze_images_returns_failure_result_on_api_error(self):
        ai_client = DummyAIClient(responses=[AIAPIError("vision unavailable")])
        client = VisionClient(ai_client=ai_client, app_config=self.build_enabled_config())

        result = await client.analyze_images(base64_images=["img1", "img2"], user_text="帮我看看")

        self.assertEqual([], result.per_image_descriptions)
        self.assertEqual("", result.merged_description)
        self.assertEqual(2, result.failure_count)
        self.assertEqual("vision_error", result.source)
        self.assertIn("vision unavailable", result.error)

    async def test_unconfigured_service_returns_unavailable_without_calling_ai(self):
        ai_client = DummyAIClient()
        client = VisionClient(
            ai_client=ai_client,
            app_config=AppConfig(vision_service=VisionServiceConfig(enabled=True, model="vision-test")),
        )

        result = await client.analyze_images(base64_images=["img1"], user_text="看看")

        self.assertEqual("unconfigured", client.status())
        self.assertFalse(client.is_available())
        self.assertEqual("unconfigured", result.source)
        self.assertEqual([], ai_client.chat_calls)


if __name__ == "__main__":
    unittest.main()
