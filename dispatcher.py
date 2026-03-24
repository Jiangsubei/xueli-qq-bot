"""
事件分发模块
根据事件类型分发到不同的处理器
"""
import logging
from typing import Callable, Dict, Any, Optional
from dataclasses import dataclass

from models import OneBotEvent, MessageEvent, PostType, MessageType

logger = logging.getLogger(__name__)


@dataclass
class EventContext:
    """事件上下文"""
    event: OneBotEvent
    raw_data: Dict[str, Any]
    should_handle: bool = True
    skip_reason: Optional[str] = None


class EventDispatcher:
    """事件分发器"""

    def __init__(self, bot_id: int = 0):
        self.bot_id = bot_id
        self.handlers: Dict[str, list] = {
            "message": [],
            "notice": [],
            "request": [],
            "meta_event": []
        }
        self.preprocessors: list = []
        self.postprocessors: list = []

        # 统计信息
        self.stats = {
            "total_events": 0,
            "handled_events": 0,
            "skipped_events": 0
        }

    def register_preprocessor(self, func: Callable[[EventContext], None]):
        """注册预处理器"""
        self.preprocessors.append(func)
        return func

    def register_postprocessor(self, func: Callable[[EventContext, Any], None]):
        """注册后处理器"""
        self.postprocessors.append(func)
        return func

    def on_message(self, func: Callable[[MessageEvent], Any]):
        """注册消息处理器"""
        self.handlers["message"].append(func)
        return func

    def on_private_message(self, func: Callable[[MessageEvent], Any]):
        """注册私聊消息处理器"""
        def wrapper(event: MessageEvent):
            if event.message_type == MessageType.PRIVATE.value:
                return func(event)
        self.handlers["message"].append(wrapper)
        return wrapper

    def on_group_message(self, func: Callable[[MessageEvent], Any]):
        """注册群聊消息处理器"""
        def wrapper(event: MessageEvent):
            if event.message_type == MessageType.GROUP.value:
                return func(event)
        self.handlers["message"].append(wrapper)
        return wrapper

    def on_notice(self, func: Callable[[Dict[str, Any]], Any]):
        """注册通知处理器"""
        self.handlers["notice"].append(func)
        return func

    def on_request(self, func: Callable[[Dict[str, Any]], Any]):
        """注册请求处理器"""
        self.handlers["request"].append(func)
        return func

    def on_meta_event(self, func: Callable[[Dict[str, Any]], Any]):
        """注册元事件处理器"""
        self.handlers["meta_event"].append(func)
        return func

    async def dispatch(self, raw_data: Dict[str, Any]):
        """分发事件"""
        self.stats["total_events"] += 1

        # 解析事件
        event = OneBotEvent.from_dict(raw_data)

        # 创建上下文
        ctx = EventContext(event=event, raw_data=raw_data)

        # 预处理器
        for preprocessor in self.preprocessors:
            try:
                if asyncio.iscoroutinefunction(preprocessor):
                    await preprocessor(ctx)
                else:
                    preprocessor(ctx)
            except Exception as e:
                logger.error(f"预处理器执行出错: {e}")

        # 检查是否跳过
        if not ctx.should_handle:
            self.stats["skipped_events"] += 1
            logger.debug(f"事件被跳过: {ctx.skip_reason}")
            return

        # 根据事件类型分发
        post_type = raw_data.get("post_type", "")
        handlers = self.handlers.get(post_type, [])

        results = []
        for handler in handlers:
            try:
                # 根据事件类型传递不同的参数
                if isinstance(event, MessageEvent):
                    if asyncio.iscoroutinefunction(handler):
                        result = await handler(event)
                    else:
                        result = handler(event)
                else:
                    if asyncio.iscoroutinefunction(handler):
                        result = await handler(raw_data)
                    else:
                        result = handler(raw_data)
                results.append(result)
            except Exception as e:
                logger.error(f"处理器执行出错: {e}", exc_info=True)

        # 后处理器
        for postprocessor in self.postprocessors:
            try:
                if asyncio.iscoroutinefunction(postprocessor):
                    await postprocessor(ctx, results)
                else:
                    postprocessor(ctx, results)
            except Exception as e:
                logger.error(f"后处理器执行出错: {e}")

        self.stats["handled_events"] += 1

    def get_stats(self) -> Dict[str, int]:
        """获取统计信息"""
        return self.stats.copy()


# 导入 asyncio
import asyncio