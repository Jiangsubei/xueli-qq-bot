from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict

from src.core.config import AppConfig, get_vision_service_status

logger = logging.getLogger(__name__)


def default_snapshot_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "runtime" / "webui_snapshot.json"


class WebUISnapshotPublisher:
    """Persist a lightweight runtime snapshot for the Django WebUI."""

    def __init__(
        self,
        *,
        app_config: AppConfig,
        status_provider: Callable[[], Dict[str, Any]],
        path: Path | None = None,
    ) -> None:
        self.app_config = app_config
        self.status_provider = status_provider
        self.path = path or default_snapshot_path()

    def publish(self, *, closing: bool = False) -> None:
        try:
            payload = self._build_payload(closing=closing)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_suffix(".tmp")
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp_path.replace(self.path)
        except Exception as exc:
            logger.debug("failed to publish webui snapshot: %s", exc, exc_info=True)

    def _build_payload(self, *, closing: bool = False) -> Dict[str, Any]:
        status = dict(self.status_provider() or {})
        assistant = self.app_config.assistant_profile
        emoji_config = self.app_config.emoji
        memory_config = self.app_config.memory
        vision_status = get_vision_service_status(self.app_config)

        ready = bool(status.get("ready", False))
        connected = bool(status.get("connected", False))
        if closing:
            ready = False
            connected = False

        return {
            "snapshot_at": datetime.now(timezone.utc).isoformat(),
            "ready": ready,
            "connected": connected,
            "uptime_seconds": status.get("uptime_seconds", 0),
            "last_error_at": status.get("last_error_at"),
            "assistant": {
                "name": assistant.name,
                "alias": assistant.alias,
            },
            "services": {
                "vision_status": vision_status,
                "memory_enabled": bool(memory_config.enabled),
                "emoji_enabled": bool(emoji_config.enabled),
            },
            "messages": {
                "messages_received": status.get("messages_received", 0),
                "messages_replied": status.get("messages_replied", 0),
                "reply_parts_sent": status.get("reply_parts_sent", 0),
                "message_errors": status.get("message_errors", 0),
            },
            "activity": {
                "active_conversations": status.get("active_conversations", 0),
                "active_message_tasks": status.get("active_message_tasks", 0),
                "background_tasks": status.get("background_tasks", 0),
            },
            "planner": {
                "planner_reply": status.get("planner_reply", 0),
                "planner_wait": status.get("planner_wait", 0),
                "planner_ignore": status.get("planner_ignore", 0),
                "planner_burst_merge": status.get("planner_burst_merge", 0),
            },
            "vision": {
                "vision_requests": status.get("vision_requests", 0),
                "vision_images_processed": status.get("vision_images_processed", 0),
                "vision_failures": status.get("vision_failures", 0),
                "vision_reused_from_plan": status.get("vision_reused_from_plan", 0),
            },
            "emoji": {
                "emoji_total": status.get("emoji_total", 0),
                "emoji_pending_classification": status.get("emoji_pending_classification", 0),
                "emoji_disabled": status.get("emoji_disabled", 0),
                "emoji_active_classifiers": status.get("emoji_active_classifiers", 0),
                "emoji_detected": status.get("emoji_detected", 0),
                "emoji_classified": status.get("emoji_classified", 0),
                "emoji_reply_sent": status.get("emoji_reply_sent", 0),
            },
            "memory": {
                "memory_reads": status.get("memory_reads", 0),
                "memory_writes": status.get("memory_writes", 0),
                "memory_shared_reads": status.get("memory_shared_reads", 0),
                "indices_built": status.get("indices_built", 0),
                "indices_dirty": status.get("indices_dirty", 0),
                "extractor_enabled": status.get("extractor_enabled", False),
                "storage_path": status.get("storage_path", memory_config.storage_path),
            },
        }
