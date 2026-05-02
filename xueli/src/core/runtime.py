"""Bot runtime coordinator."""
from __future__ import annotations

import asyncio
import logging
import random
import time
import signal
import sys
from dataclasses import replace
from typing import Any, Dict, List, Optional, Set

from src.core.bootstrap import BotBootstrapper
from src.core.config import Config
from src.core.dispatcher import EventContext, EventDispatcher
from src.core.log_text import preview_json_for_log, preview_text_for_log
from src.core.message_trace import build_trace_id, format_trace_log, get_execution_key
from src.core.model_invocation_router import ModelInvocationRouter
from src.core.pipeline_errors import (
    ImageProcessingError,
    MemoryOperationError,
    ModelParseError,
    ModelRequestError,
    PipelineExecutionError,
    SendError,
    classify_pipeline_error,
)
from src.core.platform_models import InboundEvent, ReplyAction, SessionRef
from src.handlers.message_handler import StaleWindowError
from src.core.reply_send_orchestrator import ReplyPartPlan, ReplySendOrchestrator
from src.core.platform_normalizers import get_attached_inbound_event
from src.core.session_message_pipeline import SessionMessagePipeline
from src.core.lifecycle import cancel_task, cancel_tasks, close_resource
from src.core.models import MessageEvent, MessageSegment, MessageType
from src.core.runtime_metrics import RuntimeMetrics
from src.core.webui_runtime_registry import register_runtime, unregister_runtime
from src.core.webui_snapshot import WebUISnapshotPublisher
from src.core.proactive_share_scheduler import ProactiveShareScheduler
from src.memory.storage.proactive_share_store import ProactiveShareStore

logger = logging.getLogger(__name__)


class BotRuntime:
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

        self.adapter = None
        self.connection = None
        self._adapters_by_name: Dict[str, Any] = {}
        self._adapters_by_platform: Dict[str, Any] = {}
        self.message_handler = None
        self.memory_manager = None
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None

        self._running = False
        self._initialized = False
        self._closed = False
        self._shutdown_event = asyncio.Event()
        self._message_tasks: Set[asyncio.Task] = set()
        self._processed_message_ids: set = set()
        self._dedup_lock: Optional[asyncio.Lock] = None
        self._close_lock: Optional[asyncio.Lock] = None
        self._message_pipeline = SessionMessagePipeline(on_state_change=self._on_pipeline_state_change)
        self._model_router = ModelInvocationRouter(
            base_timeout_seconds=max(1, int(self.config.app.bot_behavior.response_timeout or 60)),
            on_state_change=self._on_model_router_state_change,
        )
        self._reply_send_orchestrator = ReplySendOrchestrator(rng=random.Random())
        self._connection_task: Optional[asyncio.Task] = None
        self._snapshot_task: Optional[asyncio.Task] = None
        self._handlers_registered = False

        self._proactive_share_store: Optional[ProactiveShareStore] = None
        self._proactive_scheduler: Optional[ProactiveShareScheduler] = None

        self.status = {
            "connected": False,
            "ready": False,
            "messages_received": 0,
            "messages_sent": 0,
            "errors": 0,
        }

    def _get_dedup_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_dedup_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._dedup_lock = lock
        return lock

    def _get_close_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_close_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._close_lock = lock
        return lock

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
                model_invocation_router=self._model_router,
            )
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception:
            logger.exception("初始化机器人运行时失败")
            await self.close()
            raise

        self.adapter = runtime.connection
        self.connection = self.adapter
        self.register_runtime_adapter(self.adapter)
        self.message_handler = runtime.message_handler
        self.memory_manager = runtime.memory_manager
        self.dispatcher.configure_inbound_event_attacher(
            getattr(self.adapter, "attach_inbound_event", None),
            platform=self._default_session_platform(),
            adapter_name=self._default_adapter_name(),
        )
        self._event_loop = asyncio.get_running_loop()
        register_runtime(bot=self, memory_manager=self.memory_manager, loop=self._event_loop)

        if self.message_handler and hasattr(self.message_handler, "set_status_provider"):
            self.message_handler.set_status_provider(self.get_status)

        self._setup_proactive_share()

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
            logger.debug("收到%s消息：用户=%s", "私聊" if msg_type == "private" else "群聊", event.user_id)

        @self.dispatcher.on_message
        async def handle_message(event: MessageEvent):
            message_id = getattr(event, "message_id", 0)
            trace_id = build_trace_id(message_id)
            execution_key = get_execution_key(event)
            logger.info(
                "收到消息：%s key=%s",
                format_trace_log(trace_id=trace_id, session_key=execution_key, message_id=message_id),
                execution_key,
            )
            await self._message_pipeline.submit(
                execution_key=execution_key,
                trace_id=trace_id,
                event=event,
                handler=self._handle_pipeline_message,
            )

        self._handlers_registered = True

    def _setup_proactive_share(self) -> None:
        proactive = self.config.app.proactive_share
        if not proactive.enabled:
            return
        data_path = self.config.app.memory.storage_path or "../data/memories"
        self._proactive_share_store = ProactiveShareStore(
            base_path=f"{data_path}/proactive_shares"
        )
        self._proactive_scheduler = ProactiveShareScheduler(
            store=self._proactive_share_store,
            enabled=proactive.enabled,
            idle_hours=proactive.idle_hours,
            cooldown_hours=proactive.cooldown_hours,
            max_per_day=proactive.max_per_day,
            time_range_start=proactive.time_range_start,
            time_range_end=proactive.time_range_end,
        )

    def record_user_interaction(self) -> None:
        scheduler = getattr(self, "_proactive_scheduler", None)
        if scheduler:
            scheduler.record_interaction()

    async def _handle_pipeline_message(self, event: MessageEvent, trace_id: str) -> None:
        await self._handle_message_event(event, trace_id=trace_id)

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
        if self._message_pipeline:
            await self._message_pipeline.close()
        tasks = [task for task in self._message_tasks if not task.done()]
        if tasks:
            await cancel_tasks(tasks, label="message_tasks")
        self._message_tasks.clear()
        self._sync_runtime_counters()

    async def _try_dispatch_next_window(self, plan, *, trace_id: str = ""):
        """Try to dispatch the next buffered window. Returns (event, plan) or (None, None) if done."""
        next_dispatch = await self.message_handler.complete_window_dispatch(plan)
        if next_dispatch.status == "dispatch_window" and next_dispatch.window is not None:
            return await self.message_handler.plan_dispatched_window(next_dispatch.window, trace_id=trace_id)
        return None, None

    async def _release_window_on_error(self, plan, trace_log: str) -> None:
        try:
            await self.message_handler.complete_window_dispatch(plan)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("释放窗口调度失败：%s", trace_log, exc_info=True)

    async def _send_error_fallback(self, current_event, text: str, trace_log: str, trace_id: str) -> None:
        try:
            await self._send_response(current_event, text, trace_id=trace_id)
        except asyncio.CancelledError:
            raise
        except Exception as fallback_exc:
            logger.error("发送兜底回复失败：%s category=%s 错误=%s", trace_log, classify_pipeline_error(fallback_exc), fallback_exc, exc_info=True)

    async def _handle_message_event(self, event: MessageEvent, *, trace_id: str = ""):
        # ── 去重检查 ── 防止重放攻击或平台重试导致重复处理
        msg_id = getattr(event, "message_id", 0)
        async with self._get_dedup_lock():
            if not hasattr(self, "_processed_message_ids"):
                self._processed_message_ids = set()
            if msg_id in self._processed_message_ids:
                logger.warning("跳过重复消息: message_id=%s", msg_id)
                return
            self._processed_message_ids.add(msg_id)
            if len(self._processed_message_ids) > 10000:
                excess = len(self._processed_message_ids) - 5000
                for _ in range(excess):
                    self._processed_message_ids.pop()

        self.record_user_interaction()
        self.runtime_metrics.inc_messages_received()
        self._sync_status_cache()

        # ── 初始化 ── 提取配置、会话键和跟踪日志
        plan = None
        current_event = event
        app_config = getattr(self.message_handler, "app_config", None)
        bot_behavior = getattr(app_config, "bot_behavior", None)
        response_timeout = int(getattr(bot_behavior, "response_timeout", 60) or 60)
        session_key_getter = getattr(self.message_handler, "_get_conversation_key", None)
        session_key = session_key_getter(event) if callable(session_key_getter) else get_execution_key(event)
        message_id = getattr(event, "message_id", 0)
        trace_log = format_trace_log(trace_id=trace_id, session_key=session_key, message_id=message_id)

        try:
            logger.info("开始处理消息：%s", trace_log)

            # ── 主循环 ── 每个周期完成：规划 → 节奏判断 → 回复生成 → 发送
            #    循环会在以下情况持续：窗口内多条消息待处理、或需要重试
            while True:
                # 阶段1：规划 — 调用 ConversationPlanner 决定 reply/wait/ignore
                logger.debug("开始规划：%s", trace_log)
                if plan is None:
                    plan = await asyncio.wait_for(
                        self.message_handler.plan_message(current_event, trace_id=trace_id),
                        timeout=max(1, response_timeout),
                    )
                logger.info(
                    "规划结果：%s action=%s source=%s reason=%s",
                    trace_log,
                    plan.action,
                    plan.source,
                    plan.reason,
                )
                logger.debug("规划原始：%s reply_reference=%s", trace_log, str(getattr(plan, "reply_reference", "") or "").strip())

                # 不需要回复：尝试分发到下一窗口（合并等待中的消息）
                if not plan.should_reply:
                    current_event, plan = await self._try_dispatch_next_window(plan, trace_id=trace_id)
                    if current_event is not None:
                        continue
                    return

                # 阶段2：节奏判断 — 调用 TimingGate 决定时机是否合适
                logger.debug("开始节奏判断：%s", trace_log)
                plan = await asyncio.wait_for(
                    self.message_handler.apply_timing_gate(current_event, plan=plan, trace_id=trace_id),
                    timeout=max(1, response_timeout),
                )
                logger.info(
                    "节奏判断结果：%s action=%s source=%s reason=%s",
                    trace_log,
                    plan.action,
                    plan.source,
                    plan.reason,
                )
                if not plan.should_reply:
                    current_event, plan = await self._try_dispatch_next_window(plan, trace_id=trace_id)
                    if current_event is not None:
                        continue
                    return

                # 阶段3：速率限制检查
                if current_event.message_type == MessageType.PRIVATE.value:
                    target_id = str(current_event.user_id)
                else:
                    inbound = get_attached_inbound_event(current_event)
                    if inbound is not None:
                        target_id = str(inbound.session.channel_id or "")
                    else:
                        target_id = str(current_event.raw_data.get("group_id", ""))
                await self.message_handler.check_rate_limit(target_id)
                await self.message_handler.check_rate_limit(target_id)

                # 阶段4：回复生成 — 调用 AI 生成回复内容
                logger.debug("开始生成回复：%s", trace_log)
                reply_result = await asyncio.wait_for(
                    self.message_handler.get_ai_response(current_event, plan=plan, trace_id=trace_id),
                    timeout=max(1, response_timeout),
                )
                if not reply_result or not reply_result.text:
                    logger.info("回复为空，跳过发送：%s", trace_log)
                    current_event, plan = await self._try_dispatch_next_window(plan, trace_id=trace_id)
                    if current_event is not None:
                        continue
                    return

                # 阶段5：发送回复 + 后续处理（emoji 追评、记忆写入）
                self._log_reply_preview(reply_result, trace_log, source=getattr(reply_result, "source", ""))
                logger.info("开始发送回复：%s", trace_log)
                sent = await self._send_response(current_event, reply_result, plan=plan, trace_id=trace_id)
                if sent:
                    logger.info("发送完成：%s", trace_log)
                    await self.message_handler.record_reply_sent(current_event, reply_result.text)
                    await self._send_emoji_follow_up_if_needed(current_event, reply_result, plan, trace_id=trace_id)

                # 分发到下一窗口，处理同一会话中的后续消息
                current_event, plan = await self._try_dispatch_next_window(plan, trace_id=trace_id)
                if current_event is not None:
                    continue
                logger.info("消息处理结束：%s", trace_log)
                return

        # ── 异常处理 ──
        # StaleWindowError / CancelledError：窗口过期或任务被取消，不需要发送错误回复
        except (StaleWindowError, asyncio.CancelledError):
            logger.info("窗口过期或任务被取消：%s", trace_log)
            await self._release_window_on_error(plan, trace_log)
        except asyncio.TimeoutError:
            logger.error("处理消息事件超时：%s timeout=%ss", trace_log, response_timeout)
            self.runtime_metrics.record_error(message_error=True)
            self._sync_status_cache()
            await self._release_window_on_error(plan, trace_log)
        except (SendError, ModelRequestError, ModelParseError, ImageProcessingError, MemoryOperationError, PipelineExecutionError) as exc:
            category = classify_pipeline_error(exc)
            logger.error("处理消息事件失败：%s category=%s 错误=%s", trace_log, category, exc, exc_info=True)
            self.runtime_metrics.record_error(message_error=True)
            self._sync_status_cache()
            await self._release_window_on_error(plan, trace_log)

    async def _send_response(self, event: MessageEvent, reply: Any, plan: Any = None, trace_id: str = "") -> bool:
        # stale 检查：发送前检查窗口是否已过期或已被 superseded
        reply_context = dict(getattr(plan, "reply_context", None) or {}) if plan else {}
        window_expires_at = float(reply_context.get("expires_at", 0.0) or 0.0)
        if window_expires_at > 0 and time.time() > window_expires_at:
            trace_log = format_trace_log(
                trace_id=trace_id,
                session_key=get_execution_key(event),
                message_id=getattr(event, "message_id", 0),
            )
            logger.warning("发送前检测到窗口已过期，跳过发送：%s", trace_log)
            return False
        message = str(getattr(reply, "text", reply) or "").strip()
        is_repeat_echo = getattr(plan, "source", None) == "repeat_echo"
        raw_segments = list(getattr(reply, "segments", None) or [])
        parts = self._build_reply_part_plans(
            event=event,
            message=message,
            raw_segments=raw_segments,
            force_single_part=is_repeat_echo,
        )
        reply_session = self._reply_session_for_event(event)
        if event.message_type == MessageType.PRIVATE.value:
            quote_reply_enabled = self._private_quote_reply_enabled()
            for index, part_plan in enumerate(parts):
                if part_plan.delay_before_seconds > 0:
                    await asyncio.sleep(part_plan.delay_before_seconds)
                message_id = getattr(event, "message_id", "")
                use_quote_reply = (
                    quote_reply_enabled
                    and index == 0
                    and bool(str(message_id or "").strip())
                )
                if use_quote_reply:
                    await self._send_private_segments(
                        reply_session,
                        [MessageSegment.reply(message_id), MessageSegment.text(part_plan.text)],
                        trace_id=trace_id,
                    )
                else:
                    await self._send_private_msg(reply_session, part_plan.text, trace_id=trace_id)
        else:
            at_user = self.message_handler.resolve_at_user(event, plan)
            for index, part_plan in enumerate(parts):
                if part_plan.delay_before_seconds > 0:
                    await asyncio.sleep(part_plan.delay_before_seconds)
                part_at_user = at_user if index == 0 else None
                await self._send_scope_msg(reply_session, part_plan.text, part_at_user, trace_id=trace_id)

        self.runtime_metrics.inc_messages_replied(len(parts))
        self._sync_status_cache()
        if self._should_log_message_summary():
            if event.message_type == MessageType.GROUP.value:
                inbound = get_attached_inbound_event(event)
                target_id = str(inbound.session.channel_id) if inbound else str(event.raw_data.get("group_id", ""))
            else:
                target_id = str(event.user_id)
            logger.info(
                "回复已发送：%s 目标=%s 类型=%s 分段=%s",
                format_trace_log(trace_id=trace_id, session_key=get_execution_key(event), message_id=getattr(event, "message_id", 0)),
                target_id,
                event.message_type,
                len(parts),
            )
        return True

    def _build_reply_part_plans(
        self,
        *,
        event: MessageEvent,
        message: str,
        raw_segments: List[str],
        force_single_part: bool,
    ) -> List[ReplyPartPlan]:
        if not hasattr(self, "_reply_send_orchestrator") or self._reply_send_orchestrator is None:
            self._reply_send_orchestrator = ReplySendOrchestrator(rng=random.Random())
        app_config = getattr(self.message_handler, "app_config", None)
        bot_behavior = getattr(app_config, "bot_behavior", None)
        segmented_reply_enabled = bool(getattr(bot_behavior, "segmented_reply_enabled", True))
        max_segments = int(getattr(bot_behavior, "max_segments", 3) or 3)
        if force_single_part:
            return [ReplyPartPlan(text=message, delay_before_seconds=0.0)] if message else []
        if segmented_reply_enabled:
            plans = self._reply_send_orchestrator.build_part_plan(
                segments=raw_segments,
                fallback_text=message,
                max_segments=max_segments,
                first_segment_delay_min_ms=int(getattr(bot_behavior, "first_segment_delay_min_ms", 0) or 0),
                first_segment_delay_max_ms=int(getattr(bot_behavior, "first_segment_delay_max_ms", 600) or 600),
                followup_delay_min_seconds=float(getattr(bot_behavior, "followup_delay_min_seconds", 3.0) or 0.0),
                followup_delay_max_seconds=float(getattr(bot_behavior, "followup_delay_max_seconds", 10.0) or 0.0),
            )
            if plans:
                return plans
        raw_parts = self.message_handler.split_by_sentence(message)
        parts: List[ReplyPartPlan] = []
        for part in raw_parts:
            for long_part in self.message_handler.split_long_message(part):
                normalized = str(long_part or "").strip()
                if normalized:
                    parts.append(ReplyPartPlan(text=normalized, delay_before_seconds=0.0))
        return parts

    async def _send_emoji_follow_up_if_needed(self, event: MessageEvent, reply_result: Any, plan: Any, *, trace_id: str = "") -> None:
        if event.message_type != MessageType.GROUP.value:
            return
        selection = await self.message_handler.plan_emoji_follow_up(event, reply_result, plan=plan)
        if not selection or not getattr(selection, "emoji", None):
            return

        action = self.message_handler.build_emoji_follow_up_action(selection, self._reply_session_for_event(event))
        if action is None:
            return

        try:
            result = await self._get_adapter_for_session(action.session).send_action(action)
            if result is False:
                raise SendError("发送表情跟进失败")
            await self.message_handler.mark_emoji_follow_up_sent(event, selection)
            if self._should_log_message_summary():
                inbound = get_attached_inbound_event(event)
                group_id = str(inbound.session.channel_id) if inbound else str(event.raw_data.get("group_id", ""))
                logger.info(
                    "群聊表情跟进已发送：%s group=%s emoji_id=%s kind=%s",
                    format_trace_log(trace_id=trace_id, session_key=get_execution_key(event), message_id=getattr(event, "message_id", 0)),
                    group_id,
                    getattr(selection.emoji, "emoji_id", ""),
                    getattr(selection.emoji, "sticker_kind", ""),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "发送表情跟进失败：%s category=%s 错误=%s",
                format_trace_log(trace_id=trace_id, session_key=get_execution_key(event), message_id=getattr(event, "message_id", 0)),
                classify_pipeline_error(exc),
                exc,
                exc_info=True,
            )
            self.runtime_metrics.record_error(message_error=True)
            self._sync_status_cache()

    async def _send_private_msg(self, target: Any, message: str, *, trace_id: str = ""):
        session = self._resolve_private_reply_session(target)
        result = await self._get_adapter_for_session(session).send_action(
            ReplyAction(
                session=session,
                text=message,
            )
        )
        if result is False:
            raise SendError("发送私聊消息失败")
        logger.debug("已发送私聊消息：session=%s", session.key)

    async def _send_private_segments(self, target: Any, segments: List[MessageSegment], *, trace_id: str = "") -> None:
        self._ensure_no_outbound_image_segments(segments)
        session = self._resolve_private_reply_session(target)
        result = await self._get_adapter_for_session(session).send_action(
            ReplyAction(
                session=session,
                segments=tuple(segment.to_dict() for segment in segments),
            )
        )
        if result is False:
            raise SendError("发送私聊分段消息失败")
        logger.debug("已发送私聊分段消息：session=%s，segments=%s", session.key, len(segments))

    async def _send_scope_msg(self, target: Any, message: str, at_user: Optional[Any] = None, *, trace_id: str = ""):
        session = self._resolve_reply_session(target, at_user=at_user)
        if at_user:
            segments = [self._build_mention_segment(session, at_user)]
            if message:
                segments.append(MessageSegment.text(f" {message}").to_dict())
            result = await self._get_adapter_for_session(session).send_action(
                ReplyAction(session=session, segments=tuple(dict(segment or {}) for segment in segments))
            )
            if result is False:
                raise SendError("发送群聊 @ 消息失败")
            logger.debug("已发送群聊@消息：session=%s，at_user=%s", session.key, at_user)
            return

        result = await self._get_adapter_for_session(session).send_action(ReplyAction(session=session, text=message))
        if result is False:
            raise SendError("发送群聊消息失败")
        logger.debug("已发送群聊消息：session=%s", session.key)

    async def _send_scope_segments(self, target: Any, segments: List[MessageSegment], *, trace_id: str = "") -> None:
        self._ensure_no_outbound_image_segments(segments)
        session = self._resolve_reply_session(target)
        result = await self._get_adapter_for_session(session).send_action(
            ReplyAction(
                session=session,
                segments=tuple(segment.to_dict() for segment in segments),
            )
        )
        if result is False:
            raise SendError("发送群聊分段消息失败")
        logger.debug("已发送群聊分段消息：session=%s，segments=%s", session.key, len(segments))

    def _ensure_no_outbound_image_segments(self, segments: List[MessageSegment]) -> None:
        for segment in list(segments or []):
            if segment.is_image():
                raise SendError("当前运行模式禁止主动发送 image 段")

    def _private_quote_reply_enabled(self) -> bool:
        app_config = getattr(self.message_handler, "app_config", None)
        bot_behavior = getattr(app_config, "bot_behavior", None)
        return bool(getattr(bot_behavior, "private_quote_reply_enabled", False))

    def _should_log_message_summary(self) -> bool:
        app_config = getattr(self.message_handler, "app_config", None)
        bot_behavior = getattr(app_config, "bot_behavior", None)
        return not bool(getattr(bot_behavior, "log_full_prompt", False))

    async def _on_websocket_message(self, data: Dict[str, Any]):
        try:
            await self.ingest_adapter_payload(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("处理 WebSocket 消息失败：%s", exc, exc_info=True)
            self.runtime_metrics.record_error()
            self._sync_status_cache()

    async def ingest_inbound_event(self, inbound_event: InboundEvent, *, self_id: Any = "") -> None:
        resolved_self_id = self_id or inbound_event.session.account_id or ""
        await self.dispatcher.dispatch_inbound_event(inbound_event, self_id=resolved_self_id)

    async def ingest_adapter_payload(
        self,
        payload: Dict[str, Any],
        *,
        adapter: Any = None,
        self_id: Any = "",
    ) -> None:
        adapter_obj = adapter or self.adapter or self.connection
        if adapter_obj is not None:
            self.register_runtime_adapter(adapter_obj)
        normalizer = getattr(adapter_obj, "normalize_inbound_payload", None)
        if callable(normalizer):
            inbound_event = normalizer(payload)
            if inbound_event is not None:
                await self.ingest_inbound_event(inbound_event, self_id=self_id or payload.get("self_id", ""))
                return
        await self.dispatcher.dispatch(payload)

    async def _on_connect(self):
        self.runtime_metrics.set_connected(True)
        self.runtime_metrics.set_ready(True)
        self._sync_status_cache()
        logger.debug("平台 adapter 已连接")

    async def _on_disconnect(self):
        self.runtime_metrics.set_connected(False)
        self.runtime_metrics.set_ready(False)
        self._sync_status_cache()
        logger.debug("平台 adapter 连接已断开")

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
        self._connection_task = asyncio.create_task(self._get_adapter().run())
        self._snapshot_task = asyncio.create_task(self._run_webui_snapshot_heartbeat())
        scheduler = getattr(self, "_proactive_scheduler", None)
        if scheduler:
            await scheduler.start(self)

        try:
            await self._shutdown_event.wait()
        except asyncio.CancelledError:
            logger.info("机器人运行任务被取消")
        finally:
            await self.close()
            logger.info("机器人主循环已退出")

    async def close(self) -> None:
        async with self._get_close_lock():
            if self._closed:
                return

            self._running = False
            self._closed = True
            self._shutdown_event.set()

        await self._cancel_message_tasks()

        adapter = self.adapter or self.connection
        if adapter:
            try:
                await adapter.disconnect()
            finally:
                await cancel_task(self._connection_task, label="connection_task")
                self._connection_task = None
        else:
            await cancel_task(self._connection_task, label="connection_task")
            self._connection_task = None

        await cancel_task(self._snapshot_task, label="webui_snapshot_task")
        self._snapshot_task = None

        scheduler = getattr(self, "_proactive_scheduler", None)
        if scheduler:
            await scheduler.stop()
            self._proactive_scheduler = None
            self._proactive_share_store = None

        await close_resource(self.message_handler, label="message_handler")
        await close_resource(self.memory_manager, label="memory_manager")
        await self._model_router.close()

        self.runtime_metrics.set_state(
            connected=False,
            ready=False,
            active_message_tasks=0,
            active_session_workers=0,
            active_model_workers=0,
            pending_model_jobs=0,
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
        pipeline_snapshot = self._message_pipeline.snapshot() if self._message_pipeline else {"active_workers": 0}
        model_snapshot = self._model_router.snapshot() if self._model_router else {"active_workers": 0, "pending_jobs": 0}
        self.runtime_metrics.set_state(
            active_message_tasks=int(pipeline_snapshot.get("active_workers", 0)),
            active_session_workers=int(pipeline_snapshot.get("active_workers", 0)),
            active_model_workers=int(model_snapshot.get("active_workers", 0)),
            pending_model_jobs=int(model_snapshot.get("pending_jobs", 0)),
            active_conversations=active_conversations,
        )

    def _get_adapter(self):
        adapter = self.adapter or self.connection
        if adapter is None:
            raise RuntimeError("platform adapter is not initialized")
        return adapter

    def register_runtime_adapter(self, adapter: Any) -> None:
        if adapter is None:
            return
        if not hasattr(self, "_adapters_by_name") or not isinstance(self._adapters_by_name, dict):
            self._adapters_by_name = {}
        if not hasattr(self, "_adapters_by_platform") or not isinstance(self._adapters_by_platform, dict):
            self._adapters_by_platform = {}
        adapter_name = str(getattr(adapter, "adapter_name", "") or "").strip()
        platform = str(getattr(adapter, "platform", "") or "").strip()
        if adapter_name:
            self._adapters_by_name[adapter_name] = adapter
        if platform:
            self._adapters_by_platform[platform] = adapter

    def _default_adapter_name(self) -> str:
        adapter = self.adapter or self.connection
        adapter_name = str(getattr(adapter, "adapter_name", "") or "").strip()
        if adapter_name:
            return adapter_name
        config = getattr(self, "config", None)
        app_config = getattr(config, "app", None)
        adapter_connection = getattr(app_config, "adapter_connection", None)
        adapter_name = str(getattr(adapter_connection, "adapter", "") or "").strip()
        return adapter_name or "unknown"

    def _default_session_platform(self) -> str:
        adapter = self.adapter or self.connection
        platform = str(getattr(adapter, "platform", "") or "").strip()
        if platform:
            return platform
        config = getattr(self, "config", None)
        app_config = getattr(config, "app", None)
        adapter_connection = getattr(app_config, "adapter_connection", None)
        platform = str(getattr(adapter_connection, "platform", "") or "").strip()
        return platform or "unknown"

    def _get_adapter_for_session(self, session: SessionRef):
        adapter = None
        platform = str(session.platform or "").strip()
        if platform:
            adapter = self._adapters_by_platform.get(platform)
        return adapter or self._get_adapter()

    def _reply_session_for_event(self, event: MessageEvent) -> SessionRef:
        inbound_event = get_attached_inbound_event(event)
        if inbound_event is not None:
            return inbound_event.session
        if event.message_type == MessageType.GROUP.value:
            group_id = event.raw_data.get("group_id", "")
            return self._fallback_reply_session(group_id)
        return self._fallback_private_reply_session(event.user_id)

    def _resolve_private_reply_session(self, target: Any) -> SessionRef:
        if isinstance(target, SessionRef):
            return target if target.scope == "private" else replace(target, scope="private")
        return self._fallback_private_reply_session(target)

    def _resolve_reply_session(self, target: Any, *, at_user: Optional[Any] = None) -> SessionRef:
        if isinstance(target, SessionRef):
            session = target if target.scope in {"shared", "channel"} else replace(target, scope="shared")
            if at_user not in (None, ""):
                return replace(session, user_id=str(at_user))
            return session
        return self._fallback_reply_session(target, user_id=at_user)

    def _fallback_private_reply_session(self, user_id: Any) -> SessionRef:
        resolved_user_id = str(user_id or "")
        return SessionRef(
            platform=self._default_session_platform(),
            scope="private",
            conversation_id=f"private:{resolved_user_id}",
            user_id=resolved_user_id,
        )

    def _fallback_reply_session(self, group_id: Any, *, user_id: Any = "") -> SessionRef:
        resolved_group_id = str(group_id or "")
        resolved_user_id = str(user_id or "")
        return SessionRef(
            platform=self._default_session_platform(),
            scope="shared",
            conversation_id=f"group:{resolved_group_id}:{resolved_user_id or 0}",
            channel_id=resolved_group_id,
            user_id=resolved_user_id,
        )

    def _build_mention_segment(self, session: SessionRef, user_id: Any) -> Dict[str, Any]:
        adapter = getattr(self, "adapter", None)
        if adapter is not None and hasattr(adapter, "build_mention_payload"):
            return adapter.build_mention_payload(str(user_id))
        if session.platform == "qq":
            return MessageSegment.at(user_id).to_dict()
        return {"type": "mention", "data": {"user_id": str(user_id)}}

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

    def _on_pipeline_state_change(self, state: Dict[str, int]) -> None:
        active_workers = int(state.get("active_workers", 0))
        self.runtime_metrics.set_state(
            active_message_tasks=active_workers,
            active_session_workers=active_workers,
        )
        self._sync_status_cache()

    def _on_model_router_state_change(self, state: Dict[str, Any]) -> None:
        self.runtime_metrics.set_state(
            active_model_workers=int(state.get("active_workers", 0)),
            pending_model_jobs=int(state.get("pending_jobs", 0)),
        )
        self._sync_status_cache()

    def _log_plan_preview(self, plan: Any, trace_log: str) -> None:
        # Already logged inline at INFO; this is kept for raw_decision dump at DEBUG
        raw_decision = getattr(plan, "raw_decision", None)
        if raw_decision is None:
            return
        logger.debug(
            "规划原始：%s reply_reference=%s preview=%s",
            trace_log,
            preview_text_for_log(str(getattr(plan, "reply_reference", "") or "").strip()),
            preview_json_for_log(raw_decision),
        )

    def _log_reply_preview(self, reply: Any, trace_log: str, *, source: str = "") -> None:
        reply_text = str(getattr(reply, "text", reply) or "").strip()
        preview = preview_text_for_log(reply_text)
        if not preview:
            return
        normalized_source = str(source or "").strip() or "unknown"
        segments = list(getattr(reply, "segments", None) or [])
        if segments:
            logger.info(
                "最终回复预览：%s source=%s segments=%s content=%s raw_segments=%s",
                trace_log,
                normalized_source,
                len(segments),
                preview,
                preview_json_for_log(segments),
            )
            return
        logger.info("最终回复预览：%s source=%s content=%s", trace_log, normalized_source, preview)
