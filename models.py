"""
数据模型定义
"""
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
import time
import re


@dataclass
class MessageSegment:
    """消息段 - 表示消息中的一个片段（文本、@、图片等）"""
    type: str
    data: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def text(cls, text: str) -> "MessageSegment":
        """创建纯文本消息段"""
        return cls(type="text", data={"text": text})

    @classmethod
    def at(cls, qq: int) -> "MessageSegment":
        """创建@某人消息段"""
        return cls(type="at", data={"qq": str(qq)})

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {"type": self.type, "data": self.data}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MessageSegment":
        """从字典创建"""
        return cls(type=data.get("type", ""), data=data.get("data", {}))


@dataclass
class MessageEvent:
    """消息事件 - 表示接收到的一条消息"""
    message_type: str  # "private" 或 "group"
    user_id: int
    message: List[MessageSegment] = field(default_factory=list)
    raw_message: str = ""
    group_id: Optional[int] = None
    self_id: int = 0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MessageEvent":
        """从字典创建消息事件"""
        message_data = data.get("message", [])
        if isinstance(message_data, str):
            # CQ码格式，需要解析
            message = cls._parse_cq_code(message_data)
        else:
            # 消息段数组格式
            message = [MessageSegment.from_dict(seg) for seg in message_data]

        return cls(
            message_type=data.get("message_type", ""),
            user_id=data.get("user_id", 0),
            message=message,
            raw_message=data.get("raw_message", ""),
            group_id=data.get("group_id"),
            self_id=data.get("self_id", 0)
        )

    @staticmethod
    def _parse_cq_code(cq_code: str) -> List[MessageSegment]:
        """解析 CQ 码格式的消息"""
        segments = []
        pattern = r'\[CQ:(\w+)(,[^\]]*)?\]'
        last_end = 0

        for match in re.finditer(pattern, cq_code):
            if match.start() > last_end:
                text = cq_code[last_end:match.start()]
                if text:
                    segments.append(MessageSegment.text(text))

            cq_type = match.group(1)
            params_str = match.group(2) or ""
            params = {}
            for param in params_str.split(","):
                if "=" in param:
                    k, v = param.split("=", 1)
                    params[k.strip()] = v.strip()

            segments.append(MessageSegment(type=cq_type, data=params))
            last_end = match.end()

        if last_end < len(cq_code):
            text = cq_code[last_end:]
            if text:
                segments.append(MessageSegment.text(text))

        return segments

    def extract_text(self) -> str:
        """提取消息中的纯文本内容"""
        texts = []
        for seg in self.message:
            if seg.type == "text":
                texts.append(seg.data.get("text", ""))
            elif seg.type == "at":
                texts.append(f"[@{seg.data.get('qq', '')}]")
        return "".join(texts)

    def is_at(self, qq: int) -> bool:
        """检查消息是否 @ 了指定 QQ"""
        for seg in self.message:
            if seg.type == "at" and seg.data.get("qq") == str(qq):
                return True
        return False


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