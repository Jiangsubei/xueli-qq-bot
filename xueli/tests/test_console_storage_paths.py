from __future__ import annotations

import os
import tempfile
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
                SECRET_KEY="test",
                USE_TZ=True,
            )

    def test_resolve_storage_path_anchors_relative_paths_to_xueli_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "xueli" / "config" / "config.toml"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.touch()

            with patch.object(services.settings, "WEBUI_CONFIG_PATH", str(config_path), create=True):
                result = services._resolve_storage_path("../data/memories")
                expected = (config_path.parent.parent.parent / "data" / "memories").resolve()
                self.assertEqual(result, expected)

    def test_resolve_storage_path_keeps_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            absolute = Path(tmpdir) / "data" / "memories"
            result = services._resolve_storage_path(str(absolute))
            self.assertEqual(result, absolute.resolve())


if __name__ == "__main__":
    unittest.main()
