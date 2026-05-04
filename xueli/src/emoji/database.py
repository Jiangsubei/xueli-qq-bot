from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.models import MessageEvent, MessageSegment
from src.services.image_client import ImageClient

from .models import EmojiEmotionResult, EmojiRecord

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EmojiDatabase:
    def __init__(
        self,
        storage_path: str,
        http_url: str,
        *,
        max_stored_emojis: int = 100,
        overflow_policy: str = "replace_oldest",
    ) -> None:
        self.root = Path(storage_path)
        self.db_path = self.root / "emojis.db"
        self.http_url = http_url
        self.max_stored = max_stored_emojis
        self.overflow_policy = overflow_policy
        self._image_client = ImageClient()
        self._init_db()

    def _init_db(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS emojis (
                    emoji_id TEXT PRIMARY KEY,
                    emoji_id_str TEXT NOT NULL,
                    package_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    summary TEXT DEFAULT '',
                    local_path TEXT DEFAULT '',
                    description TEXT DEFAULT '',
                    emotion_status TEXT DEFAULT 'pending',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    usage_count INTEGER DEFAULT 1,
                    auto_reply_count INTEGER DEFAULT 0,
                    last_auto_reply_at TEXT DEFAULT '',
                    message_id TEXT DEFAULT '',
                    user_id TEXT DEFAULT '',
                    group_id TEXT DEFAULT '',
                    disabled INTEGER DEFAULT 0,
                    review_status TEXT DEFAULT 'pending',
                    manual_weight REAL DEFAULT 1.0,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS emoji_emotions (
                    emoji_id TEXT PRIMARY KEY,
                    primary_emotion TEXT DEFAULT '',
                    confidence REAL DEFAULT 0.0,
                    reason TEXT DEFAULT '',
                    all_emotions TEXT DEFAULT '[]',
                    secondary_emotions TEXT DEFAULT '[]',
                    intensity REAL DEFAULT 0.5,
                    emotion_error TEXT DEFAULT '',
                    updated_at TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (emoji_id) REFERENCES emojis(emoji_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS emoji_reply_intents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    emoji_id TEXT NOT NULL,
                    reply_tone TEXT NOT NULL,
                    reply_emotion TEXT NOT NULL,
                    reply_intent TEXT NOT NULL,
                    UNIQUE(emoji_id, reply_intent),
                    FOREIGN KEY (emoji_id) REFERENCES emojis(emoji_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_emojis_package ON emojis(package_id);
                CREATE INDEX IF NOT EXISTS idx_emojis_status ON emojis(emotion_status);
                CREATE INDEX IF NOT EXISTS idx_emojis_review ON emojis(review_status);
                CREATE INDEX IF NOT EXISTS idx_intents_intent ON emoji_reply_intents(reply_intent);
                CREATE INDEX IF NOT EXISTS idx_intents_tone ON emoji_reply_intents(reply_tone);
                """
            )
            conn.commit()

    def _connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _row_to_record(self, row: sqlite3.Row) -> EmojiRecord:
        return EmojiRecord(
            emoji_id=row["emoji_id"],
            emoji_id_str=row["emoji_id_str"],
            package_id=row["package_id"],
            key=row["key"],
            summary=row["summary"] or "",
            local_path=row["local_path"] or "",
            description=row["description"] or "",
            emotion_status=row["emotion_status"] or "pending",
            primary_emotion="",
            confidence=0.0,
            reply_tones=[],
            reply_intents=[],
            usage_count=int(row["usage_count"] or 1),
            auto_reply_count=int(row["auto_reply_count"] or 0),
            disabled=bool(row["disabled"]),
            review_status=row["review_status"] or "pending",
            manual_weight=float(row["manual_weight"] or 1.0),
            first_seen_at=row["first_seen_at"] or "",
            last_seen_at=row["last_seen_at"] or "",
            last_auto_reply_at=row["last_auto_reply_at"] or "",
            message_id=int(row["message_id"] or 0),
            user_id=int(row["user_id"] or 0),
            group_id=int(row["group_id"] or 0) if row["group_id"] else None,
        )

    @staticmethod
    def _load_json(value: str) -> List[str]:
        if not value:
            return []
        try:
            parsed = json.loads(value)
            return list(parsed) if isinstance(parsed, list) else []
        except (json.JSONDecodeError, ValueError):
            return []

    def _build_emoji_id(self, native: Dict[str, str]) -> str:
        emoji_id_str = str(native.get("emoji_id", "")).strip()
        package_id = str(native.get("emoji_package_id", "")).strip()
        key = str(native.get("key", "")).strip()
        parts = [v for v in ("mface", package_id, emoji_id_str, key) if v]
        return ":".join(parts)

    def save_mface(self, event: MessageEvent, segment: MessageSegment) -> Optional[EmojiRecord]:
        native = segment.data
        emoji_id = self._build_emoji_id(native)
        now = _now_iso()

        with self._connection() as conn:
            existing = conn.execute(
                "SELECT emoji_id FROM emojis WHERE emoji_id=?", (emoji_id,)
            ).fetchone()

            if existing:
                conn.execute(
                    """UPDATE emojis SET
                        last_seen_at=?, usage_count=usage_count+1,
                        message_id=?, user_id=?, group_id=?,
                        emotion_status=CASE WHEN emotion_status='pending' THEN 'pending' ELSE emotion_status END
                    WHERE emoji_id=?""",
                    (now, str(event.message_id), str(event.user_id),
                     str(event.raw_data.get("group_id", "")), emoji_id),
                )
            else:
                count = conn.execute(
                    "SELECT COUNT(*) FROM emojis WHERE disabled=0"
                ).fetchone()[0]
                if count >= self.max_stored:
                    if self.overflow_policy == "reject_new":
                        return None
                    oldest = conn.execute(
                        "SELECT emoji_id, local_path FROM emojis WHERE disabled=0 ORDER BY last_seen_at ASC LIMIT 1"
                    ).fetchone()
                    if oldest:
                        conn.execute("DELETE FROM emojis WHERE emoji_id=?", (oldest[0],))
                        lp = oldest[1]
                        if lp and Path(lp).exists():
                            Path(lp).unlink(missing_ok=True)

                conn.execute(
                    """INSERT INTO emojis
                        (emoji_id, emoji_id_str, package_id, key, summary,
                         emotion_status,
                         first_seen_at, last_seen_at, message_id, user_id, group_id)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        emoji_id,
                        native.get("emoji_id", "").strip(),
                        native.get("emoji_package_id", "").strip(),
                        native.get("key", "").strip(),
                        native.get("summary", "").strip(),
                        "pending",
                        now, now,
                        str(event.message_id),
                        str(event.user_id),
                        str(event.raw_data.get("group_id", "")),
                    ),
                )
            conn.commit()

        return self.get_record(emoji_id)

    async def download_preview_image(self, emoji_id: str, key: str) -> Optional[str]:
        url = await self._image_client.get_mface_image_url(key, self.http_url)
        if not url:
            return None

        image_bytes = await self._image_client.download_image_from_url(url)
        if not image_bytes:
            return None

        local_dir = self.root / "images"
        local_dir.mkdir(parents=True, exist_ok=True)
        ext = "gif" if b"GIF" in image_bytes[:20] else "png"
        file_hash = hashlib.md5(emoji_id.encode()).hexdigest()[:12]
        local_path = local_dir / f"{file_hash}.{ext}"
        Path(local_path).write_bytes(image_bytes)

        with self._connection() as conn:
            conn.execute(
                "UPDATE emojis SET local_path=? WHERE emoji_id=?",
                (str(local_path), emoji_id),
            )
            conn.commit()

        return str(local_path)

    def update_description(self, emoji_id: str, description: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE emojis SET description=?, updated_at=datetime('now') WHERE emoji_id=?",
                (description, emoji_id),
            )
            conn.commit()

    def update_emotion(self, emoji_id: str, result: EmojiEmotionResult) -> None:
        with self._connection() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO emoji_emotions
                    (emoji_id, primary_emotion, confidence, reason,
                     all_emotions, secondary_emotions, intensity, emotion_error,
                     updated_at)
                    VALUES (?,?,?,?,?,?,?,?,datetime('now'))""",
                (
                    emoji_id,
                    result.primary_emotion,
                    float(result.confidence or 0.0),
                    result.reason,
                    json.dumps(result.all_emotions or [], ensure_ascii=False),
                    json.dumps(result.secondary_emotions or [], ensure_ascii=False),
                    float(result.intensity or 0.5),
                    getattr(result, "emotion_error", None) or "",
                ),
            )
            conn.execute(
                "UPDATE emojis SET emotion_status='classified', updated_at=datetime('now') WHERE emoji_id=?",
                (emoji_id,),
            )
            conn.commit()

    def update_emotion_status(self, emoji_id: str, status: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE emojis SET emotion_status=?, updated_at=datetime('now') WHERE emoji_id=?",
                (status, emoji_id),
            )
            conn.commit()

    def add_reply_intent(self, emoji_id: str, tone: str, emotion: str, intent: str) -> None:
        with self._connection() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO emoji_reply_intents
                    (emoji_id, reply_tone, reply_emotion, reply_intent)
                    VALUES (?,?,?,?)""",
                (emoji_id, tone, emotion, intent),
            )
            conn.commit()

    def get_record(self, emoji_id: str) -> Optional[EmojiRecord]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM emojis WHERE emoji_id=?", (emoji_id,)
            ).fetchone()
            if not row:
                return None
            record = self._row_to_record(row)

            emotion_row = conn.execute(
                "SELECT * FROM emoji_emotions WHERE emoji_id=?", (emoji_id,)
            ).fetchone()
            if emotion_row:
                record.primary_emotion = emotion_row["primary_emotion"] or ""
                record.confidence = float(emotion_row["confidence"] or 0.0)
                record.emotion_candidates = self._load_json(emotion_row["all_emotions"])

            intent_rows = conn.execute(
                "SELECT reply_intent FROM emoji_reply_intents WHERE emoji_id=?", (emoji_id,)
            ).fetchall()
            record.reply_intents = [r["reply_intent"] for r in intent_rows]
            record.reply_tones = list({r["reply_tone"] for r in intent_rows})

            return record

    def list_pending(self, *, limit: int = 1) -> List[EmojiRecord]:
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT * FROM emojis
                    WHERE emotion_status='pending' AND disabled=0
                    ORDER BY last_seen_at ASC LIMIT ?""",
                (max(1, limit),),
            ).fetchall()
            return [self._row_to_record(r) for r in rows]

    def get_local_path(self, emoji_id: str) -> Optional[str]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT local_path FROM emojis WHERE emoji_id=?", (emoji_id,)
            ).fetchone()
            return row["local_path"] if row else None

    def find_reply_candidates(
        self,
        *,
        target_intent: str,
        target_tone: str,
        target_emotion: str,
    ) -> List[EmojiRecord]:
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT e.* FROM emojis e
                    INNER JOIN emoji_emotions ee ON e.emoji_id = ee.emoji_id
                    LEFT JOIN emoji_reply_intents ri ON e.emoji_id = ri.emoji_id
                    WHERE e.emotion_status='classified'
                      AND e.disabled=0
                      AND e.review_status='pending'
                    LIMIT 200""",
            ).fetchall()
            candidates = [self._row_to_record(r) for r in rows]

            if target_intent:
                exact = [c for c in candidates if target_intent in c.reply_intents]
                if exact:
                    return self._sort_candidates(exact)

            if target_tone:
                tone_match = [c for c in candidates if target_tone in c.reply_tones]
                if tone_match:
                    return self._sort_candidates(tone_match)

            if target_emotion:
                emotion_match = [
                    c for c in candidates
                    if c.primary_emotion == target_emotion
                    or target_emotion in c.emotion_candidates
                ]
                return self._sort_candidates(emotion_match)

            return []

    def find_by_intent(self, intent: str) -> List[EmojiRecord]:
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT e.* FROM emojis e
                    INNER JOIN emoji_emotions ee ON e.emoji_id = ee.emoji_id
                    LEFT JOIN emoji_reply_intents ri ON e.emoji_id = ri.emoji_id
                    WHERE e.emotion_status='classified'
                      AND e.disabled=0
                      AND e.review_status='pending'
                    LIMIT 200""",
            ).fetchall()
            candidates = [self._row_to_record(r) for r in rows]

            exact = [c for c in candidates if intent in c.reply_intents]
            if exact:
                return self._sort_candidates(exact)[: self.max_stored]

            tone = intent.split("-")[0] if "-" in intent else ""
            emotion = intent.split("-")[1] if "-" in intent else ""

            if tone:
                tone_match = [c for c in candidates if tone in c.reply_tones]
                if tone_match:
                    return self._sort_candidates(tone_match)[: self.max_stored]

            if emotion:
                emotion_match = [
                    c for c in candidates
                    if c.primary_emotion == emotion or emotion in c.emotion_candidates
                ]
                return self._sort_candidates(emotion_match)[: self.max_stored]

            return []

    def mark_auto_reply_sent(self, emoji_id: str) -> Optional[EmojiRecord]:
        with self._connection() as conn:
            conn.execute(
                """UPDATE emojis SET
                    auto_reply_count=auto_reply_count+1,
                    last_auto_reply_at=?,
                    updated_at=datetime('now')
                    WHERE emoji_id=?""",
                (_now_iso(), emoji_id),
            )
            conn.commit()
        return self.get_record(emoji_id)

    def stats(self) -> Dict[str, int]:
        with self._connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM emojis").fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM emojis WHERE emotion_status='pending' AND disabled=0"
            ).fetchone()[0]
            classified = conn.execute(
                "SELECT COUNT(*) FROM emojis WHERE emotion_status='classified' AND disabled=0"
            ).fetchone()[0]
            disabled = conn.execute(
                "SELECT COUNT(*) FROM emojis WHERE disabled=1"
            ).fetchone()[0]
        return {
            "emoji_total": total,
            "emoji_pending_classification": pending,
            "emoji_classified": classified,
            "emoji_disabled": disabled,
        }

    def has_emoji_data(self) -> bool:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM emojis WHERE emotion_status='classified' AND disabled=0 LIMIT 1"
            ).fetchone()
            return row is not None

    def _sort_candidates(self, items: List[EmojiRecord]) -> List[EmojiRecord]:
        return sorted(
            items,
            key=lambda c: (c.auto_reply_count, c.last_auto_reply_at, c.emoji_id),
        )
