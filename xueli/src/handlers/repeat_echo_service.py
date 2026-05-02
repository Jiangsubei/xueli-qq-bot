from __future__ import annotations

import logging
import re
import time
from collections import defaultdict, deque
from typing import Dict, Deque, List, Optional, Tuple

from src.core.config import AppConfig
from src.core.models import MessageEvent, MessageType
from src.core.runtime_metrics import RuntimeMetrics

logger = logging.getLogger(__name__)


class RepeatEchoService:
    """Detects and triggers the group repeat-echo feature."""

    def __init__(self, app_config: AppConfig, runtime_metrics: Optional[RuntimeMetrics] = None):
        self._app_config = app_config
        self._runtime_metrics = runtime_metrics
        self._lock = None  # set later via set_lock; avoids circular __init__
        self._history: Dict[int, Deque[Dict[str, object]]] = defaultdict(deque)
        self._cooldowns: Dict[Tuple[int, str], float] = {}

    def set_lock(self, lock: object) -> None:
        import asyncio
        self._lock = lock  # type: asyncio.Lock

    @staticmethod
    def _normalize_text(text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "").strip())

    def is_candidate(self, event: MessageEvent, text: str, *, is_direct_mention: bool, has_image: bool) -> bool:
        if event.message_type != MessageType.GROUP.value or not event.raw_data.get("group_id"):
            return False
        if not self._app_config.group_reply.repeat_echo_enabled:
            return False
        if is_direct_mention or has_image:
            return False
        normalized = self._normalize_text(text)
        if not normalized or normalized.startswith("/"):
            return False
        return 2 <= len(normalized) <= 20

    async def check_trigger(
        self,
        event: MessageEvent,
        display_text: str,
        *,
        normalize_text_fn=None,
    ) -> Optional[str]:
        normalized = normalize_text_fn(display_text) if normalize_text_fn else self._normalize_text(display_text)
        if not self._app_config.group_reply.repeat_echo_enabled:
            return None

        import asyncio
        lock = self._lock
        if lock is None:
            return None
        async with lock:
            now = time.time()
            group_id = int(event.raw_data.get("group_id") or 0)
            key = normalized.casefold()
            window_seconds = float(self._app_config.group_reply.repeat_echo_window_seconds or 20.0)
            min_count = max(2, int(self._app_config.group_reply.repeat_echo_min_count or 2))
            cooldown_seconds = max(0.0, float(self._app_config.group_reply.repeat_echo_cooldown_seconds or 0.0))

            history = self._history[group_id]
            while history and now - float(history[0]["time"]) > window_seconds:
                history.popleft()

            same_entries = [item for item in history if item.get("key") == key]
            unique_users = {int(item.get("user_id", 0)) for item in same_entries}
            unique_users.add(int(event.user_id))

            history.append({"time": now, "key": key, "user_id": int(event.user_id)})

            cooldown_key: Tuple[int, str] = (group_id, key)
            cooldown_until = float(self._cooldowns.get(cooldown_key, 0.0) or 0.0)
            if cooldown_until > now:
                return None

            if len(unique_users) < min_count:
                return None

            self._cooldowns[cooldown_key] = now + cooldown_seconds
        if self._runtime_metrics:
            self._runtime_metrics.record_group_repeat_echo()
        if not bool(getattr(self._app_config.bot_behavior, "log_full_prompt", False)):
            logger.info("[复读服务] 触发群聊复读")
        return display_text

    def cleanup(self) -> None:
        now = time.time()
        max_window = max(20.0, float(self._app_config.group_reply.repeat_echo_window_seconds or 20.0))
        history_cutoff = max_window * 3
        stale_groups: List[int] = []
        for group_id, history in self._history.items():
            while history and now - float(history[0].get("time", 0.0) or 0.0) > history_cutoff:
                history.popleft()
            if not history:
                stale_groups.append(group_id)
        for group_id in stale_groups:
            self._history.pop(group_id, None)

        stale_cooldowns = [
            key for key, until in self._cooldowns.items() if float(until or 0.0) <= now
        ]
        for key in stale_cooldowns:
            self._cooldowns.pop(key, None)
