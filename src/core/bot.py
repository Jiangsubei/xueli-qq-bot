"""QQ bot runtime coordinator."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Any, Dict, List, Optional, Set

from src.core.bootstrap import BotBootstrapper
from src.core.config import Config
from src.core.dispatcher import EventContext, EventDispatcher
from src.core.lifecycle import cancel_task, cancel_tasks, close_resource
from src.core.models import MessageEvent, MessageSegment, MessageType
from src.core.runtime_metrics import RuntimeMetrics
from src.core.webui_runtime_registry import register_runtime, unregister_runtime
from src.core.webui_snapshot import WebUISnapshotPublisher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


class QQBot:
    """Main bot runtime facade."""

    def __init__(self, *, manage_signals: bool = True, config_obj: Config | None = None):
        self.dispatcher = EventDispatcher()
        self._manage_signals = manage_signals
        self.config = config_obj or Config()
        self.runtime_metrics = RuntimeMetrics()
        self.bootstrapper = BotBootstrapper(self.config)
        self.webui_snapshot = WebUISnapshotPublisher(
            app_config=self.config.app,
            status_provider=self.get_status,
        )

        self.connection = None
        self.message_handler = None
        self.memory_manager = None
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None

        self._running = False
        self._initialized = False
        self._closed = False
        self._shutdown_event = asyncio.Event()
        self._message_tasks: Set[asyncio.Task] = set()
        self._connection_task: Optional[asyncio.Task] = None
        self._snapshot_task: Optional[asyncio.Task] = None
        self._handlers_registered = False

        self.status = {
            "connected": False,
            "ready": False,
            "messages_received": 0,
            "messages_sent": 0,
            "errors": 0,
        }

    async def initialize(self):
        """Initialize managed runtime dependencies."""
        if self._initialized:
            return

        self._closed = False
        self._shutdown_event.clear()
        self._setup_handlers()
        try:
            runtime = await self.bootstrapper.build(
                on_message=self._on_websocket_message,
                on_connect=self._on_connect,
                on_disconnect=self._on_disconnect,
                runtime_metrics=self.runtime_metrics,
                status_provider=self.get_status,
            )
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception:
            logger.exception("初始化机器人运行时失败")
            await self.close()
            raise

        self.connection = runtime.connection
        self.message_handler = runtime.message_handler
        self.memory_manager = runtime.memory_manager
        self._event_loop = asyncio.get_running_loop()
        register_runtime(bot=self, memory_manager=self.memory_manager, loop=self._event_loop)

        if self.message_handler and hasattr(self.message_handler, "set_status_provider"):
            self.message_handler.set_status_provider(self.get_status)

        self._initialized = True
        self._sync_runtime_counters()
        self._sync_status_cache()
        logger.info("机器人运行时初始化完成")

    def _setup_handlers(self):
        """Register dispatcher handlers once."""
        if self._handlers_registered:
            return

        @self.dispatcher.register_preprocessor
        def log_event(ctx: EventContext):
            if ctx.event.post_type != "message":
                return
            event = ctx.event
            if not hasattr(event, "message_type"):
                return
            msg_type = "private" if event.message_type == MessageType.PRIVATE.value else "group"
            logger.info("收到%s消息：用户=%s", "私聊" if msg_type == "private" else "群聊", event.user_id)

        @self.dispatcher.on_message
        async def handle_message(event: MessageEvent):
            self._start_message_task(event)

        self._handlers_registered = True

    def _start_message_task(self, event: MessageEvent) -> None:
        task = asyncio.create_task(self._handle_message_event(event))
        self._message_tasks.add(task)
        self._sync_runtime_counters()
        task.add_done_callback(self._on_message_task_done)

    def _on_message_task_done(self, task: asyncio.Task) -> None:
        self._message_tasks.discard(task)
        self._sync_runtime_counters()
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error("消息任务执行失败：%s", exc, exc_info=True)
            self.runtime_metrics.record_error(message_error=True)
            self._sync_status_cache()

    async def _cancel_message_tasks(self) -> None:
        tasks = list(self._message_tasks)
        if not tasks:
            self._sync_runtime_counters()
            return
        await cancel_tasks(tasks, label="message_tasks")
        self._message_tasks.clear()
        self._sync_runtime_counters()

    async def _handle_message_event(self, event: MessageEvent):
        self.runtime_metrics.inc_messages_received()
        self._sync_status_cache()
        plan = None

        try:
            plan = await self.message_handler.plan_message(event)
            planner_log_context = self._build_planner_log_context(plan)
            logger.info(
                "消息处理计划：action=%s，source=%s，batch_mode=%s，batch_size=%s，is_latest=%s，merged_into_latest=%s，reason=%s，user=%s，group=%s",
                plan.action,
                plan.source,
                planner_log_context["batch_mode"],
                planner_log_context["batch_size"],
                planner_log_context["is_latest"],
                planner_log_context["merged_into_latest"],
                plan.reason,
                event.user_id,
                event.group_id,
            )

            if not plan.should_reply:
                return

            target_id = str(
                event.user_id if event.message_type == MessageType.PRIVATE.value else event.group_id
            )
            await self.message_handler.check_rate_limit(target_id)
            reply_result = await self.message_handler.get_ai_response(event, plan=plan)
            if not reply_result or not reply_result.text:
                return

            sent = await self._send_response(event, reply_result.text, plan=plan)
            if sent:
                await self.message_handler.record_group_reply_sent(event, reply_result.text)
                await self._send_emoji_follow_up_if_needed(event, reply_result, plan)
        except Exception as exc:
            logger.error("处理消息事件失败：%s", exc, exc_info=True)
            self.runtime_metrics.record_error(message_error=True)
            self._sync_status_cache()
            if plan is not None and plan.should_reply:
                await self._send_response(event, "抱歉，这次处理消息时出了点问题。")

    async def _send_response(self, event: MessageEvent, message: str, plan: Any = None) -> bool:
        try:
            parts = self.message_handler.split_long_message(message)
            if event.message_type == MessageType.PRIVATE.value:
                quote_reply_enabled = self._private_quote_reply_enabled()
                for index, part in enumerate(parts):
                    use_quote_reply = (
                        quote_reply_enabled
                        and index == 0
                        and int(event.message_id or 0) > 0
                    )
                    if use_quote_reply:
                        await self._send_private_segments(
                            event.user_id,
                            [MessageSegment.reply(event.message_id), MessageSegment.text(part)],
                        )
                    else:
                        await self._send_private_msg(event.user_id, part)
                    await asyncio.sleep(0.5)
            else:
                at_user = self.message_handler.resolve_group_at_user(event, plan)
                for index, part in enumerate(parts):
                    part_at_user = at_user if index == 0 else None
                    await self._send_group_msg(event.group_id, part, part_at_user)
                    await asyncio.sleep(0.5)

            self.runtime_metrics.inc_messages_replied(len(parts))
            self._sync_status_cache()
            logger.info(
                "消息发送完成：target=%s，message_type=%s，parts=%s",
                event.group_id if event.message_type == MessageType.GROUP.value else event.user_id,
                event.message_type,
                len(parts),
            )
            return True
        except Exception as exc:
            logger.error("发送回复失败：%s", exc, exc_info=True)
            self.runtime_metrics.record_error(message_error=True)
            self._sync_status_cache()
            return False

    async def _send_emoji_follow_up_if_needed(self, event: MessageEvent, reply_result: Any, plan: Any) -> None:
        if event.message_type != MessageType.GROUP.value:
            return
        selection = await self.message_handler.plan_emoji_follow_up(event, reply_result, plan=plan)
        if not selection or not getattr(selection, "emoji", None):
            return

        image_path = await self.message_handler.get_emoji_follow_up_image_path(selection)
        if not image_path:
            return

        try:
            await self._send_group_segments(event.group_id, [MessageSegment.image(image_path)])
            await self.message_handler.mark_emoji_follow_up_sent(event, selection)
            logger.info(
                "群聊表情跟进已发送：group=%s，emoji_id=%s",
                event.group_id,
                getattr(selection.emoji, "emoji_id", ""),
            )
        except Exception as exc:
            logger.error("发送表情跟进失败：%s", exc, exc_info=True)
            self.runtime_metrics.record_error(message_error=True)
            self._sync_status_cache()

    async def _send_private_msg(self, user_id: int, message: str):
        payload = {
            "action": "send_private_msg",
            "params": {"user_id": user_id, "message": message},
        }
        await self.connection.send(payload)
        logger.debug("已发送私聊消息：user=%s", user_id)

    async def _send_private_segments(self, user_id: int, segments: List[MessageSegment]) -> None:
        payload = {
            "action": "send_private_msg",
            "params": {
                "user_id": user_id,
                "message": [segment.to_dict() for segment in segments],
            },
        }
        await self.connection.send(payload)
        logger.debug("已发送私聊分段消息：user=%s，segments=%s", user_id, len(segments))

    async def _send_group_msg(self, group_id: int, message: str, at_user: Optional[int] = None):
        if at_user:
            segments = [MessageSegment.at(at_user)]
            if message:
                segments.append(MessageSegment.text(f" {message}"))
            payload = {
                "action": "send_group_msg",
                "params": {
                    "group_id": group_id,
                    "message": [segment.to_dict() for segment in segments],
                },
            }
            await self.connection.send(payload)
            logger.debug("已发送群聊@消息：group=%s，at_user=%s", group_id, at_user)
            return

        payload = {
            "action": "send_group_msg",
            "params": {"group_id": group_id, "message": message},
        }
        await self.connection.send(payload)
        logger.debug("已发送群聊消息：group=%s", group_id)

    async def _send_group_segments(self, group_id: int, segments: List[MessageSegment]) -> None:
        payload = {
            "action": "send_group_msg",
            "params": {
                "group_id": group_id,
                "message": [segment.to_dict() for segment in segments],
            },
        }
        await self.connection.send(payload)
        logger.debug("已发送群聊分段消息：group=%s，segments=%s", group_id, len(segments))

    def _private_quote_reply_enabled(self) -> bool:
        app_config = getattr(self.message_handler, "app_config", None)
        bot_behavior = getattr(app_config, "bot_behavior", None)
        return bool(getattr(bot_behavior, "private_quote_reply_enabled", False))

    async def _on_websocket_message(self, data: Dict[str, Any]):
        try:
            await self.dispatcher.dispatch(data)
        except Exception as exc:
            logger.error("处理 WebSocket 消息失败：%s", exc, exc_info=True)
            self.runtime_metrics.record_error()
            self._sync_status_cache()

    async def _on_connect(self):
        self.runtime_metrics.set_connected(True)
        self.runtime_metrics.set_ready(True)
        self._sync_status_cache()
        logger.info("NapCat 已连接")

    async def _on_disconnect(self):
        self.runtime_metrics.set_connected(False)
        self.runtime_metrics.set_ready(False)
        self._sync_status_cache()
        logger.warning("NapCat 连接已断开")

    async def _run_webui_snapshot_heartbeat(self) -> None:
        while not self._shutdown_event.is_set():
            try:
                self.webui_snapshot.publish()
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("WebUI 快照心跳异常：%s", exc, exc_info=True)
                await asyncio.sleep(5)

    async def run(self):
        self._event_loop = asyncio.get_running_loop()
        await self.initialize()

        if self._manage_signals:
            def signal_handler(signum, frame):
                del frame
                logger.info("收到系统信号：%s", signum)
                self._shutdown_event.set()

            try:
                signal.signal(signal.SIGINT, signal_handler)
                signal.signal(signal.SIGTERM, signal_handler)
            except (AttributeError, ValueError):
                logger.debug("当前运行环境不支持注册系统信号处理器")

        self._running = True
        self._connection_task = asyncio.create_task(self.connection.run())
        self._snapshot_task = asyncio.create_task(self._run_webui_snapshot_heartbeat())

        try:
            await self._shutdown_event.wait()
        except asyncio.CancelledError:
            logger.info("机器人运行任务被取消")
        finally:
            await self.close()
            logger.info("机器人主循环已退出")

    async def close(self) -> None:
        if self._closed:
            return

        self._running = False
        self._closed = True
        self._shutdown_event.set()

        await self._cancel_message_tasks()

        if self.connection:
            try:
                await self.connection.disconnect()
            finally:
                await cancel_task(self._connection_task, label="connection_task")
                self._connection_task = None
        else:
            await cancel_task(self._connection_task, label="connection_task")
            self._connection_task = None

        await cancel_task(self._snapshot_task, label="webui_snapshot_task")
        self._snapshot_task = None

        await close_resource(self.message_handler, label="message_handler")
        await close_resource(self.memory_manager, label="memory_manager")

        self.runtime_metrics.set_state(
            connected=False,
            ready=False,
            active_message_tasks=0,
            active_conversations=0,
            background_tasks=0,
        )
        self._initialized = False
        unregister_runtime(self)
        self._sync_status_cache()
        self.webui_snapshot.publish(closing=True)

    def get_status(self) -> Dict[str, Any]:
        dispatcher_stats = self.dispatcher.get_stats()
        memory_stats = {}
        if self.memory_manager and hasattr(self.memory_manager, "get_stats"):
            memory_stats = self.memory_manager.get_stats()
        active_conversations = 0
        if self.message_handler and hasattr(self.message_handler, "get_active_conversation_count"):
            active_conversations = self.message_handler.get_active_conversation_count()

        self.runtime_metrics.set_state(active_conversations=active_conversations)
        snapshot = self.runtime_metrics.snapshot()
        status = {
            **snapshot,
            **dispatcher_stats,
            **memory_stats,
            "active_conversations": active_conversations,
            "messages_sent": snapshot.get("reply_parts_sent", 0),
            "errors": snapshot.get("message_errors", 0),
        }
        return status

    def _sync_runtime_counters(self) -> None:
        active_conversations = 0
        if self.message_handler and hasattr(self.message_handler, "get_active_conversation_count"):
            active_conversations = self.message_handler.get_active_conversation_count()
        self.runtime_metrics.set_state(
            active_message_tasks=len(self._message_tasks),
            active_conversations=active_conversations,
        )

    def _sync_status_cache(self) -> None:
        self._sync_runtime_counters()
        snapshot = self.runtime_metrics.snapshot()
        self.status.update(
            {
                "connected": snapshot.get("connected", False),
                "ready": snapshot.get("ready", False),
                "messages_received": snapshot.get("messages_received", 0),
                "messages_sent": snapshot.get("reply_parts_sent", 0),
                "errors": snapshot.get("message_errors", 0),
            }
        )
        self.webui_snapshot.publish()

    def _build_planner_log_context(self, plan: Any) -> Dict[str, Any]:
        if not plan or not getattr(plan, "reply_context", None):
            return {
                "batch_mode": "unknown",
                "batch_size": 1,
                "is_latest": True,
                "merged_into_latest": False,
            }

        planner_batch = plan.reply_context.get("planner_batch") or {}
        return {
            "batch_mode": planner_batch.get("mode", "unknown"),
            "batch_size": planner_batch.get("batch_size", 1),
            "is_latest": planner_batch.get("is_latest", True),
            "merged_into_latest": planner_batch.get("merged_into_latest", False),
        }
