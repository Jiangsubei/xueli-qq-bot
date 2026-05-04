from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from src.handlers.conversation_window_models import BufferedWindow, ConversationWindowState, WindowDispatchResult


MessageBuilder = Callable[[Any], Dict[str, Any]]
MergeBuilder = Callable[[List[Dict[str, Any]]], str]


class ConversationWindowScheduler:
    """Keep one rolling active buffer per conversation and queue sealed windows."""

    DEFAULT_AVG_REPLY_LATENCY = 5.0

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._states: Dict[str, ConversationWindowState] = {}
        self._close_tasks: Dict[str, asyncio.Task[Any]] = {}
        self._dispatch_waiters: Dict[Tuple[str, int], asyncio.Future[WindowDispatchResult]] = {}

    async def submit_event(
        self,
        *,
        conversation_key: str,
        chat_mode: str,
        event: Any,
        window_seconds: float,
        queue_expire_seconds: float,
        message_builder: MessageBuilder,
        merge_builder: MergeBuilder,
    ) -> WindowDispatchResult:
        waiter: Optional[asyncio.Future[WindowDispatchResult]] = None
        async with self._lock:
            state = self._states.setdefault(conversation_key, ConversationWindowState())
            now = time.time()
            state.last_activity_at = now

            if state.processing is None and state.queued_windows:
                dispatch, dropped, _ = self._dispatch_next_locked(
                    state=state,
                    conversation_key=conversation_key,
                    now=now,
                )
                if dispatch is not None:
                    return WindowDispatchResult(
                        status="dispatch_window",
                        window=dispatch,
                        reason="dispatched",
                        dropped_count=dropped,
                    )

            message = dict(message_builder(event))
            active_buffer = state.active_buffer
            if active_buffer is None:
                active_buffer = BufferedWindow(
                    conversation_key=conversation_key,
                    seq=state.next_seq,
                    chat_mode=chat_mode,
                    opened_at=now,
                    messages=[],
                    window_reason="buffer_opened",
                    latest_event=event,
                    min_messages=1,
                )
                state.active_buffer = active_buffer
                state.next_seq += 1
                if state.processing is None and not state.queued_windows:
                    waiter = asyncio.get_running_loop().create_future()
                    self._dispatch_waiters[(conversation_key, active_buffer.seq)] = waiter
                self._replace_close_task_locked(
                    conversation_key=conversation_key,
                    task=asyncio.create_task(
                        self._close_after_window(
                            conversation_key=conversation_key,
                            seq=active_buffer.seq,
                            window_seconds=max(0.0, float(window_seconds or 0.0)),
                            queue_expire_seconds=max(0.0, float(queue_expire_seconds or 0.0)),
                            merge_builder=merge_builder,
                            min_messages=active_buffer.min_messages,
                            average_reply_latency=self.DEFAULT_AVG_REPLY_LATENCY,
                        )
                    ),
                )

            active_buffer.messages.append(message)
            active_buffer.latest_event = event

        if waiter is None:
            return WindowDispatchResult(status="accepted_only", reason="queued")
        return await waiter

    async def mark_window_complete(self, conversation_key: str, seq: int) -> WindowDispatchResult:
        async with self._lock:
            state = self._states.get(conversation_key)
            if state is None:
                return WindowDispatchResult(status="accepted_only", reason="no_state")
            processing = state.processing
            if processing is None or int(processing.seq or 0) != int(seq or 0):
                return WindowDispatchResult(status="accepted_only", reason="not_processing")
            state.processing = None
            state.last_activity_at = time.time()
            dispatch, dropped, _ = self._dispatch_next_locked(
                state=state,
                conversation_key=conversation_key,
                now=time.time(),
            )
            if dispatch is None:
                return WindowDispatchResult(status="accepted_only", reason="queue_empty", dropped_count=dropped)
            return WindowDispatchResult(
                status="dispatch_window",
                window=dispatch,
                reason="dispatched",
                dropped_count=dropped,
            )

    async def cleanup(self, *, active_keys: Optional[Iterable[str]] = None, idle_seconds: float = 120.0) -> None:
        active_key_set = set(active_keys or [])
        limit = max(30.0, float(idle_seconds or 0.0))
        stale_keys: List[str] = []
        now = time.time()
        async with self._lock:
            for conversation_key, state in list(self._states.items()):
                if state.processing is not None:
                    continue
                if state.active_buffer is not None:
                    continue
                if state.queued_windows:
                    continue
                if active_keys is not None and conversation_key not in active_key_set:
                    stale_keys.append(conversation_key)
                    continue
                if state.last_activity_at and now - state.last_activity_at > limit:
                    stale_keys.append(conversation_key)
            for conversation_key in stale_keys:
                self._states.pop(conversation_key, None)
                task = self._close_tasks.pop(conversation_key, None)
                if task is not None and not task.done():
                    task.cancel()

    def get_states(self) -> Dict[str, ConversationWindowState]:
        return dict(self._states)

    async def close(self) -> None:
        async with self._lock:
            tasks = list(self._close_tasks.values())
            self._close_tasks.clear()
            self._states.clear()
            self._dispatch_waiters.clear()
        for task in tasks:
            if not task.done():
                task.cancel()

    async def _close_after_window(
        self,
        *,
        conversation_key: str,
        seq: int,
        window_seconds: float,
        queue_expire_seconds: float,
        merge_builder: MergeBuilder,
        min_messages: int = 1,
        average_reply_latency: float = 5.0,
    ) -> None:
        waiters_to_resolve: list[tuple[asyncio.Future[Any], WindowDispatchResult]] = []
        try:
            if window_seconds > 0:
                await asyncio.sleep(window_seconds)
            async with self._lock:
                state = self._states.get(conversation_key)
                if state is None or state.active_buffer is None:
                    return
                active = state.active_buffer
                if int(active.seq or 0) != int(seq or 0):
                    return
                pending_count = len(active.messages)
                trigger_threshold = max(1, min_messages)

                if pending_count < trigger_threshold:
                    idle_seconds = time.time() - state.last_activity_at
                    equivalent = pending_count + idle_seconds / average_reply_latency
                    if equivalent < trigger_threshold:
                        state.active_buffer = None
                        state.active_buffer = BufferedWindow(
                            conversation_key=conversation_key,
                            seq=state.next_seq,
                            chat_mode=active.chat_mode,
                            opened_at=time.time(),
                            messages=list(active.messages),
                            window_reason="buffer_kept_idle",
                            latest_event=active.latest_event,
                            min_messages=min_messages,
                        )
                        state.next_seq += 1
                        return

                now = time.time()
                active.closed_at = now
                active.expires_at = now + max(0.0, float(queue_expire_seconds or 0.0))
                active.merged_user_message = str(merge_builder(list(active.messages or [])) or "").strip()
                active.window_reason = "window_closed"
                state.queued_windows.append(active)
                state.active_buffer = None
                dispatch, dropped, pending_waiters = self._dispatch_next_locked(
                    state=state,
                    conversation_key=conversation_key,
                    now=now,
                )
                if dispatch is not None:
                    waiter = self._dispatch_waiters.pop((conversation_key, dispatch.seq), None)
                    if waiter is not None and not waiter.done():
                        waiters_to_resolve.append((waiter, WindowDispatchResult(
                            status="dispatch_window",
                            window=dispatch,
                            reason="dispatched",
                            dropped_count=dropped,
                        )))
                waiters_to_resolve.extend(pending_waiters)
        except asyncio.CancelledError:
            raise
        finally:
            for waiter, result in waiters_to_resolve:
                if not waiter.done():
                    waiter.set_result(result)

    def _dispatch_next_locked(
        self,
        *,
        state: ConversationWindowState,
        conversation_key: str,
        now: float,
    ) -> tuple[Optional[BufferedWindow], int, list[tuple[asyncio.Future[Any], WindowDispatchResult]]]:
        dropped_count = 0
        waiters: list[tuple[asyncio.Future[Any], WindowDispatchResult]] = []

        if state.processing is not None and str(state.processing.chat_mode or "").strip().lower() == "group":
            if self._is_window_superseded(state, conversation_key, now):
                dropped_window = state.processing
                dropped_window.window_reason = "superseded"
                state.processing = None
                waiter = self._dispatch_waiters.pop((conversation_key, dropped_window.seq), None)
                if waiter is not None and not waiter.done():
                    waiters.append((waiter, WindowDispatchResult(
                        status="dropped",
                        window=dropped_window,
                        reason="superseded",
                        dropped_count=dropped_count,
                    )))

        while state.queued_windows:
            next_window = state.queued_windows[0]
            expires_at = float(next_window.expires_at or 0.0)
            if expires_at > 0 and expires_at <= now:
                dropped = state.queued_windows.popleft()
                dropped.window_reason = "dropped_expired"
                waiter = self._dispatch_waiters.pop((conversation_key, dropped.seq), None)
                if waiter is not None and not waiter.done():
                    waiters.append((waiter, WindowDispatchResult(
                        status="dropped",
                        window=dropped,
                        reason="dropped_expired",
                        dropped_count=dropped_count + 1,
                    )))
                dropped_count += 1
                continue
            break
        if state.processing is not None or not state.queued_windows:
            return None, dropped_count, waiters
        dispatch = state.queued_windows.popleft()
        dispatch.window_reason = "dispatched"
        state.processing = dispatch
        return dispatch, dropped_count, waiters

    def _is_window_superseded(self, state: ConversationWindowState, conversation_key: str, now: float) -> bool:
        """群聊专用：检查当前 processing window 是否已被更新的 queued window 替代。"""
        if state.processing is None:
            return False
        processing_seq = int(state.processing.seq or 0)

        for queued_window in state.queued_windows:
            queued_seq = int(queued_window.seq or 0)
            if queued_seq > processing_seq:
                return True

        return False

    def _replace_close_task_locked(self, *, conversation_key: str, task: asyncio.Task[Any]) -> None:
        previous = self._close_tasks.get(conversation_key)
        if previous is not None and not previous.done():
            previous.cancel()
        self._close_tasks[conversation_key] = task
