from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.models import MessageEvent, MessageSegment

from .models import EmojiEmotionResult, EmojiRecord


class EmojiRepository:
    """Persist detected sticker images and metadata on the local filesystem."""

    def __init__(self, storage_path: str) -> None:
        self.root = Path(storage_path)
        self.images_dir = self.root / "images"
        self.index_path = self.root / "index.json"
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        await asyncio.to_thread(self.images_dir.mkdir, parents=True, exist_ok=True)
        if not self.index_path.exists():
            await self._write_index({"version": 2, "items": {}})

    async def save_detected_emoji(
        self,
        *,
        event: MessageEvent,
        segment: MessageSegment,
        image_base64: str,
        description: str,
        sticker_confidence: float,
        sticker_reason: str,
    ) -> EmojiRecord:
        image_bytes = base64.b64decode(image_base64)
        sha256 = hashlib.sha256(image_bytes).hexdigest()
        emoji_id = sha256[:24]
        file_ext = self._guess_extension(image_bytes, segment)
        relative_image_path = f"images/{emoji_id}{file_ext}"
        now = self._now()

        async with self._lock:
            index = await self._read_index()
            items = index.setdefault("items", {})
            existing = items.get(emoji_id)

            if existing:
                record = EmojiRecord.from_dict(existing)
                record.last_seen_at = now
                record.usage_count += 1
                record.message_id = event.message_id
                record.message_type = event.message_type
                record.user_id = event.user_id
                record.group_id = event.group_id
                record.raw_segment = dict(segment.data or {})
                if description:
                    record.description = description
                if sticker_reason:
                    record.sticker_reason = sticker_reason
                record.sticker_confidence = max(record.sticker_confidence, float(sticker_confidence or 0.0))
            else:
                image_path = self.root / relative_image_path
                await asyncio.to_thread(image_path.write_bytes, image_bytes)
                record = EmojiRecord(
                    emoji_id=emoji_id,
                    sha256=sha256,
                    image_path=relative_image_path,
                    file_ext=file_ext,
                    description=description,
                    sticker_confidence=float(sticker_confidence or 0.0),
                    sticker_reason=sticker_reason,
                    first_seen_at=now,
                    last_seen_at=now,
                    message_id=event.message_id,
                    message_type=event.message_type,
                    user_id=event.user_id,
                    group_id=event.group_id,
                    raw_segment=dict(segment.data or {}),
                )

            items[emoji_id] = record.to_dict()
            index["version"] = 2
            await self._write_index(index)
            return record

    async def list_pending(self, *, limit: int = 1) -> List[EmojiRecord]:
        async with self._lock:
            index = await self._read_index()
            items = index.get("items", {})
            pending = [
                EmojiRecord.from_dict(item)
                for item in items.values()
                if item.get("emotion_status") == "pending" and not item.get("disabled", False)
            ]
        pending.sort(key=lambda item: (item.last_seen_at, item.emoji_id))
        return pending[: max(1, int(limit))]

    async def get_pending_count(self) -> int:
        async with self._lock:
            index = await self._read_index()
            items = index.get("items", {})
            return sum(
                1
                for item in items.values()
                if item.get("emotion_status") == "pending" and not item.get("disabled", False)
            )

    async def update_emotion(self, emoji_id: str, result: EmojiEmotionResult) -> Optional[EmojiRecord]:
        async with self._lock:
            index = await self._read_index()
            items = index.get("items", {})
            item = items.get(emoji_id)
            if not item:
                return None
            record = EmojiRecord.from_dict(item)
            record.emotion_status = "classified"
            record.primary_emotion = result.primary_emotion
            record.emotion_confidence = float(result.confidence or 0.0)
            record.emotion_reason = result.reason
            record.emotion_candidates = list(result.all_emotions or [])
            record.reply_tones = list(result.reply_tones or [])
            record.reply_intents = list(result.reply_intents or [])
            record.emotion_error = ""
            items[emoji_id] = record.to_dict()
            await self._write_index(index)
            return record

    async def mark_classification_failed(self, emoji_id: str, error: str) -> Optional[EmojiRecord]:
        async with self._lock:
            index = await self._read_index()
            items = index.get("items", {})
            item = items.get(emoji_id)
            if not item:
                return None
            record = EmojiRecord.from_dict(item)
            record.emotion_status = "failed"
            record.emotion_error = str(error or "").strip()
            items[emoji_id] = record.to_dict()
            await self._write_index(index)
            return record

    async def get_record(self, emoji_id: str) -> Optional[EmojiRecord]:
        async with self._lock:
            index = await self._read_index()
            item = index.get("items", {}).get(emoji_id)
            if not item:
                return None
            return EmojiRecord.from_dict(item)

    async def get_image_base64(self, emoji_id: str) -> Optional[str]:
        record = await self.get_record(emoji_id)
        if not record:
            return None
        image_bytes = await asyncio.to_thread((self.root / record.image_path).read_bytes)
        return base64.b64encode(image_bytes).decode("utf-8")

    async def get_image_path(self, emoji_id: str) -> Optional[str]:
        record = await self.get_record(emoji_id)
        if not record:
            return None
        return str((self.root / record.image_path).resolve())

    async def find_reply_candidates(
        self,
        *,
        target_intent: str,
        target_tone: str,
        target_emotion: str,
    ) -> List[EmojiRecord]:
        async with self._lock:
            index = await self._read_index()
            items = [
                EmojiRecord.from_dict(item)
                for item in index.get("items", {}).values()
                if item.get("emotion_status") == "classified" and not item.get("disabled", False)
            ]

        exact_intent = [
            item for item in items if target_intent and target_intent in set(item.reply_intents or [])
        ]
        if exact_intent:
            return self._sort_candidates(exact_intent)

        tone_match = [
            item for item in items if target_tone and target_tone in set(item.reply_tones or [])
        ]
        if tone_match:
            return self._sort_candidates(tone_match)

        emotion_match = [
            item
            for item in items
            if target_emotion
            and (
                item.primary_emotion == target_emotion
                or target_emotion in set(item.emotion_candidates or [])
            )
        ]
        return self._sort_candidates(emotion_match)

    async def mark_auto_reply_sent(self, emoji_id: str) -> Optional[EmojiRecord]:
        async with self._lock:
            index = await self._read_index()
            items = index.get("items", {})
            item = items.get(emoji_id)
            if not item:
                return None
            record = EmojiRecord.from_dict(item)
            record.auto_reply_count += 1
            record.last_auto_reply_at = self._now()
            items[emoji_id] = record.to_dict()
            await self._write_index(index)
            return record

    async def stats(self) -> Dict[str, int]:
        async with self._lock:
            index = await self._read_index()
            items = list(index.get("items", {}).values())
        return {
            "emoji_total": len(items),
            "emoji_pending_classification": sum(
                1 for item in items if item.get("emotion_status") == "pending" and not item.get("disabled", False)
            ),
            "emoji_disabled": sum(1 for item in items if item.get("disabled", False)),
            "emoji_classified": sum(1 for item in items if item.get("emotion_status") == "classified"),
        }

    async def _read_index(self) -> Dict[str, Any]:
        def _read() -> Dict[str, Any]:
            if not self.index_path.exists():
                return {"version": 2, "items": {}}
            raw = self.index_path.read_text(encoding="utf-8")
            if not raw.strip():
                return {"version": 2, "items": {}}
            data = json.loads(raw)
            if not isinstance(data, dict):
                return {"version": 2, "items": {}}
            data.setdefault("version", 2)
            data.setdefault("items", {})
            return data

        return await asyncio.to_thread(_read)

    async def _write_index(self, payload: Dict[str, Any]) -> None:
        def _write() -> None:
            self.root.mkdir(parents=True, exist_ok=True)
            self.images_dir.mkdir(parents=True, exist_ok=True)
            self.index_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        await asyncio.to_thread(_write)

    def _sort_candidates(self, items: List[EmojiRecord]) -> List[EmojiRecord]:
        return sorted(items, key=lambda item: (item.auto_reply_count, item.last_auto_reply_at, item.emoji_id))

    def _guess_extension(self, image_bytes: bytes, segment: MessageSegment) -> str:
        file_name = str(segment.data.get("file") or segment.data.get("file_id") or "").lower()
        suffix = Path(file_name).suffix.lower()
        if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
            return suffix
        if image_bytes.startswith(b"\x89PNG"):
            return ".png"
        if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
            return ".gif"
        if image_bytes.startswith(b"RIFF") and b"WEBP" in image_bytes[:16]:
            return ".webp"
        if image_bytes.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        return ".img"

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()
