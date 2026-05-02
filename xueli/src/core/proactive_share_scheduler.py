from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from src.memory.storage.proactive_share_store import ProactiveShareStore

logger = logging.getLogger(__name__)


class ProactiveShareScheduler:
    """Independent scheduler for proactive topic sharing.

    Usage:
        scheduler = ProactiveShareScheduler(store=store, config=config)
        await scheduler.start(host=bot_host)
        ...
        await scheduler.stop()
    """

    def __init__(
        self,
        *,
        store: ProactiveShareStore,
        enabled: bool = False,
        idle_hours: float = 24.0,
        cooldown_hours: float = 6.0,
        max_per_day: int = 3,
        time_range_start: str = "09:00",
        time_range_end: str = "22:00",
        check_interval_seconds: float = 600.0,
    ) -> None:
        self.store = store
        self.enabled = enabled
        self.idle_hours = idle_hours
        self.cooldown_hours = cooldown_hours
        self.max_per_day = max_per_day
        self.time_range_start = time_range_start
        self.time_range_end = time_range_end
        self.check_interval = check_interval_seconds
        self._task: Optional[asyncio.Task] = None
        self._host: Any = None
        self._last_user_interaction_at: float = 0.0

    def record_interaction(self) -> None:
        self._last_user_interaction_at = datetime.now(timezone.utc).timestamp()

    async def start(self, host: Any) -> None:
        if not self.enabled:
            return
        self._host = host
        self._task = asyncio.create_task(self._run_loop(), name="proactive-share-scheduler")
        logger.debug("主动分享调度器已启动")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.debug("主动分享调度器已停止")

    async def _run_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.check_interval)
                await self._check_and_share()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("[主动分享] 主动分享检查异常")

    async def _check_and_share(self) -> None:
        if not self.enabled or self._host is None:
            return

        now = datetime.now(timezone.utc).timestamp()
        idle_seconds = self.idle_hours * 3600
        if now - self._last_user_interaction_at < idle_seconds:
            return

        if self.store.is_global_cooldown_active():
            return

        if self.store.count_sent_today() >= self.max_per_day:
            return

        shares = self.store.pending_shares(
            max_count=1,
            cooldown_hours=self.cooldown_hours,
            time_range_start=self.time_range_start,
            time_range_end=self.time_range_end,
        )
        if not shares:
            return

        for share in shares:
            await self._send_share(share)

    async def _send_share(self, share: dict) -> None:
        try:
            content = str(share.get("content", "")).strip()
            if not content:
                return
            share_id = str(share.get("id", ""))
            reply_pipeline = getattr(self._host, "reply_pipeline", None)
            send_func = getattr(self._host, "send_proactive_share", None)
            if send_func and callable(send_func):
                await send_func(content=content, source=str(share.get("source", "insight")))
            elif reply_pipeline:
                logger.debug("[主动分享] 主动分享")
            self.store.mark_sent(share_id)
            self.store.set_global_cooldown(self.cooldown_hours)
            self.record_interaction()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("[主动分享] 发送主动分享失败")
