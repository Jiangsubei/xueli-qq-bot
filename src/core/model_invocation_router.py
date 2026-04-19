from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Optional

from src.core.message_trace import format_trace_log

logger = logging.getLogger(__name__)

ModelRunner = Callable[[], Awaitable[Any]]


def _coerce_timeout_seconds(value: Any, default: float) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        timeout = float(default)
    return max(0.001, timeout)


class ModelInvocationType(str, Enum):
    GROUP_PLAN = "group_plan"
    REPLY_GENERATION = "reply_generation"
    EMOJI_REPLY_DECISION = "emoji_reply_decision"
    VISION_ANALYSIS = "vision_analysis"
    VISION_STICKER_EMOTION = "vision_sticker_emotion"
    MEMORY_EXTRACTION = "memory_extraction"
    MEMORY_RERANK = "memory_rerank"


@dataclass
class ModelInvocationTask:
    purpose: ModelInvocationType
    trace_id: str
    session_key: str
    message_id: Any
    label: str
    timeout_seconds: float
    runner: ModelRunner
    future: asyncio.Future
    enqueued_at: float = field(default_factory=lambda: asyncio.get_running_loop().time())


class ModelInvocationRouter:
    """Dispatch model calls through per-purpose FIFO workers."""

    def __init__(
        self,
        *,
        base_timeout_seconds: float = 60.0,
        purpose_timeouts: Optional[Dict[ModelInvocationType, float]] = None,
        on_state_change: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self._base_timeout_seconds = _coerce_timeout_seconds(base_timeout_seconds, 60.0)
        self._purpose_timeouts: Dict[ModelInvocationType, float] = self._build_timeout_policy(self._base_timeout_seconds)
        for purpose, timeout_seconds in dict(purpose_timeouts or {}).items():
            self._purpose_timeouts[purpose] = _coerce_timeout_seconds(timeout_seconds, self._base_timeout_seconds)
        self._on_state_change = on_state_change
        self._queues: Dict[str, asyncio.Queue[ModelInvocationTask]] = {}
        self._workers: Dict[str, asyncio.Task] = {}
        self._running_counts: Dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @staticmethod
    def _build_timeout_policy(base_timeout_seconds: float) -> Dict[ModelInvocationType, float]:
        base_timeout = _coerce_timeout_seconds(base_timeout_seconds, 60.0)
        return {
            ModelInvocationType.GROUP_PLAN: min(base_timeout, 20.0),
            ModelInvocationType.REPLY_GENERATION: base_timeout,
            ModelInvocationType.EMOJI_REPLY_DECISION: min(base_timeout, 12.0),
            ModelInvocationType.VISION_ANALYSIS: base_timeout,
            ModelInvocationType.VISION_STICKER_EMOTION: base_timeout,
            ModelInvocationType.MEMORY_EXTRACTION: base_timeout,
            ModelInvocationType.MEMORY_RERANK: min(base_timeout, 8.0),
        }

    def timeout_seconds_for(
        self,
        purpose: ModelInvocationType,
        *,
        override: Optional[float] = None,
    ) -> float:
        if override is not None:
            return _coerce_timeout_seconds(override, self._base_timeout_seconds)
        return _coerce_timeout_seconds(
            self._purpose_timeouts.get(purpose, self._base_timeout_seconds),
            self._base_timeout_seconds,
        )

    async def submit(
        self,
        *,
        purpose: ModelInvocationType,
        runner: ModelRunner,
        trace_id: str = "",
        session_key: str = "",
        message_id: Any = "",
        label: str = "",
        timeout_seconds: Optional[float] = None,
    ) -> Any:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        purpose_key = purpose.value
        resolved_timeout = self.timeout_seconds_for(purpose, override=timeout_seconds)
        task = ModelInvocationTask(
            purpose=purpose,
            trace_id=trace_id,
            session_key=session_key,
            message_id=message_id,
            label=str(label or purpose_key),
            timeout_seconds=resolved_timeout,
            runner=runner,
            future=future,
        )

        async with self._lock:
            if self._closed:
                future.set_exception(RuntimeError("模型流水线已关闭"))
                return await future
            queue = self._queues.get(purpose_key)
            if queue is None:
                queue = asyncio.Queue()
                self._queues[purpose_key] = queue
            await queue.put(task)
            worker = self._workers.get(purpose_key)
            if worker is None or worker.done():
                worker = asyncio.create_task(self._run_worker(purpose), name=f"model-pipeline-{purpose_key}")
                self._workers[purpose_key] = worker
            pending = queue.qsize()
            self._notify_state_change_locked()

        logger.info(
            "模型请求已入队：%s purpose=%s label=%s pending=%s timeout=%.3fs",
            self._trace_log(task),
            purpose_key,
            task.label,
            pending,
            task.timeout_seconds,
        )
        return await future

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
            self._running_counts.clear()
            self._notify_state_change_locked()

    def snapshot(self) -> Dict[str, Any]:
        pending_by_purpose = {
            purpose: queue.qsize()
            for purpose, queue in self._queues.items()
        }
        running_by_purpose = {
            purpose: int(self._running_counts.get(purpose, 0))
            for purpose in set(self._queues) | set(self._running_counts)
            if int(self._running_counts.get(purpose, 0)) > 0 or purpose in self._queues
        }
        return {
            "active_workers": len(self._workers),
            "active_purposes": len(self._queues),
            "pending_jobs": sum(pending_by_purpose.values()),
            "pending_by_purpose": pending_by_purpose,
            "running_by_purpose": running_by_purpose,
            "timeout_by_purpose": {
                purpose.value: timeout
                for purpose, timeout in self._purpose_timeouts.items()
            },
        }

    async def _run_worker(self, purpose: ModelInvocationType) -> None:
        purpose_key = purpose.value
        while True:
            async with self._lock:
                queue = self._queues.get(purpose_key)
            if queue is None:
                return
            try:
                task = await queue.get()
            except asyncio.CancelledError:
                raise

            try:
                await self._run_task(task)
            finally:
                queue.task_done()

            async with self._lock:
                current_queue = self._queues.get(purpose_key)
                if current_queue is None:
                    self._workers.pop(purpose_key, None)
                    self._running_counts.pop(purpose_key, None)
                    self._notify_state_change_locked()
                    return
                if current_queue.empty() and int(self._running_counts.get(purpose_key, 0)) <= 0:
                    self._queues.pop(purpose_key, None)
                    self._workers.pop(purpose_key, None)
                    self._running_counts.pop(purpose_key, None)
                    self._notify_state_change_locked()
                    return

    async def _run_task(self, task: ModelInvocationTask) -> None:
        purpose_key = task.purpose.value
        async with self._lock:
            self._running_counts[purpose_key] = int(self._running_counts.get(purpose_key, 0)) + 1
            pending = self._queues.get(purpose_key).qsize() if self._queues.get(purpose_key) else 0
            self._notify_state_change_locked()

        logger.info(
            "模型请求开始：%s purpose=%s label=%s pending=%s timeout=%.3fs",
            self._trace_log(task),
            purpose_key,
            task.label,
            pending,
            task.timeout_seconds,
        )
        try:
            result = await asyncio.wait_for(task.runner(), timeout=task.timeout_seconds)
        except asyncio.CancelledError as exc:
            if not task.future.done():
                task.future.set_exception(exc)
            raise
        except asyncio.TimeoutError as exc:
            logger.warning(
                "模型请求超时：%s purpose=%s label=%s timeout=%.3fs",
                self._trace_log(task),
                purpose_key,
                task.label,
                task.timeout_seconds,
            )
            if not task.future.done():
                task.future.set_exception(exc)
        except Exception as exc:
            logger.error(
                "模型请求失败：%s purpose=%s label=%s 错误=%s",
                self._trace_log(task),
                purpose_key,
                task.label,
                exc,
                exc_info=True,
            )
            if not task.future.done():
                task.future.set_exception(exc)
        else:
            logger.info(
                "模型请求完成：%s purpose=%s label=%s",
                self._trace_log(task),
                purpose_key,
                task.label,
            )
            if not task.future.done():
                task.future.set_result(result)
        finally:
            async with self._lock:
                current = int(self._running_counts.get(purpose_key, 0))
                if current <= 1:
                    self._running_counts.pop(purpose_key, None)
                else:
                    self._running_counts[purpose_key] = current - 1
                self._notify_state_change_locked()

    def _notify_state_change_locked(self) -> None:
        if not callable(self._on_state_change):
            return
        try:
            self._on_state_change(self.snapshot())
        except Exception:
            return

    def _trace_log(self, task: ModelInvocationTask) -> str:
        return format_trace_log(
            trace_id=task.trace_id,
            session_key=task.session_key,
            message_id=task.message_id,
        )
