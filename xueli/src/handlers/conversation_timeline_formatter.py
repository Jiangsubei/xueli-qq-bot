from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, List

from src.core.models import ConversationContextItem, PromptPlan, TemporalContext
from src.handlers.label_constants import SENDER_LABEL_ASSISTANT, SENDER_LABEL_USER


def _format_clock(timestamp: float) -> str:
    if not timestamp:
        return "--:--:--"
    return datetime.fromtimestamp(float(timestamp)).strftime("%H:%M:%S")


def _speaker_label(item: dict[str, Any]) -> str:
    role = str(item.get("speaker_role") or "user").strip().lower()
    if role == "assistant":
        return str(item.get("speaker_name") or SENDER_LABEL_ASSISTANT).strip() or SENDER_LABEL_ASSISTANT
    speaker = str(item.get("speaker_name") or item.get("user_id") or SENDER_LABEL_USER).strip()
    user_id = str(item.get("user_id") or "").strip()
    if speaker and user_id and speaker != user_id:
        return f"{speaker}({user_id})"
    return speaker or user_id or SENDER_LABEL_USER


class ConversationTimelineFormatter:
    """Render timeline context for prompt compilation and tests."""

    def build_items(self, window_messages: Iterable[dict[str, Any]]) -> List[ConversationContextItem]:
        items: List[ConversationContextItem] = []
        for message in list(window_messages or []):
            text = str(message.get("display_text") or message.get("text") or message.get("raw_text") or "用户发送了空文本").strip() or "用户发送了空文本"
            speaker_label = _speaker_label(message)
            items.append(
                ConversationContextItem(
                    kind="timeline_message",
                    text=text,
                    role=str(message.get("speaker_role") or "user"),
                    speaker_label=speaker_label,
                    timestamp=float(message.get("event_time", 0.0) or 0.0),
                    metadata={"is_latest": bool(message.get("is_latest"))},
                )
            )
        return items

    def render_recent_history(
        self,
        *,
        window_messages: Iterable[dict[str, Any]],
        prompt_plan: PromptPlan | None,
        temporal_context: TemporalContext | None,
        chat_mode: str,
    ) -> str:
        detail = str(getattr(prompt_plan, "timeline_detail", "summary") or "summary").strip().lower()
        items = list(window_messages or [])
        previous_items = [item for item in items if not bool(item.get("is_latest"))]
        if not previous_items:
            return ""
        if detail == "off":
            return ""
        if detail == "summary":
            lines = ["最近上下文摘要："]
            for item in previous_items[-3:]:
                lines.append(f"- {_speaker_label(item)}：{str(item.get('display_text') or item.get('text') or '用户发送了空文本').strip() or '用户发送了空文本'}")
            if temporal_context and str(getattr(temporal_context, "summary_text", "")).strip():
                lines.append(f"- 时间线观察：{temporal_context.summary_text}")
            return "\n".join(lines)

        lines = ["最近对话时间线："]
        for item in previous_items:
            lines.append(
                f"- [{_format_clock(float(item.get('event_time', 0.0) or 0.0))}] {_speaker_label(item)}："
                f"{str(item.get('display_text') or item.get('text') or '用户发送了空文本').strip() or '用户发送了空文本'}"
            )
        if chat_mode == "group" and temporal_context and str(getattr(temporal_context, "summary_text", "")).strip():
            lines.append(f"- 时间线观察：{temporal_context.summary_text}")
        return "\n".join(lines)

    def render_summary(self, temporal_context: TemporalContext | None) -> str:
        if temporal_context is None:
            return ""
        summary = str(getattr(temporal_context, "summary_text", "") or "").strip()
        if not summary:
            return ""
        recent_gap = str(getattr(temporal_context, "recent_gap_bucket", "unknown") or "unknown")
        continuity = str(getattr(temporal_context, "continuity_hint", "unknown") or "unknown")
        return f"{summary} 最近时间分层={recent_gap}，连续性={continuity}。"
