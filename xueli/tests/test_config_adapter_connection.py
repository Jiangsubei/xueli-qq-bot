from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from src.core.config import Config


class ConfigAdapterConnectionTests(unittest.TestCase):
    def _load_config(self, toml_text: str) -> Config:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.toml"
            path.write_text(textwrap.dedent(toml_text).strip() + "\n", encoding="utf-8")
            config_obj = Config(str(path))
            self.assertIsNone(getattr(config_obj, "_load_error", None))
            return config_obj

    def test_reads_adapter_connection_section(self) -> None:
        config_obj = self._load_config(
            """
            [adapter_connection]
            adapter = "api"
            platform = "api"
            ws_url = "ws://127.0.0.1:9100"
            http_url = "http://127.0.0.1:9200"
            """
        )

        self.assertEqual(config_obj.app.adapter_connection.adapter, "api")
        self.assertEqual(config_obj.app.adapter_connection.platform, "api")
        self.assertEqual(config_obj.app.adapter_connection.ws_url, "ws://127.0.0.1:9100")
        self.assertEqual(config_obj.app.adapter_connection.http_url, "http://127.0.0.1:9200")
        self.assertEqual(config_obj.ADAPTER_CONNECTION_WS_URL, "ws://127.0.0.1:9100")
        self.assertEqual(config_obj.NAPCAT_WS_URL, "ws://127.0.0.1:9100")

    def test_falls_back_to_legacy_napcat_section(self) -> None:
        config_obj = self._load_config(
            """
            [napcat]
            ws_url = "ws://127.0.0.1:9300"
            http_url = "http://127.0.0.1:9400"
            """
        )

        self.assertEqual(config_obj.app.adapter_connection.ws_url, "ws://127.0.0.1:9300")
        self.assertEqual(config_obj.app.adapter_connection.http_url, "http://127.0.0.1:9400")
        self.assertEqual(config_obj.app.adapter_connection.adapter, "napcat")
        self.assertEqual(config_obj.app.adapter_connection.platform, "qq")
        self.assertEqual(config_obj.app.napcat.ws_url, "ws://127.0.0.1:9300")


if __name__ == "__main__":
    unittest.main()
