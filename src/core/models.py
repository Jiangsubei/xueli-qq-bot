"""
数据模型定义
"""
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Literal
from enum import Enum
import time


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
    def image(cls, file: str, url: Optional[str] = None) -> "MessageSegment":
        """图片消息段"""
        data = {"file": file}
        if url:
            data["url"] = url
        return cls(type="image", data=data)

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "data": self.data}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MessageSegment":
        return cls(type=data.get("type", ""), data=data.get("data", {}))

    def extract_text(self) -> str:
        """提取消息段中的文本"""
        if self.type == "text":
            return self.data.get("text", "")
        elif self.type == "at":
            return f"[@{self.data.get('qq', '')}]"
        return ""

    def is_image(self) -> bool:
        """检查是否为图片消息段"""
        return self.type == "image"

    def get_image_file_id(self) -> Optional[str]:
        """获取图片文件 ID（用于下载）"""
        if self.type == "image":
            # NapCat 可能使用 file 或 file_id 字段
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
        """检查消息是否 @ 了指定 QQ"""
        for seg in self.message:
            if seg.type == "at" and seg.data.get("qq") == str(qq):
                return True
        return False

    def get_at_qqs(self) -> List[int]:
        """获取消息中所有 @ 的 QQ 号"""
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

    def add_message(self, role: str, content: str):
        """添加消息到对话"""
        self.messages.append({"role": role, "content": content})
        self.last_update = time.time()

    def get_messages(self, max_length: int = 10) -> List[Dict[str, str]]:
        """获取最近的对话历史"""
        return self.messages[-max_length:]

    def clear(self):
        """清空对话"""
        self.messages = []
        self.last_update = time.time()

    def is_expired(self, timeout: int = 3600) -> bool:
        """检查对话是否过期（默认1小时）"""
        return time.time() - self.last_update > timeout


class MessagePlanAction(Enum):
    """消息处理计划动作"""

    REPLY = "reply"
    WAIT = "wait"
    IGNORE = "ignore"


@dataclass
class MessageHandlingPlan:
    """消息处理计划"""

    action: str
    reason: str
    source: str = "rule"
    raw_decision: Optional[Dict[str, Any]] = None
    reply_context: Optional[Dict[str, Any]] = None

    @property
    def should_reply(self) -> bool:
        return self.action == MessagePlanAction.REPLY.value
