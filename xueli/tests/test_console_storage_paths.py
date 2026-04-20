from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from src.webui.console import services


class ConsoleStoragePathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from django.conf import settings

        if not settings.configured:
            settings.configure(
                WEBUI_CONFIG_PATH=r"C:\Users\Jiangsubei\Desktop\xueli\xueli\config\config.toml",
                SECRET_KEY="test",
                USE_TZ=True,
            )

    def test_resolve_storage_path_anchors_relative_paths_to_xueli_root(self) -> None:
        config_path = Path(r"C:\Users\Jiangsubei\Desktop\xueli\xueli\config\config.toml")
        with patch.object(services.settings, "WEBUI_CONFIG_PATH", str(config_path), create=True):
            self.assertEqual(
                services._resolve_storage_path("../data/memories"),
                Path(r"C:\Users\Jiangsubei\Desktop\xueli\data\memories"),
            )
            self.assertEqual(
                services._resolve_storage_path("../data/emojis"),
                Path(r"C:\Users\Jiangsubei\Desktop\xueli\data\emojis"),
            )
            self.assertEqual(
                services._resolve_storage_path("../data/runtime"),
                Path(r"C:\Users\Jiangsubei\Desktop\xueli\data\runtime"),
            )

    def test_resolve_storage_path_keeps_absolute_path(self) -> None:
        absolute = Path(r"C:\Users\Jiangsubei\Desktop\xueli\data\memories")
        self.assertEqual(services._resolve_storage_path(str(absolute)), absolute)


if __name__ == "__main__":
    unittest.main()
