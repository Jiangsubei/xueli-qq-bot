from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


DEFAULT_EMOTION_LABELS: List[str] = [
    "开心",
    "喜欢",
    "惊讶",
    "无语",
    "委屈",
    "生气",
    "伤心",
    "嘲讽",
    "害怕",
    "困惑",
    "焦虑",
    "疲惫",
    "温暖",
    "内疚",
    "感动",
]

DEFAULT_REPLY_TONES: List[str] = [
    "安慰",
    "附和",
    "吐槽",
    "庆祝",
    "调侃",
    "拒绝",
    "提醒",
    "收尾",
]


@dataclass
class EmojiEmotionResult:
    primary_emotion: str = ""
    confidence: float = 0.0
    reason: str = ""
    all_emotions: List[str] = field(default_factory=list)
    reply_tones: List[str] = field(default_factory=list)
    reply_intents: List[str] = field(default_factory=list)
    secondary_emotions: List[str] = field(default_factory=list)
    intensity: float = 0.5

    def to_dict(self) -> Dict[str, Any]:
        return {
            "primary_emotion": self.primary_emotion,
            "confidence": float(self.confidence),
            "reason": self.reason,
            "all_emotions": list(self.all_emotions),
            "reply_tones": list(self.reply_tones),
            "reply_intents": list(self.reply_intents),
            "secondary_emotions": list(self.secondary_emotions),
            "intensity": float(self.intensity),
        }


@dataclass
class EmojiReplyDecision:
    should_send: bool = False
    target_tone: str = ""
    target_emotion: str = ""
    target_intent: str = ""
    reason: str = ""


@dataclass
class EmojiRecord:
    emoji_id: str
    sticker_kind: str = ""
    native_id: str = ""
    emoji_package_id: str = ""
    native_key: str = ""
    native_summary: str = ""
    description: str = ""
    emotion_status: str = "pending"
    primary_emotion: str = ""
    emotion_confidence: float = 0.0
    emotion_reason: str = ""
    emotion_candidates: List[str] = field(default_factory=list)
    reply_tones: List[str] = field(default_factory=list)
    reply_intents: List[str] = field(default_factory=list)
    disabled: bool = False
    review_status: str = "pending"
    usage_count: int = 1
    manual_weight: float = 1.0
    auto_reply_count: int = 0
    last_auto_reply_at: str = ""
    first_seen_at: str = ""
    last_seen_at: str = ""
    message_id: int = 0
    message_type: str = ""
    user_id: int = 0
    group_id: Optional[int] = None
    raw_segment: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    emotion_error: str = ""
    # Legacy fields kept for backward compatibility with existing indexes/UI.
    sha256: str = ""
    image_path: str = ""
    file_ext: str = ""
    sticker_confidence: float = 0.0
    sticker_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EmojiRecord":
        payload = dict(data or {})
        payload["emotion_candidates"] = list(payload.get("emotion_candidates") or [])
        payload["reply_tones"] = list(payload.get("reply_tones") or [])
        payload["reply_intents"] = list(payload.get("reply_intents") or [])
        payload["raw_segment"] = dict(payload.get("raw_segment") or {})
        payload["tags"] = list(payload.get("tags") or [])
        payload["manual_weight"] = float(payload.get("manual_weight", 1.0) or 1.0)
        payload["auto_reply_count"] = int(payload.get("auto_reply_count", 0) or 0)
        payload["last_auto_reply_at"] = str(payload.get("last_auto_reply_at", "") or "")
        allowed = {field_name for field_name in cls.__dataclass_fields__}
        filtered = {key: value for key, value in payload.items() if key in allowed}
        return cls(**filtered)


@dataclass
class EmojiReplySelection:
    decision: EmojiReplyDecision = field(default_factory=EmojiReplyDecision)
    emoji: Optional[EmojiRecord] = None
    skip_reason: str = ""
