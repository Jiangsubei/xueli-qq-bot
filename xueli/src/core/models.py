"""
数据模型定义
"""
from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Any, Dict, List, Literal, Optional


class MessageType(Enum):
    """消息类型"""
    PRIVATE = "private"
    GROUP = "group"


class PostType(Enum):
    """上报类型"""
    MESSAGE = "message"
    NOTICE = "notice"
    REQUEST = "request"
    META_EVENT = "meta_event"


class Role(Enum):
    """对话角色"""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass
class MessageSegment:
    """消息段"""
    type: str
    data: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def text(cls, text: str) -> "MessageSegment":
        """纯文本消息段"""
        return cls(type="text", data={"text": text})

    @classmethod
    def at(cls, qq: int) -> "MessageSegment":
        """@某人消息段"""
        return cls(type="at", data={"qq": str(qq)})

    @classmethod
    def reply(cls, message_id: int | str) -> "MessageSegment":
        return cls(type="reply", data={"id": str(message_id)})

    @classmethod
    def image(cls, file: str, url: Optional[str] = None) -> "MessageSegment":
        """图片消息段"""
        data = {"file": file}
        if url:
            data["url"] = url
        return cls(type="image", data=data)

    @classmethod
    def face(cls, face_id: int | str) -> "MessageSegment":
        return cls(type="face", data={"id": str(face_id)})

    @classmethod
    def mface(
        cls,
        *,
        emoji_id: str,
        emoji_package_id: str = "",
        key: str = "",
        summary: str = "",
    ) -> "MessageSegment":
        data = {"emoji_id": str(emoji_id)}
        if emoji_package_id:
            data["emoji_package_id"] = str(emoji_package_id)
        if key:
            data["key"] = str(key)
        if summary:
            data["summary"] = str(summary)
        return cls(type="mface", data=data)

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "data": self.data}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MessageSegment":
        return cls(type=data.get("type", ""), data=data.get("data", {}))

    def extract_text(self) -> str:
        """提取消息段中的纯文本。AT 提及段不产出可见文本（由 protocol adapter 负责去除）。"""
        if self.type == "text":
            return self.data.get("text", "")
        return ""

    def is_image(self) -> bool:
        """检查是否为图片消息段"""
        return self.type == "image"

    def is_face(self) -> bool:
        return self.type == "face" and bool(str(self.data.get("id", "")).strip())

    def is_mface(self) -> bool:
        if self.type == "mface":
            return bool(str(self.data.get("emoji_id", "")).strip())
        if self.type != "image":
            return False
        return bool(
            str(self.data.get("emoji_id", "")).strip()
            or str(self.data.get("emoji_package_id", "")).strip()
            or str(self.data.get("summary", "")).strip()
            or str(self.data.get("key", "")).strip()
        )

    def is_native_sticker(self) -> bool:
        return self.is_face() or self.is_mface()

    def get_native_sticker_kind(self) -> str:
        if self.is_face():
            return "face"
        if self.is_mface():
            return "mface"
        return ""

    def get_native_sticker_ref(self) -> Dict[str, str]:
        kind = self.get_native_sticker_kind()
        if not kind:
            return {}
        if kind == "face":
            return {"kind": "face", "id": str(self.data.get("id", "")).strip()}
        return {
            "kind": "mface",
            "emoji_id": str(self.data.get("emoji_id", "")).strip(),
            "emoji_package_id": str(self.data.get("emoji_package_id", "")).strip(),
            "key": str(self.data.get("key", "")).strip(),
            "summary": str(self.data.get("summary", "")).strip(),
        }

    def get_image_file_id(self) -> Optional[str]:
        """获取图片文件 ID（用于下载）"""
        if self.type == "image":
            # 不同 adapter 可能使用 file 或 file_id 字段
            return self.data.get("file") or self.data.get("file_id")
        return None

    def get_image_url(self) -> Optional[str]:
        """获取图片 URL（如果存在）"""
        if self.type == "image":
            return self.data.get("url")
        return None

    def get_image_filename(self) -> Optional[str]:
        """获取图片文件名"""
        if self.type == "image":
            return self.data.get("file_name") or self.data.get("file_id")
        return None


@dataclass
class OneBotEvent:
    """OneBot 事件基类"""
    post_type: str
    time: int = field(default_factory=lambda: int(time.time()))
    self_id: int = 0
    raw_data: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OneBotEvent":
        post_type = data.get("post_type", "")

        if post_type == "message":
            return MessageEvent.from_dict(data)
        elif post_type == "meta_event":
            return MetaEvent.from_dict(data)
        else:
            return cls(
                post_type=post_type,
                time=data.get("time", int(time.time())),
                self_id=data.get("self_id", 0),
                raw_data=data
            )


@dataclass
class MessageEvent(OneBotEvent):
    """消息事件"""
    message_type: str = ""
    sub_type: str = ""
    message_id: int = 0
    user_id: int = 0
    message: List[MessageSegment] = field(default_factory=list)
    raw_message: str = ""
    font: int = 0
    # 群聊特有
    group_id: Optional[int] = None
    anonymous: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MessageEvent":
        message_data = data.get("message", [])
        if isinstance(message_data, str):
            # CQ码格式，需要解析
            message = cls._parse_cq_code(message_data)
        else:
            # 消息段数组格式
            message = [MessageSegment.from_dict(seg) for seg in message_data]

        return cls(
            post_type="message",
            time=data.get("time", int(time.time())),
            self_id=data.get("self_id", 0),
            raw_data=data,
            message_type=data.get("message_type", ""),
            sub_type=data.get("sub_type", ""),
            message_id=data.get("message_id", 0),
            user_id=data.get("user_id", 0),
            message=message,
            raw_message=data.get("raw_message", ""),
            font=data.get("font", 0),
            group_id=data.get("group_id"),
            anonymous=data.get("anonymous")
        )

    @staticmethod
    def _parse_cq_code(cq_code: str) -> List[MessageSegment]:
        """解析 CQ 码格式的消息"""
        import re
        segments = []
        # 匹配 [CQ:xxx,...] 格式的 CQ 码
        pattern = r'\[CQ:(\w+)(,[^\]]*)?\]'

        last_end = 0
        for match in re.finditer(pattern, cq_code):
            # 添加 CQ 码前的文本
            if match.start() > last_end:
                text = cq_code[last_end:match.start()]
                if text:
                    segments.append(MessageSegment.text(text))

            # 解析 CQ 码
            cq_type = match.group(1)
            params_str = match.group(2) or ""
            params = {}
            for param in params_str.split(","):
                if "=" in param:
                    k, v = param.split("=", 1)
                    params[k.strip()] = v.strip()

            segments.append(MessageSegment(type=cq_type, data=params))
            last_end = match.end()

        # 添加最后剩余的文本
        if last_end < len(cq_code):
            text = cq_code[last_end:]
            if text:
                segments.append(MessageSegment.text(text))

        return segments

    def extract_text(self) -> str:
        """提取消息中的纯文本内容"""
        return "".join(seg.extract_text() for seg in self.message)

    def is_at(self, qq: int) -> bool:
        """检查消息是否包含指定 at 目标（基于 OneBot 段格式）"""
        for seg in self.message:
            if seg.type == "at" and seg.data.get("qq") == str(qq):
                return True
        return False

    def get_at_qqs(self) -> List[int]:
        """获取消息中所有 at 目标 ID（基于 OneBot 段格式）"""
        qq_list = []
        for seg in self.message:
            if seg.type == "at":
                try:
                    qq_list.append(int(seg.data.get("qq", 0)))
                except ValueError:
                    pass
        return qq_list

    def get_image_segments(self) -> List[MessageSegment]:
        """获取消息中所有的图片段"""
        return [seg for seg in self.message if seg.is_image()]

    def has_image(self) -> bool:
        """检查消息是否包含图片"""
        return any(seg.is_image() for seg in self.message)

    def get_native_sticker_segments(self) -> List[MessageSegment]:
        return [seg for seg in self.message if seg.is_native_sticker()]

    def get_sender_info(self) -> Dict[str, Any]:
        sender = self.raw_data.get("sender", {}) if isinstance(self.raw_data, dict) else {}
        return sender if isinstance(sender, dict) else {}

    def get_sender_nickname(self) -> str:
        sender = self.get_sender_info()
        for key in ("nickname", "card", "remark"):
            value = str(sender.get(key, "") or "").strip()
            if value:
                return value
        return ""

    def get_sender_display_name(self) -> str:
        sender = self.get_sender_info()
        preferred_keys = ("card", "nickname", "remark") if self.message_type == MessageType.GROUP.value else ("nickname", "card", "remark")
        for key in preferred_keys:
            value = str(sender.get(key, "") or "").strip()
            if value:
                return value
        return ""

    def get_image_file_ids(self) -> List[str]:
        """获取所有图片的文件 ID 列表"""
        file_ids = []
        for seg in self.message:
            if seg.is_image():
                file_id = seg.get_image_file_id()
                if file_id:
                    file_ids.append(file_id)
        return file_ids


@dataclass
class MetaEvent(OneBotEvent):
    """元事件（心跳、生命周期等）"""
    meta_event_type: str = ""
    sub_type: str = ""
    interval: Optional[int] = None
    status: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MetaEvent":
        return cls(
            post_type="meta_event",
            time=data.get("time", int(time.time())),
            self_id=data.get("self_id", 0),
            raw_data=data,
            meta_event_type=data.get("meta_event_type", ""),
            sub_type=data.get("sub_type", ""),
            interval=data.get("interval"),
            status=data.get("status")
        )


@dataclass
class Conversation:
    """对话历史记录"""
    messages: List[Dict[str, str]] = field(default_factory=list)
    last_update: float = field(default_factory=time.time)
    restored_previous_session_time: float = 0.0
    restored_last_message_time: float = 0.0
    restored_session_id: str = ""
    restored_session_pending: bool = False

    def add_message(
        self,
        role: str,
        content: str,
        *,
        timestamp: Optional[float] = None,
        image_description: str = "",
        message_id: str = "",
        restored: bool = False,
    ):
        """添加消息到对话"""
        event_time = float(timestamp or time.time())
        msg = {"role": role, "content": content, "timestamp": event_time}
        if image_description:
            msg["image_description"] = image_description
        if message_id:
            msg["message_id"] = message_id
        self.messages.append(msg)
        self.last_update = event_time
        if restored:
            self.restored_last_message_time = max(float(self.restored_last_message_time or 0.0), event_time)
        elif self.restored_session_pending:
            self.restored_session_pending = False

    def get_messages(self, max_length: int = 10) -> List[Dict[str, str]]:
        """获取最近的对话历史"""
        return self.messages[-max_length:]

    def clear(self):
        """清空对话"""
        self.messages = []
        self.last_update = time.time()
        self.restored_previous_session_time = 0.0
        self.restored_last_message_time = 0.0
        self.restored_session_id = ""
        self.restored_session_pending = False

    def is_expired(self, timeout: int = 3600) -> bool:
        """检查对话是否过期（默认1小时）"""
        return time.time() - self.last_update > timeout


class MessagePlanAction(Enum):
    """消息处理计划动作"""

    REPLY = "reply"
    WAIT = "wait"
    IGNORE = "ignore"


@dataclass
class TemporalContext:
    """Time-gap signals used by planning and prompt compilation."""

    current_event_time: float = 0.0
    previous_message_time: float = 0.0
    conversation_last_time: float = 0.0
    previous_session_time: float = 0.0
    recent_gap_seconds: Optional[float] = None
    conversation_gap_seconds: Optional[float] = None
    session_gap_seconds: Optional[float] = None
    history_span_seconds: Optional[float] = None
    recent_gap_bucket: str = "unknown"
    conversation_gap_bucket: str = "unknown"
    session_gap_bucket: str = "unknown"
    continuity_hint: str = "unknown"
    summary_text: str = ""


@dataclass
class ConversationContextItem:
    """Structured context item shared across timeline, memory, and reference layers."""

    kind: str
    text: str
    role: str = ""
    speaker_label: str = ""
    timestamp: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    count_in_context: bool = True


@dataclass
class PromptSectionPolicy:
    """Planner-controlled switches for prompt section compilation."""

    include_recent_history: bool = True
    include_person_facts: bool = True
    include_session_restore: bool = True
    include_precise_recall: bool = True
    include_dynamic_memory: bool = True
    include_vision_context: bool = True
    include_reply_scope: bool = True
    include_style_guide: bool = True


PromptLayerPolicy = PromptSectionPolicy


@dataclass
class FinalStyleGuide:
    """Structured guidance for the final visible reply style."""

    verbosity_guidance: str = ""
    warmth_guidance: str = ""
    initiative_guidance: str = ""
    tone_guidance: str = ""
    expression_guidance: str = ""
    opening_style: str = ""
    sentence_shape: str = ""
    followup_shape: str = ""
    allowed_colloquialism: str = ""
    anti_patterns: List[str] = field(default_factory=list)


@dataclass
class PlanningWindowResult:
    """Stabilized user input emitted by the planning window layer."""

    merged_user_message: str = ""
    window_messages: List[Dict[str, Any]] = field(default_factory=list)
    planning_signals: Dict[str, Any] = field(default_factory=dict)
    window_reason: str = ""
    bypassed: bool = False


class TimingDecisionAction(str, Enum):
    CONTINUE = "continue"
    WAIT = "wait"
    NO_REPLY = "no_reply"


@dataclass
class TimingDecision:
    """Result produced by the timing gate layer."""

    decision: str = TimingDecisionAction.CONTINUE.value
    reason: str = ""
    source: str = "rule"
    raw_decision: Optional[Dict[str, Any]] = None

    @property
    def should_continue(self) -> bool:
        return self.decision == TimingDecisionAction.CONTINUE.value


@dataclass
class PromptPlan:
    """Structured prompt policy produced after action planning."""

    reply_goal: str = "continue"
    continuity_mode: str = "direct_continue"
    timeline_detail: str = "summary"
    context_profile: str = "standard"
    memory_profile: str = "relevant"
    tone_profile: str = "balanced"
    initiative: str = "gentle_follow"
    expression_profile: str = "plain"
    policy: PromptSectionPolicy = field(default_factory=PromptSectionPolicy)
    notes: str = ""


@dataclass
class MemoryDisputeDecision:
    """Normalized background judgement for a reflected memory conflict."""

    level: str = "ignore"
    confidence: float = 0.0
    action: str = ""
    conflict_type: str = "none"
    summary: str = ""
    reason: str = ""
    targets: List[Dict[str, Any]] = field(default_factory=list)
    evidence: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class FactEvidenceRecord:
    """Structured persisted evidence for a memory dispute."""

    record_id: str
    user_id: str
    source_memory_id: str = ""
    source_memory_type: str = ""
    decision_level: str = "ignore"
    confidence: float = 0.0
    action: str = ""
    conflict_type: str = "none"
    summary: str = ""
    reason: str = ""
    targets: List[Dict[str, Any]] = field(default_factory=list)
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SoftUncertaintySignal:
    """Cautious tone signal derived from a high-confidence memory dispute."""

    signal_id: str
    user_id: str
    summary: str = ""
    confidence: float = 0.0
    conflict_type: str = "none"
    action: str = ""
    active: bool = True
    source_memory_id: str = ""
    created_at: str = ""
    expires_at: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CharacterCardSnapshot:
    """Layered lightweight character adjustments consumed by reply style policy."""

    user_id: str = ""
    core_traits: List[str] = field(default_factory=list)
    tone_preferences: List[str] = field(default_factory=list)
    behavior_habits: List[str] = field(default_factory=list)
    explicit_feedback_count: int = 0
    stable_signal_count: int = 0
    updated_at: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MessageHandlingPlan:
    """消息处理计划"""

    action: str
    reason: str
    source: str = "rule"
    raw_decision: Optional[Dict[str, Any]] = None
    reply_context: Optional[Dict[str, Any]] = None
    prompt_plan: Optional[PromptPlan] = None
    reply_reference: str = ""

    @property
    def should_reply(self) -> bool:
        return self.action == MessagePlanAction.REPLY.value
