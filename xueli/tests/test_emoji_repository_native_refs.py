from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.core.models import MessageEvent, MessageSegment
from src.emoji.repository import EmojiRepository


class EmojiRepositoryNativeRefsTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_native_face_reference_without_creating_image_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repository = EmojiRepository(tmp_dir)
            await repository.initialize()
            event = MessageEvent.from_dict(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "message_id": 1001,
                    "user_id": 42,
                    "group_id": 99,
                    "message": [{"type": "face", "data": {"id": "14"}}],
                }
            )

            record = await repository.save_native_emoji(
                event=event,
                segment=MessageSegment.face("14"),
                description="开心",
            )

            self.assertEqual(record.sticker_kind, "face")
            self.assertEqual(record.native_id, "14")
            self.assertFalse((Path(tmp_dir) / "images").exists())
            self.assertTrue((Path(tmp_dir) / "index.json").exists())

    async def test_save_native_mface_reference_with_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repository = EmojiRepository(tmp_dir)
            await repository.initialize()
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

            record = await repository.save_native_emoji(
                event=event,
                segment=MessageSegment.mface(
                    emoji_id="991",
                    emoji_package_id="7",
                    key="native-key",
                    summary="开心",
                ),
                description="开心",
            )

            self.assertEqual(record.sticker_kind, "mface")
            self.assertEqual(record.native_id, "991")
            self.assertEqual(record.emoji_package_id, "7")
            self.assertEqual(record.native_key, "native-key")
            self.assertEqual(record.emotion_status, "classified")
            self.assertIn("附和-开心", record.reply_intents)


if __name__ == "__main__":
    unittest.main()
