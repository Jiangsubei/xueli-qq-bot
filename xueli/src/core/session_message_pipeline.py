from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from src.core.models import MessageEvent

logger = logging.getLogger(__name__)

MessageHandlerCallback = Callable[[MessageEvent, str], Awaitable[None]]


@dataclass
class QueuedMessage:
    execution_key: str
    trace_id: str
    event: MessageEvent
    handler: MessageHandlerCallback
    enqueue_time: float = field(default_factory=time.time)


class SessionMessagePipeline:
    """Process messages with per-user serial execution and per-group concurrency control."""

    def __init__(
        self,
        *,
        on_state_change: Optional[Callable[[Dict[str, int]], None]] = None,
        group_max_concurrent: int = 3,
        group_queue_timeout: int = 120,
    ) -> None:
        self._on_state_change = on_state_change
        self._queues: Dict[str, asyncio.Queue[QueuedMessage]] = {}
        self._workers: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self.group_max_concurrent = group_max_concurrent
        self.group_queue_timeout = group_queue_timeout
        self._group_active: Dict[str, Set[str]] = {}

    async def submit(
        self,
        *,
        execution_key: str,
        trace_id: str,
        event: MessageEvent,
        handler: MessageHandlerCallback,
    ) -> None:
        raw_text = str(getattr(event, "raw_text", "") or "")
        is_at_mention = "@" in raw_text
        enqueue_time = time.time()

        async with self._lock:
            if self._closed:
                logger.warning("[流水线] 消息流水线已关闭，忽略新消息")
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
                    enqueue_time=enqueue_time,
                )
            )

            worker = self._workers.get(execution_key)
            if worker is None or worker.done():
                worker = asyncio.create_task(
                    self._run_worker(execution_key, is_at_mention=is_at_mention),
                    name=f"session-pipeline-{execution_key}",
                )
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

    def _extract_group_key(self, execution_key: str) -> Optional[str]:
        parts = execution_key.split(":")
        if len(parts) >= 3 and parts[1] == "group":
            platform = parts[0] if parts[0] not in ("group",) else ""
            group_id = parts[2]
            return f"{platform}:group:{group_id}" if platform else f"group:{group_id}"
        if len(parts) >= 2 and parts[0] == "group":
            return execution_key
        return None

    def _extract_user_id(self, execution_key: str) -> Optional[str]:
        parts = execution_key.split(":")
        if len(parts) >= 4 and parts[1] == "group":
            return parts[3]
        if len(parts) >= 2 and parts[0] == "private":
            return parts[1]
        return None

    async def _run_worker(self, execution_key: str, is_at_mention: bool = False) -> None:
        group_key = self._extract_group_key(execution_key)
        group_key_for_active: Optional[str] = None

        async with self._lock:
            if group_key and self.group_max_concurrent > 0:
                if group_key not in self._group_active:
                    self._group_active[group_key] = set()
                active_in_group = len(self._group_active[group_key])
                if active_in_group >= self.group_max_concurrent:
                    self._workers.pop(execution_key, None)
                    self._notify_state_change()
                    return
                self._group_active[group_key].add(execution_key)
                group_key_for_active = group_key

        try:
            while True:
                async with self._lock:
                    if group_key_for_active and self.group_max_concurrent > 0:
                        active_now = len(self._group_active.get(group_key_for_active, set()))
                        if active_now >= self.group_max_concurrent:
                            break
                    queue = self._queues.get(execution_key)
                if queue is None:
                    return
                queued = None
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
                    if queue is not None:
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
        finally:
            if group_key_for_active:
                async with self._lock:
                    if group_key_for_active in self._group_active:
                        self._group_active[group_key_for_active].discard(execution_key)
                        if not self._group_active[group_key_for_active]:
                            del self._group_active[group_key_for_active]

    def _notify_state_change(self) -> None:
        if not callable(self._on_state_change):
            return
        try:
            self._on_state_change(self.snapshot())
        except Exception:
            return
