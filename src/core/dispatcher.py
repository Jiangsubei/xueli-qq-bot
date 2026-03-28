"""
事件分发模块。

根据事件类型把消息分发给不同处理器。
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from src.core.models import MessageEvent, MessageType, OneBotEvent

logger = logging.getLogger(__name__)


@dataclass
class EventContext:
    """事件上下文。"""

    event: OneBotEvent
    raw_data: Dict[str, Any]
    should_handle: bool = True
    skip_reason: Optional[str] = None


class EventDispatcher:
    """事件分发器。"""

    def __init__(self, bot_id: int = 0):
        self.bot_id = bot_id
        self.handlers: Dict[str, list] = {
            "message": [],
            "notice": [],
            "request": [],
            "meta_event": [],
        }
        self.preprocessors: list = []
        self.postprocessors: list = []
        self.stats = {
            "total_events": 0,
            "handled_events": 0,
            "skipped_events": 0,
        }

    def register_preprocessor(self, func: Callable[[EventContext], None]):
        """注册预处理器。"""
        self.preprocessors.append(func)
        return func

    def register_postprocessor(self, func: Callable[[EventContext, Any], None]):
        """注册后处理器。"""
        self.postprocessors.append(func)
        return func

    def on_message(self, func: Callable[[MessageEvent], Any]):
        """注册消息处理器。"""
        self.handlers["message"].append(func)
        return func

    def on_private_message(self, func: Callable[[MessageEvent], Any]):
        """注册私聊消息处理器。"""

        def wrapper(event: MessageEvent):
            if event.message_type == MessageType.PRIVATE.value:
                return func(event)

        self.handlers["message"].append(wrapper)
        return wrapper

    def on_group_message(self, func: Callable[[MessageEvent], Any]):
        """注册群聊消息处理器。"""

        def wrapper(event: MessageEvent):
            if event.message_type == MessageType.GROUP.value:
                return func(event)

        self.handlers["message"].append(wrapper)
        return wrapper

    def on_notice(self, func: Callable[[Dict[str, Any]], Any]):
        """注册通知处理器。"""
        self.handlers["notice"].append(func)
        return func

    def on_request(self, func: Callable[[Dict[str, Any]], Any]):
        """注册请求处理器。"""
        self.handlers["request"].append(func)
        return func

    def on_meta_event(self, func: Callable[[Dict[str, Any]], Any]):
        """注册元事件处理器。"""
        self.handlers["meta_event"].append(func)
        return func

    async def dispatch(self, raw_data: Dict[str, Any]):
        """分发事件。"""
        self.stats["total_events"] += 1
        event = OneBotEvent.from_dict(raw_data)
        ctx = EventContext(event=event, raw_data=raw_data)

        for preprocessor in self.preprocessors:
            try:
                if asyncio.iscoroutinefunction(preprocessor):
                    await preprocessor(ctx)
                else:
                    preprocessor(ctx)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    "预处理器执行失败: 名称=%s, 错误=%s",
                    getattr(preprocessor, "__name__", preprocessor.__class__.__name__),
                    e,
                )

        if not ctx.should_handle:
            self.stats["skipped_events"] += 1
            logger.debug("事件已跳过: 原因=%s", ctx.skip_reason or "未说明")
            return

        post_type = raw_data.get("post_type", "")
        handlers = self.handlers.get(post_type, [])

        results = []
        for handler in handlers:
            try:
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
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    "处理器执行失败: 类型=%s, 名称=%s, 错误=%s",
                    post_type,
                    getattr(handler, "__name__", handler.__class__.__name__),
                    e,
                    exc_info=True,
                )

        for postprocessor in self.postprocessors:
            try:
                if asyncio.iscoroutinefunction(postprocessor):
                    await postprocessor(ctx, results)
                else:
                    postprocessor(ctx, results)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    "后处理器执行失败: 名称=%s, 错误=%s",
                    getattr(postprocessor, "__name__", postprocessor.__class__.__name__),
                    e,
                )

        self.stats["handled_events"] += 1

    def get_stats(self) -> Dict[str, int]:
        """获取统计信息。"""
        return self.stats.copy()
