from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.core.models import Conversation, ConversationContextItem, FinalStyleGuide, PromptPlan, TemporalContext


@dataclass
class MessageContext:
    """Unified per-message context shared across planning and reply generation."""

    trace_id: str = ""
    execution_key: str = ""
    conversation_key: str = ""
    user_message: str = ""
    current_sender_label: str = ""
    is_first_turn: bool = False
    current_event_time: float = 0.0
    previous_message_time: float = 0.0
    conversation_last_time: float = 0.0
    previous_session_time: float = 0.0
    temporal_context: TemporalContext = field(default_factory=TemporalContext)
    context_items: List[ConversationContextItem] = field(default_factory=list)
    window_messages: List[Dict[str, Any]] = field(default_factory=list)
    recent_history_text: str = ""
    rendered_recent_history: str = ""
    rendered_timeline_summary: str = ""
    rendered_memory_sections: Dict[str, str] = field(default_factory=dict)
    base64_images: List[str] = field(default_factory=list)
    vision_analysis: Dict[str, Any] = field(default_factory=dict)
    person_fact_context: str = ""
    persistent_memory_context: str = ""
    session_restore_context: str = ""
    precise_recall_context: str = ""
    dynamic_memory_context: str = ""
    related_history_messages: List[Dict[str, Any]] = field(default_factory=list)
    reply_context: Dict[str, Any] = field(default_factory=dict)
    direct_reply_text: str = ""
    planning_signals: Dict[str, Any] = field(default_factory=dict)
    prompt_plan: Optional[PromptPlan] = None
    final_style_guide: FinalStyleGuide = field(default_factory=FinalStyleGuide)
    conversation: Optional[Conversation] = None
