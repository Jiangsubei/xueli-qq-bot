from __future__ import annotations

import asyncio
import base64
import logging
import time
from datetime import datetime, time as local_time
from pathlib import Path
from typing import List, Optional, Tuple

from src.core.config import AppConfig
from src.core.models import MessageEvent, MessageSegment
from src.core.runtime_metrics import RuntimeMetrics
from src.services.vision_client import ImageAnalysisResult, VisionClient

from .database import EmojiDatabase
from .models import DEFAULT_EMOTION_LABELS, DEFAULT_REPLY_TONES, EmojiEmotionResult

logger = logging.getLogger(__name__)


class EmojiTaskManager:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    def create_task(self, awaitable, *, name: str | None = None) -> asyncio.Task:
        task = asyncio.create_task(awaitable, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def cancel_all(self) -> None:
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    def count(self) -> int:
        return len([task for task in self._tasks if not task.done()])


class EmojiManager:
    """Coordinate sticker persistence and idle-time emotion classification."""

    def __init__(
        self,
        *,
        vision_client: VisionClient,
        runtime_metrics: Optional[RuntimeMetrics],
        app_config: AppConfig,
    ) -> None:
        self.vision_client = vision_client
        self.runtime_metrics = runtime_metrics
        self.app_config = app_config
        emoji_config = getattr(app_config, "emoji", None)

        self.enabled = bool(emoji_config and emoji_config.enabled)
        self.capture_enabled = bool(self.enabled and getattr(emoji_config, "capture_enabled", True))
        self.classification_enabled = bool(getattr(emoji_config, "classification_enabled", False))
<<<<<<< HEAD
        http_url = getattr(app_config.adapter_connection, "http_url", "http://127.0.0.1:6700") if app_config else "http://127.0.0.1:6700"
        self.repository = EmojiDatabase(
            getattr(emoji_config, "storage_path", "data/emojis") if self.enabled else "data/emojis",
            http_url=http_url,
=======
        self.repository = EmojiRepository(
            getattr(emoji_config, "storage_path", "data/emojis") if self.enabled else "data/emojis",
>>>>>>> fc5b56b (WIP on main: 250d0b0 fix: 修复导入问题)
            max_stored_emojis=int(getattr(emoji_config, "max_stored_emojis", 100)),
            overflow_policy=str(getattr(emoji_config, "overflow_policy", "replace_oldest")),
        )
        self.idle_seconds = float(getattr(emoji_config, "idle_seconds_before_classify", 45.0))
        self.classification_interval_seconds = float(
            getattr(emoji_config, "classification_interval_seconds", 30.0)
        )
        self.classification_windows = list(getattr(emoji_config, "classification_windows", []) or [])
        self.emotion_labels = list(getattr(emoji_config, "emotion_labels", DEFAULT_EMOTION_LABELS))
        self.reply_tones = list(DEFAULT_REPLY_TONES)
        if not self.emotion_labels:
            self.emotion_labels = list(DEFAULT_EMOTION_LABELS)
        self.task_manager = EmojiTaskManager()
        self._last_activity_at = time.monotonic()
        self._worker_task: Optional[asyncio.Task] = None
        self._initialized = False

    async def initialize(self) -> None:
        if not self.enabled or self._initialized:
            return
        self._initialized = True
        await self._sync_metrics()

    def record_activity(self) -> None:
        if not self.enabled:
            return
        self._last_activity_at = time.monotonic()

    async def capture_native_emoji_references(self, *, event: MessageEvent) -> None:
        if not self.enabled or not self.capture_enabled:
            return
        if not self._initialized:
            await self.initialize()

        captured = 0
        for segment in list(event.message or []):
            if segment.type != "mface":
                continue
            record = self.repository.save_mface(event=event, segment=segment)
            if record:
                captured += 1
                self.task_manager.create_task(
                    self._download_and_classify(record),
                    name=f"emoji-{record.emoji_id}",
                )
        if captured and self.runtime_metrics:
            self.runtime_metrics.record_emoji_detection(captured)
        if captured:
            self.record_activity()
            await self._sync_metrics()

    async def process_detection_result(
        self,
        *,
        event: MessageEvent,
        image_segments: List[MessageSegment],
        base64_images: List[str],
        analysis_result: ImageAnalysisResult,
    ) -> None:
        del event, image_segments, base64_images, analysis_result
        if self.enabled:
            await self._sync_metrics()

    async def close(self) -> None:
        await self.task_manager.cancel_all()
        self._worker_task = None
        await self._sync_metrics(active_classifiers=0)

    def _ensure_worker(self) -> None:
        if not self.classification_enabled:
            return
        if self._worker_task and not self._worker_task.done():
            return
        self._worker_task = self.task_manager.create_task(
            self._classification_loop(),
            name="emoji-classification-loop",
        )
        self._sync_metrics_now(emoji_active_classifiers=1)

    async def _classification_loop(self) -> None:
        try:
            while True:
                if not self._can_run_classification_now():
                    await asyncio.sleep(self._next_wait_seconds())
                    continue

                pending = self.repository.list_pending(limit=1)
                if not pending:
                    return

                await self._classify_one(pending[0].emoji_id)
                await self._sync_metrics()
                await asyncio.sleep(max(0.01, self.classification_interval_seconds))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[表情管理] 表情包后台分类循环失败")
        finally:
            await self._sync_metrics(active_classifiers=0)
            self._worker_task = None

    async def _download_and_classify(self, record) -> None:
        emoji_id = record.emoji_id
        key = record.key
        if not key:
            return
        self.repository.update_emotion_status(emoji_id, "processing")
        try:
            local_path = await self.repository.download_preview_image(emoji_id, key)
            if not local_path or not Path(local_path).exists():
                return
            image_bytes = Path(local_path).read_bytes()
            image_b64 = base64.b64encode(image_bytes).decode()

            availability = getattr(self.vision_client, "is_available", None)
            if callable(availability) and not availability():
                return

            vision_r, emotion_r = await asyncio.gather(
                self.vision_client.analyze_images(
                    base64_images=[image_b64],
                    user_text="这是表情包图片，请描述其内容",
                ),
                self.vision_client.classify_sticker_emotion(
                    image_base64=image_b64,
                    emotion_labels=self.emotion_labels,
                ),
            )

            if vision_r:
                desc = (vision_r.merged_description or "") or (
                    (vision_r.per_image_descriptions or [""])[0] if vision_r.per_image_descriptions else ""
                )
                if desc:
                    self.repository.update_description(emoji_id, desc)

            if emotion_r:
                result = EmojiEmotionResult(
                    primary_emotion=emotion_r.get("primary_emotion", ""),
                    confidence=float(emotion_r.get("confidence", 0.0)),
                    reason=emotion_r.get("reason", ""),
                    all_emotions=list(emotion_r.get("all_emotions") or []),
                    reply_tones=list(emotion_r.get("reply_tones") or []),
                    reply_intents=list(emotion_r.get("reply_intents") or []),
                )
                self.repository.update_emotion(emoji_id, result)
                for intent in result.reply_intents:
                    parts = intent.split("-", 1) if "-" in intent else ("", "")
                    if len(parts) == 2 and parts[0] and parts[1]:
                        self.repository.add_reply_intent(emoji_id, parts[0], parts[1], intent)

            if self.runtime_metrics:
                self.runtime_metrics.record_emoji_classification(1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[表情管理] 表情包分类失败: %s", exc)
            if self.runtime_metrics:
                self.runtime_metrics.record_emoji_classification_failure(1)

    async def _classify_one(self, emoji_id: str) -> None:
        record = self.repository.get_record(emoji_id)
        if not record:
            return
        await self._download_and_classify(record)

    def _can_run_classification_now(self) -> bool:
        if not self.classification_enabled:
            return False
        if (time.monotonic() - self._last_activity_at) < self.idle_seconds:
            return False
        return self._is_within_classification_window(self._current_local_time())

    def _next_wait_seconds(self) -> float:
        idle_remaining = max(0.0, self.idle_seconds - (time.monotonic() - self._last_activity_at))
        if idle_remaining > 0:
            return min(max(idle_remaining, 0.05), 1.0)
        if not self._is_within_classification_window(self._current_local_time()):
            return min(max(self.classification_interval_seconds, 0.1), 5.0)
        return max(0.05, self.classification_interval_seconds)

    def _is_within_classification_window(self, now: local_time) -> bool:
        windows = [window for window in self.classification_windows if str(window).strip()]
        if not windows:
            return True
        current_minutes = now.hour * 60 + now.minute
        for start_minutes, end_minutes in self._parsed_windows():
            if start_minutes <= end_minutes:
                if start_minutes <= current_minutes < end_minutes:
                    return True
            elif current_minutes >= start_minutes or current_minutes < end_minutes:
                return True
        return False

    def _parsed_windows(self) -> List[Tuple[int, int]]:
        parsed: List[Tuple[int, int]] = []
        for window in self.classification_windows:
            try:
                start_text, end_text = [part.strip() for part in str(window).split("-", 1)]
                start_minutes = self._parse_clock_minutes(start_text)
                end_minutes = self._parse_clock_minutes(end_text)
            except (ValueError, TypeError):
                continue
            parsed.append((start_minutes, end_minutes))
        return parsed

    def _parse_clock_minutes(self, text: str) -> int:
        hours_text, minutes_text = text.split(":", 1)
        hours = int(hours_text)
        minutes = int(minutes_text)
        return hours * 60 + minutes

    def _current_local_time(self) -> local_time:
        return datetime.now().time()

    async def _sync_metrics(self, *, active_classifiers: Optional[int] = None) -> None:
        stats = self.repository.stats() if self.enabled else {
            "emoji_total": 0,
            "emoji_pending_classification": 0,
            "emoji_disabled": 0,
            "emoji_classified": 0,
        }
        self._sync_metrics_now(
            emoji_active_classifiers=(
                active_classifiers if active_classifiers is not None else self.task_manager.count()
            ),
            **stats,
        )

    def _sync_metrics_now(self, **kwargs) -> None:
        if self.runtime_metrics:
            self.runtime_metrics.set_state(**kwargs)
