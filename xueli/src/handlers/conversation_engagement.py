from __future__ import annotations

import re
from typing import Any, Dict


_LIGHT_RESPONSE_TOKENS = {
    "嗯",
    "嗯嗯",
    "好哦",
    "好耶",
    "原来如此",
    "这样啊",
    "懂了",
    "然后呢",
    "继续",
    "后来呢",
}


def normalize_engagement_text(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "").strip()).lower()


def _message_length_bucket(text: str) -> str:
    length = len(text)
    if length <= 3:
        return "ultra_short"
    if length <= 8:
        return "short"
    if length <= 20:
        return "medium"
    return "long"


def _is_light_response_candidate(text: str) -> bool:
    normalized = normalize_engagement_text(text)
    if not normalized:
        return False
    return len(normalized) <= 6 and normalized in _LIGHT_RESPONSE_TOKENS


def _is_continuation_candidate(text: str) -> bool:
    normalized = normalize_engagement_text(text)
    if not normalized:
        return False
    continuation_tokens = (
        "然后呢",
        "后来呢",
        "后来",
        "继续",
        "接着",
        "结果呢",
        "结果",
        "再然后",
        "再说说",
        "展开讲",
        "那现在",
        "那后来",
        "所以呢",
        "所以",
        "还有呢",
        "细说",
    )
    return any(token in normalized for token in continuation_tokens)


def build_message_observations(
    text: str,
    *,
    current_user_id: Any = "",
    previous_speaker_role: str = "",
    previous_user_id: Any = "",
    recent_gap_bucket: str = "unknown",
    recent_history_count: int = 0,
) -> Dict[str, Any]:
    """
    构建中性消息观察向量。

    只描述可观测事实，不输出语义判断结论。
    所有语义判断由 planner 和 timing gate 基于这些观察自行决策。
    """
    normalized = normalize_engagement_text(text)
    previous_role = str(previous_speaker_role or "").strip().lower()
    current_user = str(current_user_id or "").strip()
    previous_user = str(previous_user_id or "").strip()

    assistant_replied_recently = (
        previous_role == "assistant"
        and recent_gap_bucket in {"immediate", "very_recent", "recent"}
    )

    same_user_continuation = (
        previous_role == "user"
        and bool(current_user)
        and current_user == previous_user
        and _is_continuation_candidate(text)
    )

    follows_assistant_recently = (
        assistant_replied_recently
        and (_is_continuation_candidate(text) or len(normalized) <= 8)
    )

    return {
        "message_length_bucket": _message_length_bucket(normalized),
        "is_short_message": len(normalized) <= 8,
        "is_light_response_candidate": _is_light_response_candidate(text),
        "is_continuation_candidate": _is_continuation_candidate(text),
        "assistant_replied_recently": assistant_replied_recently,
        "follows_assistant_recently": follows_assistant_recently,
        "same_user_continuation": same_user_continuation,
        "recent_history_count": max(0, int(recent_history_count or 0)),
        "latest_message_length": len(normalized),
    }
