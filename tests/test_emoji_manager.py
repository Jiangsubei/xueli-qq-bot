import asyncio
import shutil
import unittest
import uuid
from datetime import time as local_time
from pathlib import Path
from unittest.mock import patch

from tests.test_support import DummyVisionClient, DummyVisionResult, build_event, install_dependency_stubs

install_dependency_stubs()

from src.core.config import AppConfig, EmojiConfig, VisionServiceConfig
from src.core.runtime_metrics import RuntimeMetrics
from src.emoji.manager import EmojiManager

TMP_ROOT = Path("tests") / "_tmp"


class EmojiManagerTests(unittest.IsolatedAsyncioTestCase):
    def make_storage(self) -> Path:
        temp_dir = TMP_ROOT / f"emoji_{uuid.uuid4().hex}"
        temp_dir.mkdir(parents=True, exist_ok=False)
        self.addCleanup(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        return temp_dir

    def build_config(self, storage_path: Path, *, idle_seconds: float, windows=None) -> AppConfig:
        return AppConfig(
            vision_service=VisionServiceConfig(
                enabled=True,
                api_base="https://vision.example.com/v1",
                api_key="sk-vision",
                model="vision-test",
            ),
            emoji=EmojiConfig(
                enabled=True,
                storage_path=str(storage_path),
                idle_seconds_before_classify=idle_seconds,
                classification_interval_seconds=0.01,
                classification_windows=list(windows or []),
            ),
        )

    async def test_process_detection_result_saves_only_detected_stickers(self):
        storage = self.make_storage()
        metrics = RuntimeMetrics()
        vision_client = DummyVisionClient()
        manager = EmojiManager(
            vision_client=vision_client,
            runtime_metrics=metrics,
            app_config=self.build_config(storage, idle_seconds=3600.0),
        )
        await manager.initialize()

        event = build_event("看看", image_count=2, message_id=77)
        result = DummyVisionResult(
            per_image_descriptions=["一张生气猫猫表情包", "一张普通风景照片"],
            merged_description="一张表情包和一张风景图",
            success_count=2,
            failure_count=0,
            sticker_flags=[True, False],
            sticker_confidences=[0.96, 0.12],
            sticker_reasons=["夸张表情和大字文案", "普通照片"],
        )

        await manager.process_detection_result(
            event=event,
            image_segments=event.get_image_segments(),
            base64_images=["aW1nMQ==", "aW1nMg=="],
            analysis_result=result,
        )

        stats = await manager.repository.stats()
        snapshot = metrics.snapshot()
        self.assertEqual(1, stats["emoji_total"])
        self.assertEqual(1, stats["emoji_pending_classification"])
        self.assertEqual(1, snapshot["emoji_detected"])
        self.assertEqual([], vision_client.emotion_calls)
        await manager.close()

    async def test_background_classification_runs_after_idle(self):
        storage = self.make_storage()
        metrics = RuntimeMetrics()
        vision_client = DummyVisionClient()
        manager = EmojiManager(
            vision_client=vision_client,
            runtime_metrics=metrics,
            app_config=self.build_config(storage, idle_seconds=0.0),
        )
        await manager.initialize()

        event = build_event("看看", image_count=1, message_id=88)
        result = DummyVisionResult(
            per_image_descriptions=["一张开心柴犬表情包"],
            merged_description="开心柴犬表情包",
            success_count=1,
            failure_count=0,
            sticker_flags=[True],
            sticker_confidences=[0.98],
            sticker_reasons=["典型 reaction image"],
        )

        await manager.process_detection_result(
            event=event,
            image_segments=event.get_image_segments(),
            base64_images=["aW1nMQ=="],
            analysis_result=result,
        )
        await asyncio.sleep(0.08)

        stats = await manager.repository.stats()
        snapshot = metrics.snapshot()
        self.assertEqual(0, stats["emoji_pending_classification"])
        self.assertEqual(1, stats["emoji_classified"])
        self.assertEqual(1, snapshot["emoji_classified"])
        self.assertEqual(1, len(vision_client.emotion_calls))
        await manager.close()

    async def test_background_classification_respects_time_windows(self):
        storage = self.make_storage()
        metrics = RuntimeMetrics()
        vision_client = DummyVisionClient()
        manager = EmojiManager(
            vision_client=vision_client,
            runtime_metrics=metrics,
            app_config=self.build_config(storage, idle_seconds=0.0, windows=["01:00-02:00"]),
        )
        await manager.initialize()

        event = build_event("看看", image_count=1, message_id=99)
        result = DummyVisionResult(
            per_image_descriptions=["一张无语猫猫表情包"],
            merged_description="无语猫猫表情包",
            success_count=1,
            failure_count=0,
            sticker_flags=[True],
            sticker_confidences=[0.99],
            sticker_reasons=["典型 reaction image"],
        )

        with patch.object(manager, "_current_local_time", return_value=local_time(3, 0)):
            await manager.process_detection_result(
                event=event,
                image_segments=event.get_image_segments(),
                base64_images=["aW1nMQ=="],
                analysis_result=result,
            )
            await asyncio.sleep(0.05)
            blocked_stats = await manager.repository.stats()

        self.assertEqual(1, blocked_stats["emoji_pending_classification"])
        self.assertEqual(0, len(vision_client.emotion_calls))

        with patch.object(manager, "_current_local_time", return_value=local_time(1, 30)):
            await asyncio.sleep(0.08)
            allowed_stats = await manager.repository.stats()

        self.assertEqual(0, allowed_stats["emoji_pending_classification"])
        self.assertEqual(1, allowed_stats["emoji_classified"])
        await manager.close()


if __name__ == "__main__":
    unittest.main()
