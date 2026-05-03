from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.core.models import MessageEvent, MessageSegment
from src.emoji.database import EmojiDatabase


class EmojiDatabaseNativeRefsTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_native_mface_reference_with_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db = EmojiDatabase(tmp_dir, http_url="http://127.0.0.1:6700")
            event = MessageEvent.from_dict(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "message_id": 1002,
                    "user_id": 84,
                    "group_id": 199,
                    "message": [
                        {
                            "type": "mface",
                            "data": {
                                "emoji_id": "991",
                                "emoji_package_id": "7",
                                "key": "native-key",
                                "summary": "开心",
                            },
                        }
                    ],
                }
            )

            record = db.save_mface(
                event=event,
                segment=MessageSegment.mface(
                    emoji_id="991",
                    emoji_package_id="7",
                    key="native-key",
                    summary="开心",
                ),
            )

            self.assertIsNotNone(record)
            self.assertEqual(record.emoji_id_str, "991")
            self.assertEqual(record.package_id, "7")
            self.assertEqual(record.key, "native-key")
            self.assertEqual(record.summary, "开心")
            self.assertEqual(record.emotion_status, "pending")
            self.assertTrue(Path(tmp_dir, "emojis.db").exists())


if __name__ == "__main__":
    unittest.main()
