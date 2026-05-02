from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional

from src.core.runtime import BotRuntime
from src.core.config import Config

logger = logging.getLogger(__name__)


class BotRuntimeSupervisor:
    """Manage bot lifecycle without restarting the embedded WebUI."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._bot: Optional[BotRuntime] = None
        self._bot_task: Optional[asyncio.Task] = None
        self._state = "stopped"
        self._last_error = ""

    def get_state(self) -> Dict[str, str]:
        task = self._bot_task
        if task is not None and task.done() and self._state not in {"stopped", "error"}:
            try:
                task.result()
            except asyncio.CancelledError:
                self._state = "stopped"
            except Exception as exc:  # pragma: no cover - defensive sync read
                self._state = "error"
                self._last_error = str(exc)
            else:
                self._state = "stopped"
        return {"state": self._state, "last_error": self._last_error}

    async def start_bot(self) -> Dict[str, str]:
        async with self._lock:
            return await self._start_locked()

    async def stop_bot(self) -> Dict[str, str]:
        async with self._lock:
            return await self._stop_locked()

    async def restart_bot(self) -> Dict[str, str]:
        async with self._lock:
            logger.debug("收到来自 WebUI 的重启请求")
            self._state = "restarting"
            self._last_error = ""
            await self._stop_locked()
            try:
                result = await self._start_locked()
            except asyncio.CancelledError:
                self._state = "stopped"
                raise
            except Exception as exc:
                self._state = "error"
                self._last_error = str(exc)
                logger.exception("助手重启失败：%s", exc)
                raise
            logger.debug("助手重启完成")
            return result

    async def shutdown(self) -> None:
        async with self._lock:
            await self._stop_locked()
            self._state = "stopped"

    async def _start_locked(self) -> Dict[str, str]:
        task = self._bot_task
        if task is not None and not task.done():
            self._state = "running"
            return {"state": self._state, "message": "助手已经在运行中"}

        self._state = "starting"
        self._last_error = ""
        bot = BotRuntime(manage_signals=False, config_obj=Config())
        task = asyncio.create_task(self._run_bot(bot), name="bot-runtime")
        self._bot = bot
        self._bot_task = task

        try:
            await self._wait_until_started(bot, task)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._state = "error"
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                raise

        self._state = "running"
        return {"state": self._state, "message": "助手服务已启动"}

    async def _stop_locked(self) -> Dict[str, str]:
        task = self._bot_task
        bot = self._bot
        if task is None and bot is None:
            self._state = "stopped"
            return {"state": self._state, "message": "助手已经停止"}

        self._state = "stopping"

        if bot is not None:
            await bot.close()
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("[运行时] 助手停止时出现异常")

        self._bot = None
        self._bot_task = None
        self._state = "stopped"
        return {"state": self._state, "message": "助手已停止"}

    async def _wait_until_started(self, bot: BotRuntime, task: asyncio.Task, *, timeout: float = 20.0) -> None:
        async def _poll() -> None:
            while True:
                if task.done():
                    task.result()
                if getattr(bot, "_initialized", False):
                    return
                await asyncio.sleep(0.1)

        await asyncio.wait_for(_poll(), timeout=timeout)

    async def _run_bot(self, bot: BotRuntime) -> None:
        try:
            await bot.run()
        except asyncio.CancelledError:
            self._state = "stopped"
            raise
        except Exception as exc:
            self._state = "error"
            self._last_error = str(exc)
            logger.exception("助手运行异常退出：%s", exc)
            raise
        else:
            if self._state not in {"stopping", "restarting"}:
                self._state = "stopped"
        finally:
            if self._bot is bot:
                self._bot = None
            if self._bot_task is asyncio.current_task():
                self._bot_task = None
