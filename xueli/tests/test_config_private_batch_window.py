from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.core.config import Config


class ConfigPrivateBatchWindowTests(unittest.TestCase):
    def test_reads_private_batch_window_from_bot_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.toml"
            path.write_text(
                """
[ai_service]
api_base = "https://example.com"
api_key = "key"
model = "model"

[adapter_connection]
ws_url = "ws://127.0.0.1:8095"
http_url = "http://127.0.0.1:6700"

[assistant_profile]
name = "Test"

[bot_behavior]
private_batch_window_seconds = 2.5
""".strip(),
                encoding="utf-8",
            )

            config_obj = Config(str(path))
            app_config = config_obj.validate()

            self.assertEqual(app_config.bot_behavior.private_batch_window_seconds, 2.5)

    def test_reads_segmented_reply_behavior_from_bot_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.toml"
            path.write_text(
                """
[ai_service]
api_base = "https://example.com"
api_key = "key"
model = "model"

[adapter_connection]
ws_url = "ws://127.0.0.1:8095"
http_url = "http://127.0.0.1:6700"

[assistant_profile]
name = "Test"

[bot_behavior]
segmented_reply_enabled = true
max_segments = 2
first_segment_delay_min_ms = 10
first_segment_delay_max_ms = 20
followup_delay_min_seconds = 3
followup_delay_max_seconds = 5
""".strip(),
                encoding="utf-8",
            )

            config_obj = Config(str(path))
            app_config = config_obj.validate()

            self.assertTrue(app_config.bot_behavior.segmented_reply_enabled)
            self.assertEqual(app_config.bot_behavior.max_segments, 2)
            self.assertEqual(app_config.bot_behavior.first_segment_delay_min_ms, 10)
            self.assertEqual(app_config.bot_behavior.first_segment_delay_max_ms, 20)
            self.assertEqual(app_config.bot_behavior.followup_delay_min_seconds, 3.0)
            self.assertEqual(app_config.bot_behavior.followup_delay_max_seconds, 5.0)


if __name__ == "__main__":
    unittest.main()
