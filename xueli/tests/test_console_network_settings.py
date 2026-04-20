from __future__ import annotations

import unittest
from unittest.mock import patch

from src.webui.console.services import save_network_settings


class ConsoleNetworkSettingsTests(unittest.TestCase):
    def test_save_network_settings_migrates_legacy_napcat_section(self) -> None:
        raw = {
            "napcat": {
                "ws_url": "ws://127.0.0.1:8095",
                "http_url": "http://127.0.0.1:6700",
            },
            "ai_service": {"api_base": "https://example.com", "model": "gpt"},
        }
        written = {}

        def capture_write(payload):
            written.update(payload)

        with patch("src.webui.console.services._load_config_document", return_value=(object(), raw)), patch(
            "src.webui.console.services._write_validated_config",
            side_effect=capture_write,
        ):
            result = save_network_settings(
                {
                    "adapter": "api",
                    "platform": "api",
                    "ws_url": "ws://127.0.0.1:9100",
                    "http_url": "http://127.0.0.1:9200",
                }
            )

        self.assertTrue(result["ok"])
        self.assertNotIn("napcat", written)
        self.assertEqual(
            written["adapter_connection"],
            {
                "adapter": "api",
                "platform": "api",
                "ws_url": "ws://127.0.0.1:9100",
                "http_url": "http://127.0.0.1:9200",
            },
        )

    def test_save_network_settings_updates_existing_adapter_connection_section(self) -> None:
        raw = {
            "adapter_connection": {
                "ws_url": "ws://127.0.0.1:8095",
                "http_url": "http://127.0.0.1:6700",
            },
            "ai_service": {"api_base": "https://example.com", "model": "gpt"},
        }
        written = {}

        def capture_write(payload):
            written.update(payload)

        with patch("src.webui.console.services._load_config_document", return_value=(object(), raw)), patch(
            "src.webui.console.services._write_validated_config",
            side_effect=capture_write,
        ):
            save_network_settings(
                {
                    "adapter": "napcat",
                    "platform": "qq",
                    "ws_url": "ws://127.0.0.1:9300",
                    "http_url": "http://127.0.0.1:9400",
                }
            )

        self.assertEqual(
            written["adapter_connection"],
            {
                "adapter": "napcat",
                "platform": "qq",
                "ws_url": "ws://127.0.0.1:9300",
                "http_url": "http://127.0.0.1:9400",
            },
        )


if __name__ == "__main__":
    unittest.main()
