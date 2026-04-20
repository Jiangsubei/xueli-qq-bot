from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional


@dataclass
class BufferedWindow:
    """A sealed or active time-sliced batch of inbound conversation inputs."""

    conversation_key: str = ""
    seq: int = 0
    chat_mode: str = "private"
    opened_at: float = 0.0
    closed_at: float = 0.0
    expires_at: float = 0.0
    messages: List[Dict[str, Any]] = field(default_factory=list)
    merged_user_message: str = ""
    planning_signals: Dict[str, Any] = field(default_factory=dict)
    window_reason: str = ""
    latest_event: Optional[Any] = None


@dataclass
class ConversationWindowState:
    """Scheduler-owned per-conversation window state."""

    active_buffer: Optional[BufferedWindow] = None
    queued_windows: Deque[BufferedWindow] = field(default_factory=deque)
    processing: Optional[BufferedWindow] = None
    next_seq: int = 1
    last_activity_at: float = 0.0


@dataclass
class WindowDispatchResult:
    """Scheduler/service result for message submission or queue advancement."""

    status: str = "accepted_only"
    window: Optional[BufferedWindow] = None
    reason: str = ""
    dropped_count: int = 0
