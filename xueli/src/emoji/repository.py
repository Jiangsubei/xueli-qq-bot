from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.models import MessageEvent, MessageSegment

from .models import EmojiEmotionResult, EmojiRecord


class EmojiRepository:
    """Persist native emoji references and metadata on the local filesystem."""

    def __init__(self, storage_path: str) -> None:
        self.root = Path(storage_path)
        self.index_path = self.root / "index.json"
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        await asyncio.to_thread(self.root.mkdir, parents=True, exist_ok=True)
        if not self.index_path.exists():
            await self._write_index({"version": 3, "items": {}})

    async def save_native_emoji(
        self,
        *,
        event: MessageEvent,
        segment: MessageSegment,
        description: str = "",
    ) -> EmojiRecord:
        native_ref = segment.get_native_sticker_ref()
        if not native_ref:
            raise ValueError("segment is not a native emoji reference")
        emoji_id = self._build_emoji_id(native_ref)
        now = self._now()
        summary_text = str(native_ref.get("summary", "")).strip()
        inferred = self._infer_emotion(summary_text, description)

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
                record.group_id = event.raw_data.get("group_id")
                record.raw_segment = dict(segment.data or {})
                record.sticker_kind = native_ref.get("kind", "")
                record.native_id = self._resolve_native_id(native_ref)
                record.emoji_package_id = str(native_ref.get("emoji_package_id", "")).strip()
                record.native_key = str(native_ref.get("key", "")).strip()
                if summary_text:
                    record.native_summary = summary_text
                if description:
                    record.description = description
                self._apply_inferred_emotion(record, inferred)
                if inferred is None and record.emotion_status == "pending":
                    record.emotion_status = "manual_only"
            else:
                record = EmojiRecord(
                    emoji_id=emoji_id,
                    sticker_kind=native_ref.get("kind", ""),
                    native_id=self._resolve_native_id(native_ref),
                    emoji_package_id=str(native_ref.get("emoji_package_id", "")).strip(),
                    native_key=str(native_ref.get("key", "")).strip(),
                    native_summary=summary_text,
                    description=description,
                    emotion_status="classified" if inferred else "manual_only",
                    first_seen_at=now,
                    last_seen_at=now,
                    message_id=event.message_id,
                    message_type=event.message_type,
                    user_id=event.user_id,
                    group_id=event.raw_data.get("group_id"),
                    raw_segment=dict(segment.data or {}),
                )
                self._apply_inferred_emotion(record, inferred)

            items[emoji_id] = record.to_dict()
            index["version"] = 3
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
        del emoji_id
        return None

    async def get_image_path(self, emoji_id: str) -> Optional[str]:
        del emoji_id
        return None

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
                if item.get("emotion_status") == "classified"
                and not item.get("disabled", False)
                and item.get("sticker_kind") in {"face", "mface"}
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
                return {"version": 3, "items": {}}
            raw = self.index_path.read_text(encoding="utf-8")
            if not raw.strip():
                return {"version": 3, "items": {}}
            data = json.loads(raw)
            if not isinstance(data, dict):
                return {"version": 3, "items": {}}
            data.setdefault("version", 3)
            data.setdefault("items", {})
            return data

        return await asyncio.to_thread(_read)

    async def _write_index(self, payload: Dict[str, Any]) -> None:
        def _write() -> None:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp_path = self.index_path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_path, self.index_path)

        await asyncio.to_thread(_write)

    def _sort_candidates(self, items: List[EmojiRecord]) -> List[EmojiRecord]:
        return sorted(items, key=lambda item: (item.auto_reply_count, item.last_auto_reply_at, item.emoji_id))

    def _build_emoji_id(self, native_ref: Dict[str, str]) -> str:
        kind = str(native_ref.get("kind", "")).strip()
        if kind == "face":
            return f"face:{str(native_ref.get('id', '')).strip()}"
        emoji_id = str(native_ref.get("emoji_id", "")).strip()
        package_id = str(native_ref.get("emoji_package_id", "")).strip()
        key = str(native_ref.get("key", "")).strip()
        parts = [value for value in ("mface", package_id, emoji_id, key) if value]
        return ":".join(parts)

    def _resolve_native_id(self, native_ref: Dict[str, str]) -> str:
        if native_ref.get("kind") == "face":
            return str(native_ref.get("id", "")).strip()
        return str(native_ref.get("emoji_id", "")).strip()

    def _apply_inferred_emotion(self, record: EmojiRecord, inferred: EmojiEmotionResult | None) -> None:
        if inferred is None:
            return
        record.emotion_status = "classified"
        record.primary_emotion = inferred.primary_emotion
        record.emotion_confidence = float(inferred.confidence or 0.0)
        record.emotion_reason = inferred.reason
        record.emotion_candidates = list(inferred.all_emotions or [])
        record.reply_tones = list(inferred.reply_tones or [])
        record.reply_intents = list(inferred.reply_intents or [])

    def _infer_emotion(self, summary: str, description: str) -> EmojiEmotionResult | None:
        text = " ".join(part for part in [summary.strip(), description.strip()] if part).strip()
        if not text:
            return None
        rules = [
            ("开心", ["庆祝", "附和"], ["笑", "开心", "高兴", "乐", "可爱", "庆", "耶", "比心"]),
            ("喜欢", ["附和"], ["爱", "喜欢", "亲亲", "抱抱", "贴贴"]),
            ("无语", ["吐槽", "收尾"], ["无语", "汗", "尴尬", "黑线"]),
            ("委屈", ["安慰"], ["委屈", "呜呜", "可怜"]),
            ("伤心", ["安慰"], ["伤心", "难过", "哭", "泪"]),
            ("生气", ["吐槽", "拒绝"], ["怒", "生气", "火", "炸毛"]),
            ("嘲讽", ["调侃"], ["嘲讽", "坏笑", "doge", "鄙视", "斜眼"]),
            ("惊讶", ["附和", "调侃"], ["惊", "震惊", "吃惊", "啊?", "问号"]),
            ("困惑", ["提醒", "附和"], ["疑惑", "困惑", "不懂", "啥", "什么"]),
        ]
        for emotion, tones, keywords in rules:
            if any(keyword in text for keyword in keywords):
                intents = [f"{tone}-{emotion}" for tone in tones]
                return EmojiEmotionResult(
                    primary_emotion=emotion,
                    confidence=0.55,
                    reason="native_summary_heuristic",
                    all_emotions=[emotion],
                    reply_tones=tones,
                    reply_intents=intents,
                )
        return None

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()
