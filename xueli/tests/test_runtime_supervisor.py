from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from src.core.runtime_supervisor import BotRuntimeSupervisor


class _FakeBotRuntime:
    instances = []

    def __init__(self, *, manage_signals: bool = True, config_obj=None):
        self.manage_signals = manage_signals
        self.config_obj = config_obj
        self._initialized = False
        self._closed = asyncio.Event()
        self.closed = False
        _FakeBotRuntime.instances.append(self)

    async def run(self) -> None:
        self._initialized = True
        await self._closed.wait()

    async def close(self) -> None:
        self.closed = True
        self._closed.set()


class BotRuntimeSupervisorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _FakeBotRuntime.instances = []

    async def test_start_and_stop_bot_runtime(self) -> None:
        supervisor = BotRuntimeSupervisor()

        with patch("src.core.runtime_supervisor.BotRuntime", _FakeBotRuntime), patch(
            "src.core.runtime_supervisor.Config",
            return_value=object(),
        ):
            start_result = await supervisor.start_bot()
            self.assertEqual(start_result["state"], "running")
            self.assertEqual(supervisor.get_state()["state"], "running")
            self.assertEqual(len(_FakeBotRuntime.instances), 1)
            self.assertFalse(_FakeBotRuntime.instances[0].manage_signals)

            stop_result = await supervisor.stop_bot()
            self.assertEqual(stop_result["state"], "stopped")
            self.assertTrue(_FakeBotRuntime.instances[0].closed)
            self.assertEqual(supervisor.get_state()["state"], "stopped")

    async def test_restart_replaces_runtime_instance(self) -> None:
        supervisor = BotRuntimeSupervisor()

        with patch("src.core.runtime_supervisor.BotRuntime", _FakeBotRuntime), patch(
            "src.core.runtime_supervisor.Config",
            return_value=object(),
        ):
            await supervisor.start_bot()
            first_bot = _FakeBotRuntime.instances[0]

            restart_result = await supervisor.restart_bot()
            self.assertEqual(restart_result["state"], "running")
            self.assertEqual(len(_FakeBotRuntime.instances), 2)
            self.assertTrue(first_bot.closed)
            self.assertIsNot(_FakeBotRuntime.instances[-1], first_bot)

            await supervisor.shutdown()


if __name__ == "__main__":
    unittest.main()
