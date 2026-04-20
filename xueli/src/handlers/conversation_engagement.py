from __future__ import annotations

import re
from typing import Any, Dict


_CARE_TOKENS = (
    "累",
    "好累",
    "疲惫",
    "困",
    "难受",
    "不舒服",
    "头疼",
    "发烧",
    "生病",
    "失眠",
    "焦虑",
    "紧张",
    "烦",
    "崩溃",
    "难过",
    "委屈",
    "低落",
    "沮丧",
    "压力大",
    "加班",
    "心烦",
)

_CONTINUATION_TOKENS = (
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


def detect_care_cue(text: str) -> bool:
    normalized = normalize_engagement_text(text)
    if not normalized:
        return False
    return any(token in normalized for token in _CARE_TOKENS)


def detect_topic_continuation_cue(text: str) -> bool:
    normalized = normalize_engagement_text(text)
    if not normalized:
        return False
    if any(token in normalized for token in _CONTINUATION_TOKENS):
        return True
    return len(normalized) <= 6 and normalized in _LIGHT_RESPONSE_TOKENS


def build_companionship_signals(
    text: str,
    *,
    current_user_id: Any = "",
    previous_speaker_role: str = "",
    previous_user_id: Any = "",
    recent_gap_bucket: str = "unknown",
    recent_history_count: int = 0,
) -> Dict[str, Any]:
    normalized = normalize_engagement_text(text)
    care_cue = detect_care_cue(text)
    continuation_cue = detect_topic_continuation_cue(text)
    previous_role = str(previous_speaker_role or "").strip().lower()
    current_user = str(current_user_id or "").strip()
    previous_user = str(previous_user_id or "").strip()
    assistant_replied_recently = previous_role == "assistant" and recent_gap_bucket in {"immediate", "very_recent", "recent"}
    same_user_continuation = previous_role == "user" and bool(current_user) and current_user == previous_user and continuation_cue
    follow_up_after_assistant = assistant_replied_recently and (continuation_cue or len(normalized) <= 8)
    return {
        "care_cue_detected": care_cue,
        "continuation_cue_detected": continuation_cue,
        "assistant_replied_recently": assistant_replied_recently,
        "follow_up_after_assistant": follow_up_after_assistant,
        "same_user_continuation": same_user_continuation,
        "recent_history_count": max(0, int(recent_history_count or 0)),
        "latest_message_length": len(normalized),
    }
