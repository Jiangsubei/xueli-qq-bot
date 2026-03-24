"""
QQ 机器人主类
整合连接管理、事件分发和消息处理
"""
import asyncio
import logging
import signal
import sys
from typing import Optional, Dict, Any

from config import config
from connection import NapCatConnection
from dispatcher import EventDispatcher, EventContext
from message_handler import MessageHandler
from models import MessageEvent, MessageType

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


class QQBot:
    """QQ 机器人"""

    def __init__(self):
        self.connection: Optional[NapCatConnection] = None
        self.dispatcher = EventDispatcher()
        self.message_handler: Optional[MessageHandler] = None
        self._running = False
        self._shutdown_event = asyncio.Event()

        # 机器人状态
        self.status = {
            "connected": False,
            "ready": False,
            "messages_received": 0,
            "messages_sent": 0,
            "errors": 0
        }

    async def initialize(self):
        """初始化机器人"""
        logger.info("=" * 50)
        logger.info(f"初始化 QQ 机器人: {config.BOT_NAME}")
        logger.info("=" * 50)

        # 验证 API 配置
        if not config.OPENAI_API_KEY:
            logger.warning("未设置 OPENAI_API_KEY，AI 功能可能无法正常使用")
        if not config.OPENAI_API_BASE:
            logger.error("未设置 OPENAI_API_BASE，AI 功能将无法使用")
            raise ValueError("必须设置 OPENAI_API_BASE 才能使用 AI 功能")

        logger.info(f"使用 AI 服务: {config.OPENAI_API_BASE}")
        logger.info(f"使用模型: {config.OPENAI_MODEL}")

        # 初始化消息处理器
        self.message_handler = MessageHandler()

        # 设置事件处理器
        self._setup_handlers()

        # 初始化 WebSocket 服务端（等待 NapCat 连接）
        # 从 NAPCAT_WS_URL 解析 host 和 port
        ws_url = config.NAPCAT_WS_URL
        if "://" in ws_url:
            ws_url = ws_url.split("://", 1)[1]
        if ":" in ws_url:
            host, port_str = ws_url.rsplit(":", 1)
            port = int(port_str)
        else:
            host = ws_url
            port = 8095

        logger.info(f"将启动 WebSocket 服务端: ws://{host}:{port}")
        logger.info("请配置 NapCat 连接到此地址")

        self.connection = NapCatConnection(
            host=host,
            port=port,
            on_message=self._on_websocket_message,
            on_connect=self._on_connect,
            on_disconnect=self._on_disconnect
        )

        logger.info("机器人初始化完成")

    def _setup_handlers(self):
        """设置事件处理器"""
        # 注册预处理器
        @self.dispatcher.register_preprocessor
        def log_event(ctx: EventContext):
            """记录事件日志"""
            if ctx.event.post_type == "message":
                event = ctx.event
                if hasattr(event, 'message_type'):
                    msg_type = "私聊" if event.message_type == "private" else "群聊"
                    logger.info(f"收到{msg_type}消息: {event.user_id}")

        # 注册消息处理器
        @self.dispatcher.on_message
        async def handle_message(event: MessageEvent):
            """处理消息"""
            await self._handle_message_event(event)

    async def _handle_message_event(self, event: MessageEvent):
        """处理消息事件"""
        # 更新统计
        self.status["messages_received"] += 1

        # 检查是否应该处理
        if not self.message_handler.should_process(event):
            return

        # 检查频率限制
        target_id = str(event.user_id if event.message_type == MessageType.PRIVATE.value else event.group_id)
        await self.message_handler.check_rate_limit(target_id)

        try:
            # 获取 AI 回复
            response = await self.message_handler.get_ai_response(event)

            if response:
                # 发送回复
                await self._send_response(event, response)

        except Exception as e:
            logger.error(f"处理消息时出错: {e}", exc_info=True)
            self.status["errors"] += 1
            await self._send_response(
                event,
                "❌ 处理消息时出错，请稍后重试"
            )

    async def _send_response(self, event: MessageEvent, message: str):
        """发送回复"""
        try:
            # 分割长消息
            parts = self.message_handler.split_long_message(message)

            # 构建发送参数
            if event.message_type == MessageType.PRIVATE.value:
                # 私聊
                for part in parts:
                    await self._send_private_msg(event.user_id, part)
                    await asyncio.sleep(0.5)  # 避免发送过快
            else:
                # 群聊
                for part in parts:
                    await self._send_group_msg(event.group_id, part, event.user_id)
                    await asyncio.sleep(0.5)

            self.status["messages_sent"] += len(parts)
            logger.info(f"发送回复完成，共 {len(parts)} 条消息")

        except Exception as e:
            logger.error(f"发送回复失败: {e}", exc_info=True)
            self.status["errors"] += 1

    async def _send_private_msg(self, user_id: int, message: str):
        """发送私聊消息"""
        payload = {
            "action": "send_private_msg",
            "params": {
                "user_id": user_id,
                "message": message
            }
        }
        await self.connection.send(payload)
        logger.debug(f"发送私聊消息给 {user_id}")

    async def _send_group_msg(self, group_id: int, message: str, at_user: Optional[int] = None):
        """发送群聊消息"""
        # 构建消息
        msg_content = message
        if at_user:
            # 在消息前添加 @
            msg_content = f"[CQ:at,qq={at_user}] {message}"

        payload = {
            "action": "send_group_msg",
            "params": {
                "group_id": group_id,
                "message": msg_content
            }
        }
        await self.connection.send(payload)
        logger.debug(f"发送群消息到 {group_id}")

    async def _on_websocket_message(self, data: Dict[str, Any]):
        """WebSocket 消息回调"""
        try:
            await self.dispatcher.dispatch(data)
        except Exception as e:
            logger.error(f"处理事件时出错: {e}", exc_info=True)
            self.status["errors"] += 1

    async def _on_connect(self):
        """连接成功回调"""
        self.status["connected"] = True
        self.status["ready"] = True
        logger.info("=" * 50)
        logger.info("✅ 机器人已连接到 NapCat")
        logger.info("=" * 50)

    async def _on_disconnect(self):
        """断开连接回调"""
        self.status["connected"] = False
        self.status["ready"] = False
        logger.warning("⚠️ 与 NapCat 的连接已断开")

    async def run(self):
        """运行机器人"""
        await self.initialize()

        # 设置信号处理
        def signal_handler(signum, frame):
            logger.info(f"收到信号 {signum}，正在关闭...")
            self._shutdown_event.set()

        # Windows 不支持 SIGTERM，需要特殊处理
        try:
            import signal
            signal.signal(signal.SIGINT, signal_handler)
            signal.signal(signal.SIGTERM, signal_handler)
        except (AttributeError, ValueError):
            pass

        # 启动连接
        connection_task = asyncio.create_task(self.connection.run())

        try:
            # 等待关闭信号
            await self._shutdown_event.wait()
        except asyncio.CancelledError:
            logger.info("任务被取消")
        finally:
            # 关闭连接
            await self.connection.disconnect()
            connection_task.cancel()
            try:
                await connection_task
            except asyncio.CancelledError:
                pass

            logger.info("=" * 50)
            logger.info("机器人已关闭")
            logger.info("=" * 50)

    def get_status(self) -> Dict[str, Any]:
        """获取机器人状态"""
        stats = self.dispatcher.get_stats()
        return {
            **self.status,
            **stats,
            "active_conversations": len(self.conversations) if hasattr(self, 'conversations') else 0
        }