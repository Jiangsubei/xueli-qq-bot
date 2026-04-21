from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.core.runtime import BotRuntime


class BotRuntimeCloseOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_keeps_model_router_alive_until_memory_manager_finishes(self) -> None:
        order: list[str] = []

        runtime = BotRuntime.__new__(BotRuntime)
        runtime._closed = False
        runtime._running = True
        runtime._initialized = True
        runtime._shutdown_event = asyncio.Event()
        runtime.adapter = SimpleNamespace(disconnect=AsyncMock(side_effect=lambda: order.append("adapter.disconnect")))
        runtime.connection = None
        runtime._connection_task = None
        runtime._snapshot_task = None
        runtime.message_handler = object()
        runtime.memory_manager = object()
        runtime._message_tasks = set()
        runtime._message_pipeline = None
        runtime._model_router = SimpleNamespace(close=AsyncMock(side_effect=lambda: order.append("model_router.close")))
        runtime.runtime_metrics = SimpleNamespace(set_state=lambda **kwargs: order.append("runtime_metrics.set_state"))
        runtime.webui_snapshot = SimpleNamespace(publish=lambda **kwargs: order.append("webui_snapshot.publish"))
        runtime._sync_status_cache = lambda: order.append("sync_status_cache")

        async def cancel_message_tasks() -> None:
            order.append("cancel_message_tasks")

        runtime._cancel_message_tasks = cancel_message_tasks

        async def fake_close_resource(resource, *, label: str):
            order.append(f"close_resource:{label}")

        async def fake_cancel_task(task, *, label: str):
            order.append(f"cancel_task:{label}")

        with patch("src.core.runtime.close_resource", side_effect=fake_close_resource), patch(
            "src.core.runtime.cancel_task",
            side_effect=fake_cancel_task,
        ), patch("src.core.runtime.unregister_runtime", side_effect=lambda _runtime: order.append("unregister_runtime")):
            await runtime.close()

        self.assertEqual(
            order[:7],
            [
                "cancel_message_tasks",
                "adapter.disconnect",
                "cancel_task:connection_task",
                "cancel_task:webui_snapshot_task",
                "close_resource:message_handler",
                "close_resource:memory_manager",
                "model_router.close",
            ],
        )


if __name__ == "__main__":
    unittest.main()
