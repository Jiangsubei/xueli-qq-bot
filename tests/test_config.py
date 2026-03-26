import json
import shutil
import textwrap
import unittest
import uuid
from pathlib import Path

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from src.core.config import (
    AppConfig,
    Config,
    ConfigValidationError,
    VisionServiceConfig,
    get_vision_service_status,
    is_vision_service_configured,
)

TMP_ROOT = Path("tests") / "_tmp"


class ConfigTests(unittest.TestCase):
    def write_config(self, content):
        temp_dir = TMP_ROOT / f"config_{uuid.uuid4().hex}"
        temp_dir.mkdir(parents=True, exist_ok=False)
        self.addCleanup(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        path = temp_dir / "config.json"
        path.write_text(textwrap.dedent(content).strip(), encoding="utf-8")
        return path

    def test_validate_supports_comments_and_emoji_reply_fields(self):
        path = self.write_config(
            """
            {
              // comment before napcat
              "napcat": {
                "ws_url": "ws://127.0.0.1:8095",
                "http_url": "http://127.0.0.1:6700"
              },
              "ai_service": {
                "api_base": "https://example.com/v1",
                "api_key": "sk-test",
                "model": "gpt-test"
              },
              "vision_service": {
                "enabled": true,
                "api_base": "https://vision.example.com/v1",
                "api_key": "sk-vision",
                "model": "vision-test"
              },
              "emoji": {
                "enabled": true,
                "capture_enabled": true,
                "classification_enabled": true,
                "classification_interval_seconds": 15,
                "classification_windows": ["01:00-06:00", "23:00-02:00"],
                "reply_enabled": true,
                "reply_cooldown_seconds": 90
              },
              "assistant_profile": {
                "name": "Claude QQ"
              },
              "memory": {
                "enabled": true,
                "read_scope": "global"
              }
            }
            """
        )

        loaded = Config(str(path)).validate()

        self.assertTrue(loaded.emoji.enabled)
        self.assertEqual(15.0, loaded.emoji.classification_interval_seconds)
        self.assertEqual(["01:00-06:00", "23:00-02:00"], loaded.emoji.classification_windows)
        self.assertTrue(loaded.emoji.reply_enabled)
        self.assertEqual(90.0, loaded.emoji.reply_cooldown_seconds)

    def test_validate_reports_aggregated_errors(self):
        path = self.write_config(
            json.dumps(
                {
                    "ai_service": {
                        "api_base": "",
                        "api_key": "sk-test",
                        "model": 123,
                    },
                    "vision_service": {
                        "enabled": "yes",
                        "extra_headers": [],
                    },
                    "emoji": {
                        "classification_windows": ["bad-window"],
                        "reply_cooldown_seconds": -1,
                    },
                    "bot_behavior": {
                        "max_context_length": 0,
                    },
                    "group_reply": {
                        "plan_request_max_parallel": 0,
                    },
                    "memory": {
                        "read_scope": "team",
                        "bm25_top_k": 2,
                        "rerank_top_k": 3,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )

        config = Config(str(path))
        with self.assertRaises(ConfigValidationError) as ctx:
            config.validate()

        errors = "\n".join(ctx.exception.errors)
        self.assertIn("ai_service.api_base", errors)
        self.assertIn("ai_service.model", errors)
        self.assertIn("vision_service.enabled", errors)
        self.assertIn("vision_service.extra_headers", errors)
        self.assertIn("emoji.classification_windows", errors)
        self.assertIn("emoji.reply_cooldown_seconds", errors)
        self.assertIn("bot_behavior.max_context_length", errors)
        self.assertIn("group_reply.plan_request_max_parallel", errors)
        self.assertIn("memory.read_scope", errors)
        self.assertIn("memory.rerank_top_k", errors)

    def test_vision_service_status_distinguishes_disabled_unconfigured_and_enabled(self):
        disabled = AppConfig(vision_service=VisionServiceConfig(enabled=False))
        unconfigured = AppConfig(vision_service=VisionServiceConfig(enabled=True, model="vision-only"))
        enabled = AppConfig(
            vision_service=VisionServiceConfig(
                enabled=True,
                api_base="https://vision.example.com/v1",
                api_key="sk-vision",
                model="vision-test",
            )
        )

        self.assertFalse(is_vision_service_configured(disabled))
        self.assertEqual("disabled", get_vision_service_status(disabled))
        self.assertFalse(is_vision_service_configured(unconfigured))
        self.assertEqual("unconfigured", get_vision_service_status(unconfigured))
        self.assertTrue(is_vision_service_configured(enabled))
        self.assertEqual("enabled", get_vision_service_status(enabled))


if __name__ == "__main__":
    unittest.main()
