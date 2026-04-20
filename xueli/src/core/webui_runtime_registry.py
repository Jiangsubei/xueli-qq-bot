from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future
from typing import Any, Awaitable, Dict, Optional

_LOCK = threading.RLock()
_STATE: Dict[str, Any] = {
    "bot": None,
    "memory_manager": None,
    "loop": None,
    "supervisor": None,
    "control_loop": None,
}


def register_runtime(*, bot: Any = None, memory_manager: Any = None, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    with _LOCK:
        if bot is not None:
            _STATE["bot"] = bot
        if memory_manager is not None:
            _STATE["memory_manager"] = memory_manager
        if loop is not None:
            _STATE["loop"] = loop


def register_runtime_control(*, supervisor: Any = None, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    with _LOCK:
        if supervisor is not None:
            _STATE["supervisor"] = supervisor
        if loop is not None:
            _STATE["control_loop"] = loop


def unregister_runtime(bot: Any = None, *, clear_control: bool = False) -> None:
    with _LOCK:
        current_bot = _STATE.get("bot")
        if bot is not None and current_bot is not bot:
            return
        _STATE["bot"] = None
        _STATE["memory_manager"] = None
        _STATE["loop"] = None
        if clear_control:
            _STATE["supervisor"] = None
            _STATE["control_loop"] = None


def get_runtime_state() -> Dict[str, Any]:
    with _LOCK:
        return dict(_STATE)


def run_coro_threadsafe(coro: Awaitable[Any], *, timeout: float = 5.0) -> Any:
    state = get_runtime_state()
    loop = state.get("control_loop") or state.get("loop")
    if loop is None or not loop.is_running():
        raise RuntimeError("runtime loop unavailable")
    future: Future[Any] = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)
