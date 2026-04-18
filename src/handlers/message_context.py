from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.core.models import Conversation


@dataclass
class MessageContext:
    """Unified per-message context shared across planning and reply generation."""

    trace_id: str = ""
    execution_key: str = ""
    conversation_key: str = ""
    user_message: str = ""
    current_sender_label: str = ""
    is_first_turn: bool = False
    window_messages: List[Dict[str, Any]] = field(default_factory=list)
    recent_history_text: str = ""
    base64_images: List[str] = field(default_factory=list)
    vision_analysis: Dict[str, Any] = field(default_factory=dict)
    persistent_memory_context: str = ""
    dynamic_memory_context: str = ""
    related_history_messages: List[Dict[str, Any]] = field(default_factory=list)
    reply_context: Dict[str, Any] = field(default_factory=dict)
    direct_reply_text: str = ""
    conversation: Optional[Conversation] = None
