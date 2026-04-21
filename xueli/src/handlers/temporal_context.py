from __future__ import annotations

import time
from typing import Iterable, Optional

from src.core.models import TemporalContext


def normalize_event_time(raw_value: object) -> float:
    try:
        value = float(raw_value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if value <= 0:
        return 0.0
    if value > 1_000_000_000_000:
        return value / 1000.0
    return value


def _gap_thresholds(chat_mode: str) -> list[tuple[float, str]]:
    normalized_mode = str(chat_mode or "private").strip().lower()
    if normalized_mode == "group":
        return [
            (30.0, "immediate"),
            (180.0, "very_recent"),
            (900.0, "recent"),
            (3600.0, "same_day_resume"),
            (21600.0, "late_same_day"),
            (86400.0, "short_resume"),
            (259200.0, "long_resume"),
        ]
    return [
        (60.0, "immediate"),
        (600.0, "very_recent"),
        (3600.0, "recent"),
        (21600.0, "same_day_resume"),
        (86400.0, "late_same_day"),
        (259200.0, "short_resume"),
        (604800.0, "long_resume"),
    ]


def bucket_gap(seconds: Optional[float], *, chat_mode: str = "private") -> str:
    if seconds is None or seconds < 0:
        return "unknown"
    for threshold, label in _gap_thresholds(chat_mode):
        if seconds < threshold:
            return label
    return "stale"


def continuity_hint(recent_bucket: str, conversation_bucket: str = "unknown") -> str:
    candidate = recent_bucket if recent_bucket != "unknown" else conversation_bucket
    if candidate in {"immediate", "very_recent"}:
        return "strong_continuation"
    if candidate == "recent":
        return "soft_continuation"
    if candidate in {"same_day_resume", "late_same_day"}:
        return "resume_after_break"
    if candidate in {"short_resume", "long_resume", "stale"}:
        return "old_topic_resume"
    return "unknown"


def _bucket_observation(bucket: str) -> str:
    mapping = {
        "immediate": "当前消息和最近一条上下文消息几乎连在一起",
        "very_recent": "当前消息和最近一条上下文消息间隔很短",
        "recent": "当前消息和最近一条上下文消息间隔不久",
        "same_day_resume": "当前消息和最近一条上下文消息之间已经有一段同日间隔",
        "late_same_day": "当前消息和最近一条上下文消息之间已经有较明显的同日间隔",
        "short_resume": "当前消息和最近一条上下文消息之间已隔了 1 到 3 天左右",
        "long_resume": "当前消息和最近一条上下文消息之间已隔了数天",
        "stale": "当前消息和最近一条上下文消息之间已经间隔较久",
    }
    return mapping.get(bucket, "当前缺少足够的最近消息时间信息")


def summarize_temporal_context(ctx: TemporalContext) -> str:
    lines = []
    if ctx.session_gap_bucket != "unknown":
        lines.append(f"最近一条历史消息时间分层是 {ctx.recent_gap_bucket}")
        lines.append(f"上一轮已关闭会话的时间分层是 {ctx.session_gap_bucket}")
    else:
        lines.append(_bucket_observation(ctx.recent_gap_bucket))
    if ctx.history_span_seconds is not None:
        if ctx.history_span_seconds < 300:
            lines.append("最近窗口里的消息时间分布比较集中")
        elif ctx.history_span_seconds < 7200:
            lines.append("最近窗口里的消息覆盖了同一段较短时间范围")
        else:
            lines.append("最近窗口里的消息跨越了较长时间范围")
    return "；".join(lines)


def build_temporal_context(
    *,
    current_event_time: object,
    chat_mode: str = "private",
    previous_message_time: object = 0.0,
    conversation_last_time: object = 0.0,
    previous_session_time: object = 0.0,
    history_event_times: Optional[Iterable[object]] = None,
) -> TemporalContext:
    current_time = normalize_event_time(current_event_time) or time.time()
    previous_time = normalize_event_time(previous_message_time)
    conversation_time = normalize_event_time(conversation_last_time)
    session_time = normalize_event_time(previous_session_time)

    recent_gap_seconds = current_time - previous_time if previous_time > 0 else None
    conversation_gap_seconds = current_time - conversation_time if conversation_time > 0 else None
    session_gap_seconds = current_time - session_time if session_time > 0 else None

    normalized_history = [normalize_event_time(item) for item in list(history_event_times or []) if normalize_event_time(item) > 0]
    history_span_seconds = None
    if len(normalized_history) >= 2:
        history_span_seconds = max(normalized_history) - min(normalized_history)

    result = TemporalContext(
        current_event_time=current_time,
        previous_message_time=previous_time,
        conversation_last_time=conversation_time,
        previous_session_time=session_time,
        recent_gap_seconds=recent_gap_seconds,
        conversation_gap_seconds=conversation_gap_seconds,
        session_gap_seconds=session_gap_seconds,
        history_span_seconds=history_span_seconds,
        recent_gap_bucket=bucket_gap(recent_gap_seconds, chat_mode=chat_mode),
        conversation_gap_bucket=bucket_gap(conversation_gap_seconds, chat_mode=chat_mode),
        session_gap_bucket=bucket_gap(session_gap_seconds, chat_mode=chat_mode),
    )
    result.continuity_hint = continuity_hint(result.recent_gap_bucket, result.conversation_gap_bucket)
    result.summary_text = summarize_temporal_context(result)
    return result
