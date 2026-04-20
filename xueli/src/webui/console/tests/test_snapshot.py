from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import uuid4

from django.test import SimpleTestCase

from src.core.config import (
    AIServiceConfig,
    AppConfig,
    AssistantProfileConfig,
    EmojiConfig,
    MemoryConfig,
    VisionServiceConfig,
)
from src.core.webui_snapshot import WebUISnapshotPublisher


class WebUISnapshotPublisherTests(SimpleTestCase):
    def test_publish_writes_snapshot_and_closing_state(self):
        repo_root = Path(__file__).resolve().parents[4]
        temp_root = repo_root / "tests" / "_tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        temp_dir = temp_root / f"webui_snapshot_{uuid4().hex}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        snapshot_path = temp_dir / "webui_snapshot.json"

        app_config = AppConfig(
            ai_service=AIServiceConfig(api_base="https://api.main.example/v1", api_key="key", model="main"),
            vision_service=VisionServiceConfig(
                enabled=True,
                api_base="https://api.vision.example/v1",
                api_key="vision-key",
                model="vision-model",
            ),
            emoji=EmojiConfig(enabled=True),
            assistant_profile=AssistantProfileConfig(name="雪梨", alias="小梨"),
            memory=MemoryConfig(enabled=True, storage_path="memories"),
        )
        status = {
            "ready": True,
            "connected": True,
            "uptime_seconds": 123,
            "messages_received": 9,
            "messages_replied": 6,
            "reply_parts_sent": 11,
            "message_errors": 1,
            "active_conversations": 2,
            "active_message_tasks": 1,
            "background_tasks": 3,
            "emoji_total": 8,
            "emoji_pending_classification": 2,
            "memory_reads": 4,
            "memory_writes": 5,
        }
        publisher = WebUISnapshotPublisher(
            app_config=app_config,
            status_provider=lambda: status,
            path=snapshot_path,
        )

        publisher.publish()
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        self.assertTrue(payload["ready"])
        self.assertTrue(payload["connected"])
        self.assertEqual(payload["assistant"]["name"], "雪梨")
        self.assertEqual(payload["messages"]["reply_parts_sent"], 11)
        self.assertEqual(payload["memory"]["memory_writes"], 5)

        publisher.publish(closing=True)
        closing_payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        self.assertFalse(closing_payload["ready"])
        self.assertFalse(closing_payload["connected"])
