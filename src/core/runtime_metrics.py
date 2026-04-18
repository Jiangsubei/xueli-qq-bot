from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from threading import Lock
from typing import Any, Dict, Optional


class RuntimeMetrics:
    """Lightweight in-memory runtime metrics facade."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._started_at = datetime.now()
        self._last_error_at: Optional[str] = None
        self._state: Dict[str, Any] = {
            "ready": False,
            "connected": False,
            "messages_received": 0,
            "messages_replied": 0,
            "reply_parts_sent": 0,
            "message_errors": 0,
            "command_hits": 0,
            "planner_reply": 0,
            "planner_wait": 0,
            "planner_ignore": 0,
            "group_repeat_echo": 0,
            "vision_requests": 0,
            "vision_images_processed": 0,
            "vision_failures": 0,
            "vision_reused_from_plan": 0,
            "emoji_detected": 0,
            "emoji_classified": 0,
            "emoji_classification_failures": 0,
            "emoji_reply_decisions": 0,
            "emoji_reply_sent": 0,
            "emoji_reply_skipped": 0,
            "emoji_reply_no_candidate": 0,
            "emoji_total": 0,
            "emoji_pending_classification": 0,
            "emoji_disabled": 0,
            "emoji_active_classifiers": 0,
            "memory_reads": 0,
            "memory_shared_reads": 0,
            "memory_scene_rule_hits": 0,
            "memory_access_denied": 0,
            "memory_writes": 0,
            "memory_migrations": 0,
            "memory_compactions": 0,
            "active_message_tasks": 0,
            "active_session_workers": 0,
            "active_model_workers": 0,
            "pending_model_jobs": 0,
            "active_conversations": 0,
            "background_tasks": 0,
        }
        self._command_hits_by_name: Dict[str, int] = defaultdict(int)

    def set_state(self, **kwargs: Any) -> None:
        with self._lock:
            self._state.update(kwargs)

    def set_ready(self, ready: bool) -> None:
        self.set_state(ready=bool(ready))

    def set_connected(self, connected: bool) -> None:
        self.set_state(connected=bool(connected))

    def record_error(self, *, message_error: bool = False) -> None:
        with self._lock:
            self._last_error_at = datetime.now().isoformat()
            if message_error:
                self._state["message_errors"] += 1

    def inc_messages_received(self, count: int = 1) -> None:
        with self._lock:
            self._state["messages_received"] += max(0, int(count))

    def inc_messages_replied(self, parts: int = 1) -> None:
        with self._lock:
            self._state["messages_replied"] += 1
            self._state["reply_parts_sent"] += max(0, int(parts))

    def inc_command(self, name: str) -> None:
        normalized = str(name or "unknown").strip().lower() or "unknown"
        with self._lock:
            self._state["command_hits"] += 1
            self._command_hits_by_name[normalized] += 1

    def record_planner_action(self, action: str, *, source: str = "") -> None:
        normalized_action = str(action or "").strip().lower()
        with self._lock:
            if normalized_action == "reply":
                self._state["planner_reply"] += 1
            elif normalized_action == "wait":
                self._state["planner_wait"] += 1
            elif normalized_action == "ignore":
                self._state["planner_ignore"] += 1

    def record_group_repeat_echo(self, count: int = 1) -> None:
        with self._lock:
            self._state["group_repeat_echo"] += max(0, int(count))

    def record_vision_request(
        self,
        *,
        image_count: int = 0,
        failure_count: int = 0,
        reused_from_plan: bool = False,
    ) -> None:
        with self._lock:
            if reused_from_plan:
                self._state["vision_reused_from_plan"] += 1
                return
            self._state["vision_requests"] += 1
            self._state["vision_images_processed"] += max(0, int(image_count))
            self._state["vision_failures"] += max(0, int(failure_count))

    def record_emoji_detection(self, count: int = 1) -> None:
        with self._lock:
            self._state["emoji_detected"] += max(0, int(count))

    def record_emoji_classification(self, count: int = 1) -> None:
        with self._lock:
            self._state["emoji_classified"] += max(0, int(count))

    def record_emoji_classification_failure(self, count: int = 1) -> None:
        with self._lock:
            self._state["emoji_classification_failures"] += max(0, int(count))

    def record_emoji_reply_decision(self, count: int = 1) -> None:
        with self._lock:
            self._state["emoji_reply_decisions"] += max(0, int(count))

    def record_emoji_reply_sent(self, count: int = 1) -> None:
        with self._lock:
            self._state["emoji_reply_sent"] += max(0, int(count))

    def record_emoji_reply_skipped(self, count: int = 1) -> None:
        with self._lock:
            self._state["emoji_reply_skipped"] += max(0, int(count))

    def record_emoji_reply_no_candidate(self, count: int = 1) -> None:
        with self._lock:
            self._state["emoji_reply_no_candidate"] += max(0, int(count))

    def inc_memory_read(self, *, shared: bool = False) -> None:
        with self._lock:
            self._state["memory_reads"] += 1
            if shared:
                self._state["memory_shared_reads"] += 1

    def inc_memory_scene_rule_hits(self, count: int = 1) -> None:
        with self._lock:
            self._state["memory_scene_rule_hits"] += max(0, int(count))

    def inc_memory_access_denied(self, count: int = 1) -> None:
        with self._lock:
            self._state["memory_access_denied"] += max(0, int(count))

    def inc_memory_write(self, count: int = 1) -> None:
        with self._lock:
            self._state["memory_writes"] += max(0, int(count))

    def inc_memory_migration(self, count: int = 1) -> None:
        with self._lock:
            self._state["memory_migrations"] += max(0, int(count))

    def inc_memory_compaction(self, count: int = 1) -> None:
        with self._lock:
            self._state["memory_compactions"] += max(0, int(count))

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            uptime_seconds = max(0.0, (datetime.now() - self._started_at).total_seconds())
            return {
                **self._state,
                "uptime_seconds": round(uptime_seconds, 2),
                "last_error_at": self._last_error_at,
                "command_hits_by_name": dict(self._command_hits_by_name),
            }
