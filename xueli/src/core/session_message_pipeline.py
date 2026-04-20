from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, Optional

from src.core.models import MessageEvent

logger = logging.getLogger(__name__)

MessageHandlerCallback = Callable[[MessageEvent, str], Awaitable[None]]


@dataclass
class QueuedMessage:
    execution_key: str
    trace_id: str
    event: MessageEvent
    handler: MessageHandlerCallback


class SessionMessagePipeline:
    """Process messages serially within the same execution shard."""

    def __init__(
        self,
        *,
        on_state_change: Optional[Callable[[Dict[str, int]], None]] = None,
    ) -> None:
        self._on_state_change = on_state_change
        self._queues: Dict[str, asyncio.Queue[QueuedMessage]] = {}
        self._workers: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def submit(
        self,
        *,
        execution_key: str,
        trace_id: str,
        event: MessageEvent,
        handler: MessageHandlerCallback,
    ) -> None:
        async with self._lock:
            if self._closed:
                logger.warning("消息流水线已关闭，忽略新消息：trace=%s key=%s", trace_id, execution_key)
                return
            queue = self._queues.get(execution_key)
            if queue is None:
                queue = asyncio.Queue()
                self._queues[execution_key] = queue
            await queue.put(
                QueuedMessage(
                    execution_key=execution_key,
                    trace_id=trace_id,
                    event=event,
                    handler=handler,
                )
            )
            worker = self._workers.get(execution_key)
            if worker is None or worker.done():
                worker = asyncio.create_task(self._run_worker(execution_key), name=f"session-pipeline-{execution_key}")
                self._workers[execution_key] = worker
            self._notify_state_change()

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            queues = list(self._queues.values())
            workers = list(self._workers.values())
        if queues:
            await asyncio.gather(*(queue.join() for queue in queues), return_exceptions=True)
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        async with self._lock:
            self._queues.clear()
            self._workers.clear()
            self._notify_state_change()

    def snapshot(self) -> Dict[str, int]:
        pending_jobs = 0
        for queue in self._queues.values():
            pending_jobs += queue.qsize()
        return {
            "active_workers": len(self._workers),
            "active_sessions": len(self._queues),
            "pending_jobs": pending_jobs,
        }

    def get_active_worker_count(self) -> int:
        return len(self._workers)

    async def _run_worker(self, execution_key: str) -> None:
        while True:
            async with self._lock:
                queue = self._queues.get(execution_key)
            if queue is None:
                return
            try:
                queued = await queue.get()
            except asyncio.CancelledError:
                raise

            try:
                await queued.handler(queued.event, queued.trace_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "消息流水线任务异常：trace=%s key=%s 错误=%s",
                    queued.trace_id,
                    execution_key,
                    exc,
                    exc_info=True,
                )
            finally:
                queue.task_done()

            async with self._lock:
                current_queue = self._queues.get(execution_key)
                if current_queue is None:
                    self._workers.pop(execution_key, None)
                    self._notify_state_change()
                    return
                if current_queue.empty():
                    self._queues.pop(execution_key, None)
                    self._workers.pop(execution_key, None)
                    self._notify_state_change()
                    return

    def _notify_state_change(self) -> None:
        if not callable(self._on_state_change):
            return
        try:
            self._on_state_change(self.snapshot())
        except Exception:
            return
