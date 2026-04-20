from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Set

logger = logging.getLogger(__name__)


class MemoryTaskManager:
    """Track and coordinate memory-related background tasks."""

    def __init__(self) -> None:
        self._tasks: Set[asyncio.Task] = set()

    def create_task(self, awaitable: Awaitable[Any], *, name: str | None = None) -> asyncio.Task:
        task = asyncio.create_task(awaitable, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def flush(self) -> None:
        tasks = [task for task in self._tasks if not task.done()]
        if not tasks:
            return
        logger.debug("等待 memory 后台任务完成：数量=%s", len(tasks))
        await asyncio.gather(*tasks, return_exceptions=True)

    async def cancel_all(self) -> None:
        tasks = [task for task in self._tasks if not task.done()]
        if not tasks:
            return

        logger.debug("取消 memory 后台任务：数量=%s", len(tasks))
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    def count(self) -> int:
        return len([task for task in self._tasks if not task.done()])
