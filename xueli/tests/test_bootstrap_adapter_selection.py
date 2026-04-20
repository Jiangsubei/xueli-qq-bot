from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from src.core.bootstrap import BotBootstrapper


class BootstrapAdapterSelectionTests(unittest.TestCase):
    async def _noop_async(self, *args, **kwargs):
        del args, kwargs

    def _build_bootstrapper(self, *, adapter: str, platform: str = "qq") -> BotBootstrapper:
        bootstrapper = BotBootstrapper.__new__(BotBootstrapper)
        bootstrapper.config = types.SimpleNamespace(
            app=types.SimpleNamespace(
                adapter_connection=types.SimpleNamespace(
                    adapter=adapter,
                    platform=platform,
                    ws_url="ws://127.0.0.1:8095",
                    http_url="http://127.0.0.1:6700",
                )
            )
        )
        return bootstrapper

    def test_create_adapter_uses_api_adapter_without_ws_args(self) -> None:
        bootstrapper = self._build_bootstrapper(adapter="api", platform="api")

        with patch("src.core.bootstrap.create_adapter", return_value=object()) as create_adapter_mock:
            bootstrapper._create_adapter(
                on_message=self._noop_async,
                on_connect=self._noop_async,
                on_disconnect=self._noop_async,
            )

        create_adapter_mock.assert_called_once()
        args, kwargs = create_adapter_mock.call_args
        self.assertEqual(args[0], "api")
        self.assertNotIn("host", kwargs)
        self.assertNotIn("port", kwargs)
        self.assertIn("on_connect", kwargs)
        self.assertIn("on_disconnect", kwargs)

    def test_create_adapter_uses_napcat_ws_args(self) -> None:
        bootstrapper = self._build_bootstrapper(adapter="napcat", platform="qq")

        with patch.object(bootstrapper, "_parse_ws_endpoint", return_value=("127.0.0.1", 9100)), patch(
            "src.core.bootstrap.create_adapter",
            return_value=object(),
        ) as create_adapter_mock:
            bootstrapper._create_adapter(
                on_message=self._noop_async,
                on_connect=self._noop_async,
                on_disconnect=self._noop_async,
            )

        create_adapter_mock.assert_called_once_with(
            "napcat",
            host="127.0.0.1",
            port=9100,
            on_message=self._noop_async,
            on_connect=self._noop_async,
            on_disconnect=self._noop_async,
        )


if __name__ == "__main__":
    unittest.main()
