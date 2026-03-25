"""
QQ 机器人主类
整合连接管理、事件分发和消息处理
"""
import asyncio
import logging
import signal
import sys
from typing import Optional, Dict, Any

from src.core.config import config
from src.core.connection import NapCatConnection
from src.core.dispatcher import EventDispatcher, EventContext
from src.handlers.message_handler import MessageHandler
from src.core.models import MessageEvent, MessageType

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
        logger.info(f"启动机器人: {config.BOT_NAME}")

        # 验证 API 配置
        if not config.OPENAI_API_KEY:
            logger.warning("未设置 OPENAI_API_KEY，AI 功能可能无法正常使用")
        if not config.OPENAI_API_BASE:
            logger.error("未设置 OPENAI_API_BASE，AI 功能将无法使用")
            raise ValueError("必须设置 OPENAI_API_BASE 才能使用 AI 功能")

        logger.info(f"AI 服务: {config.OPENAI_API_BASE}")
        logger.info(f"默认模型: {config.OPENAI_MODEL}")

        # 初始化记忆模块（如果配置启用）
        self.memory_manager = None
        memory_enabled = getattr(config, 'MEMORY_ENABLED', None)
        logger.info(f"记忆模块: {'启用' if memory_enabled else '未启用'}")

        if memory_enabled:
            try:
                from src.memory import MemoryManager, MemoryManagerConfig, RetrievalConfig, ExtractionConfig
                memory_extraction_client_config = config.get_memory_extraction_client_config()
                extraction_model = memory_extraction_client_config.get("model")

                if getattr(config, "MEMORY_EXTRACTION_MODEL", None):
                    logger.info(f"记忆提取模型: {extraction_model}")
                else:
                    logger.info(f"记忆提取模型未单独配置，回退主模型: {extraction_model}")

                # 创建异步 LLM 回调函数用于记忆提取
                async def llm_callback(system_prompt: str, messages: list):
                    from src.services.ai_client import AIClient
                    client = AIClient(**memory_extraction_client_config)

                    try:
                        # 构建完整的消息列表
                        full_messages = [
                            client.build_text_message("system", system_prompt),
                        ]
                        for m in messages:
                            full_messages.append(client.build_text_message(m["role"], m["content"]))

                        # 检查是否配置了专用的记忆提取模型
                        extraction_model = getattr(config, 'MEMORY_EXTRACTION_MODEL', None)

                        # 异步调用；只有配置了专用提取模型时才覆盖默认模型
                        request_kwargs = {
                            "messages": full_messages,
                            "temperature": 0.3,
                        }
                        if extraction_model:
                            request_kwargs["model"] = extraction_model

                        result = await client.chat_completion(**request_kwargs)
                        return result.content if hasattr(result, 'content') else str(result)
                    finally:
                        # 确保关闭 HTTP 会话
                        await client.close()

                # 配置记忆管理器
                mm_config = MemoryManagerConfig(
                    storage_base_path=getattr(config, 'MEMORY_STORAGE_PATH', 'memories'),
                    retrieval_config=RetrievalConfig(
                        bm25_top_k=getattr(config, 'MEMORY_BM25_TOP_K', 100),
                        rerank_enabled=getattr(config, 'MEMORY_RERANK_ENABLED', False),
                        rerank_top_k=getattr(config, 'MEMORY_RERANK_TOP_K', 20)
                    ),
                    extraction_config=ExtractionConfig(
                        extract_every_n_turns=getattr(config, 'MEMORY_EXTRACT_EVERY_N_TURNS', 3)
                    ),
                    ordinary_decay_enabled=getattr(config, 'MEMORY_ORDINARY_DECAY_ENABLED', True),
                    ordinary_half_life_days=getattr(config, 'MEMORY_ORDINARY_HALF_LIFE_DAYS', 30.0),
                    ordinary_forget_threshold=getattr(config, 'MEMORY_ORDINARY_FORGET_THRESHOLD', 0.5),
                    conversation_save_interval=getattr(config, 'MEMORY_CONVERSATION_SAVE_INTERVAL', 10),
                    auto_extract_memory=getattr(config, 'MEMORY_AUTO_EXTRACT', True),
                    auto_build_index=True
                )

                self.memory_manager = MemoryManager(
                    llm_callback=llm_callback,
                    config=mm_config
                )

                await self.memory_manager.initialize()
                logger.info("记忆模块初始化完成")

            except Exception as e:
                logger.error(f"记忆模块初始化失败: {e}", exc_info=True)
                self.memory_manager = None
        else:
            if memory_enabled is None:
                logger.warning("记忆模块配置缺失: MEMORY_ENABLED")
            elif memory_enabled is False:
                logger.info("记忆模块已禁用")

        # 初始化消息处理器
        self.message_handler = MessageHandler(memory_manager=self.memory_manager)

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

        logger.info(f"监听 NapCat 连接: ws://{host}:{port}")

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
                    logger.info(f"收到{msg_type}消息: 用户={event.user_id}")

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
            logger.error(f"消息处理失败: {e}", exc_info=True)
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
            logger.info(f"回复已发送: {len(parts)} 条")

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
        logger.debug(f"发送私聊: 用户={user_id}")

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
        logger.debug(f"发送群消息: 群={group_id}")

    async def _on_websocket_message(self, data: Dict[str, Any]):
        """WebSocket 消息回调"""
        try:
            await self.dispatcher.dispatch(data)
        except Exception as e:
            logger.error(f"事件分发失败: {e}", exc_info=True)
            self.status["errors"] += 1

    async def _on_connect(self):
        """连接成功回调"""
        self.status["connected"] = True
        self.status["ready"] = True
        logger.info("NapCat 已连接")

    async def _on_disconnect(self):
        """断开连接回调"""
        self.status["connected"] = False
        self.status["ready"] = False
        logger.warning("NapCat 连接已断开")

    async def run(self):
        """运行机器人"""
        await self.initialize()

        # 设置信号处理
        def signal_handler(signum, frame):
            logger.info(f"收到退出信号: {signum}")
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
            logger.info("运行任务已取消")
        finally:
            # 关闭连接
            await self.connection.disconnect()
            connection_task.cancel()
            try:
                await connection_task
            except asyncio.CancelledError:
                pass

            logger.info("机器人已关闭")

    def get_status(self) -> Dict[str, Any]:
        """获取机器人状态"""
        stats = self.dispatcher.get_stats()
        return {
            **self.status,
            **stats,
            "active_conversations": len(self.conversations) if hasattr(self, 'conversations') else 0
        }
