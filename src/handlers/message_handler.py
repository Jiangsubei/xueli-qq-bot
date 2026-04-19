from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import defaultdict, deque
from typing import Any, Deque, Dict, List, Optional

from src.core.config import AppConfig, config, get_vision_service_status, is_group_reply_decision_configured, is_vision_service_configured
from src.core.message_trace import format_trace_log, get_execution_key
from src.core.model_invocation_router import ModelInvocationRouter
from src.core.pipeline_errors import ImageProcessingError, wrap_image_error
from src.core.platform_normalizers import event_mentions_account, get_attached_inbound_event, get_inbound_reply_to_message_id
from src.core.models import (
    Conversation,
    MessageEvent,
    MessageHandlingPlan,
    MessagePlanAction,
    MessageType,
)
from src.core.runtime_metrics import RuntimeMetrics
from src.emoji.manager import EmojiManager
from src.emoji.reply_service import EmojiReplyService
from src.handlers.command_handler import CommandHandler
from src.handlers.conversation_planner import ConversationPlanner
from src.handlers.conversation_session_manager import ConversationSessionManager
from src.handlers.group_plan_coordinator import GroupPlanCoordinator
from src.handlers.message_context import MessageContext
from src.handlers.reply_pipeline import ReplyPipeline, ReplyResult
from src.handlers.temporal_context import build_temporal_context, normalize_event_time
from src.services.ai_client import AIClient, AIResponse
from src.services.image_client import ImageClient
from src.services.vision_client import VisionClient

logger = logging.getLogger(__name__)


class MessageHandler:
    """High-level message orchestration layer."""

    def __init__(
        self,
        ai_client: Optional[AIClient] = None,
        image_client: Optional[ImageClient] = None,
        vision_client: Optional[VisionClient] = None,
        memory_manager: Optional[Any] = None,
        group_reply_planner: Optional[ConversationPlanner] = None,
        *,
        runtime_metrics: Optional[RuntimeMetrics] = None,
        status_provider: Optional[Any] = None,
        app_config: Optional[AppConfig] = None,
        model_invocation_router: Optional[ModelInvocationRouter] = None,
    ) -> None:
        self.app_config = app_config or config.app
        self.runtime_metrics = runtime_metrics
        self.memory_manager = memory_manager
        self.model_invocation_router = model_invocation_router
        self.ai_client = ai_client or self._create_ai_client()
        self.image_client = image_client or ImageClient()
        self.vision_client = vision_client or self._create_vision_client()
        self.emoji_manager = EmojiManager(
            vision_client=self.vision_client,
            runtime_metrics=self.runtime_metrics,
            app_config=self.app_config,
        )
        self.emoji_reply_service = EmojiReplyService(
            repository=self.emoji_manager.repository,
            ai_client=self.ai_client,
            runtime_metrics=self.runtime_metrics,
            app_config=self.app_config,
            model_invocation_router=self.model_invocation_router,
        )
        self.group_reply_planner = group_reply_planner or ConversationPlanner(
            app_config=self.app_config,
            model_invocation_router=self.model_invocation_router,
        )

        self.session_manager = ConversationSessionManager()
        self.group_plan_coordinator = GroupPlanCoordinator(
            planner=self.group_reply_planner,
            session_manager=self.session_manager,
            runtime_metrics=self.runtime_metrics,
            group_reply_config=self.app_config.group_reply,
            context_window_size=self.app_config.bot_behavior.max_context_length,
            event_text_getter=self._get_event_text,
            sender_display_name_getter=self._get_sender_display_name,
            has_image_getter=self._has_image_input,
            image_count_getter=self._get_image_count,
            image_file_ids_getter=self._get_image_file_ids,
            image_analyzer=(
                (lambda event, user_text, trace_id="": self.analyze_event_images(event, user_text, trace_id=trace_id))
                if self.vision_enabled()
                else None
            ),
        )
        self.command_handler = CommandHandler(
            self.session_manager,
            status_provider=status_provider,
            runtime_metrics=self.runtime_metrics,
            app_config=self.app_config,
            reset_callback=self._handle_reset_command,
        )
        self.reply_pipeline = ReplyPipeline(self)

        self.last_send_time: Dict[str, float] = {}
        self.rate_limit_lock = asyncio.Lock()
        self.at_pattern = re.compile(r"\[CQ:at,qq=\d+\]")
        self.private_batch_lock = asyncio.Lock()
        self.private_batch_versions: Dict[str, int] = defaultdict(int)
        self.private_pending_inputs: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.private_batch_window_seconds = float(getattr(self.app_config.bot_behavior, "private_batch_window_seconds", 1.2) or 0.0)
        self.group_repeat_lock = asyncio.Lock()
        self._group_repeat_history: Dict[int, Deque[Dict[str, Any]]] = defaultdict(deque)
        self._group_repeat_cooldowns: Dict[tuple[int, str], float] = {}

        self._sync_active_conversations_metric()

    async def initialize(self) -> None:
        if self.emoji_manager:
            await self.emoji_manager.initialize()

    def set_status_provider(self, status_provider: Any) -> None:
        self.command_handler.set_status_provider(status_provider)

    def _create_ai_client(self) -> AIClient:
        logger.debug("初始化回复模型：模型=%s", self.app_config.ai_service.model)
        return AIClient(log_label="reply", app_config=self.app_config)

    def _create_vision_client(self) -> VisionClient:
        return VisionClient(app_config=self.app_config, model_invocation_router=self.model_invocation_router)

    def _get_assistant_name(self) -> str:
        if self.app_config is config.app:
            return config.get_assistant_name()
        alias = self.app_config.assistant_profile.name.strip()
        return alias or config.get_assistant_name()

    def _get_assistant_alias(self) -> str:
        if self.app_config is config.app:
            return config.get_assistant_alias()
        return self.app_config.assistant_profile.alias.strip()

    def _get_memory_read_scope(self) -> str:
        if self.app_config is config.app:
            return config.get_memory_read_scope()
        return self.app_config.memory.read_scope

    def _should_label_memory_owner(self) -> bool:
        return self._get_memory_read_scope() == "global"

    def _format_memory_prompt_entry(self, content: str, owner_user_id: str = "") -> str:
        text = str(content or "").strip()
        owner = str(owner_user_id or "").strip()
        if owner and self._should_label_memory_owner():
            return f"[来源用户 {owner}] {text}"
        return text

    def _format_identity_label(self, user_id: Any, display_name: str = "") -> str:
        identifier = str(user_id or "").strip() or "unknown"
        name = str(display_name or "").strip()
        if name and name != identifier:
            return f"{identifier}（{name}）"
        return identifier

    def _build_assistant_identity_text(self) -> str:
        assistant_name = self._get_assistant_name()
        assistant_alias = self._get_assistant_alias()
        if assistant_alias:
            return (
                f"你的名字是“{assistant_name}”，别名是“{assistant_alias}”。"
                f"当用户提到“{assistant_name}”或“{assistant_alias}”时，说的都是你。"
            )
        return f"你的名字是“{assistant_name}”。当用户提到“{assistant_name}”时，说的就是你。"

    def _build_assistant_identity_prompt(self) -> str:
        return self._build_assistant_identity_text()

    def vision_enabled(self) -> bool:
        if not self.vision_client:
            return False
        available = getattr(self.vision_client, "is_available", None)
        if callable(available):
            return bool(available())
        return bool(is_vision_service_configured(self.app_config))

    def vision_status(self) -> str:
        if self.vision_client:
            status = getattr(self.vision_client, "status", None)
            if callable(status):
                return str(status())
        return get_vision_service_status(self.app_config)

    def _build_system_prompt(self) -> str:
        parts = []
        if self.app_config.personality.content:
            parts.append(self.app_config.personality.content)
        if self.app_config.dialogue_style.content:
            parts.append(self.app_config.dialogue_style.content)
        if self.app_config.behavior.content:
            parts.append(self.app_config.behavior.content)
        return "\n\n".join(parts) if parts else "你是一个友好、可靠、有帮助的 AI 助手。"

    def _build_memory_tools(self) -> List[Dict[str, Any]]:
        return self.reply_pipeline.build_memory_tools()

    def _augment_system_prompt_for_tools(self, system_prompt: str, tools: List[Dict[str, Any]]) -> str:
        return self.reply_pipeline.augment_system_prompt_for_tools(system_prompt, tools)

    async def _execute_tool_call(self, tool_call: Dict[str, Any], user_id: str) -> Dict[str, Any]:
        return await self.reply_pipeline.execute_tool_call(tool_call, user_id)

    async def _chat_with_tools(
        self,
        messages: List[Dict[str, Any]],
        user_id: str,
        temperature: float = 0.7,
        event: Optional[MessageEvent] = None,
    ) -> AIResponse:
        return await self.reply_pipeline.chat_with_tools(messages=messages, user_id=user_id, temperature=temperature, event=event)

    def _format_prompt_message_content(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            lines = []
            image_count = 0
            for part in content:
                if not isinstance(part, dict):
                    lines.append(str(part))
                    continue
                if part.get("type") == "text":
                    text = part.get("text", "")
                    if text:
                        lines.append(text)
                elif part.get("type") == "image_url":
                    image_count += 1
            if image_count:
                lines.append(f"[图片 {image_count} 张]")
            return "\n".join(lines) if lines else str(content)
        return str(content)

    def _serialize_prompt_message_content(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        try:
            return json.dumps(content, ensure_ascii=False, indent=2)
        except TypeError:
            return str(content)

    def _format_system_prompt_log_with_history(
        self,
        event: MessageEvent,
        messages: List[Dict[str, Any]],
        related_history_messages: Optional[List[Dict[str, Any]]] = None,
        title: str = "[FULL PROMPT]",
        trace_id: str = "",
    ) -> str:
        del related_history_messages
        system_prompt = ""
        for message in messages or []:
            if str(message.get("role") or "").strip().lower() != "system":
                continue
            system_prompt = self._format_prompt_message_content(message.get("content", ""))
            break

        lines = [
            title,
            f"trace: {trace_id}" if trace_id else "",
            f"用户: {event.user_id}",
            f"会话: {self._get_conversation_key(event)}",
            "",
            "--- 完整提示词 ---",
            system_prompt or "[空]",
        ]
        return "\n".join(line for line in lines if line is not None).rstrip()

    def _get_inbound_event(self, event: MessageEvent):
        return get_attached_inbound_event(event)

    def _get_conversation_key(self, event: MessageEvent) -> str:
        inbound_event = self._get_inbound_event(event)
        if inbound_event is not None:
            return self.session_manager.get_key_for_inbound_event(inbound_event)
        return self.session_manager.get_key(event)

    def _get_sender_display_name(self, event: MessageEvent) -> str:
        inbound_event = self._get_inbound_event(event)
        if inbound_event is not None and inbound_event.sender.display_name:
            return str(inbound_event.sender.display_name)
        return event.get_sender_display_name()

    def _get_event_text(self, event: MessageEvent) -> str:
        inbound_event = self._get_inbound_event(event)
        if inbound_event is not None and inbound_event.text:
            return str(inbound_event.text)
        return event.extract_text()

    def _has_image_input(self, event: MessageEvent) -> bool:
        return self._get_image_count(event) > 0

    def _get_image_count(self, event: MessageEvent) -> int:
        inbound_event = self._get_inbound_event(event)
        if inbound_event is not None:
            return sum(1 for attachment in inbound_event.attachments if str(attachment.kind or "") == "image")
        return len(event.get_image_segments())

    def _get_image_file_ids(self, event: MessageEvent) -> List[str]:
        inbound_event = self._get_inbound_event(event)
        if inbound_event is not None:
            return [
                str(attachment.attachment_id)
                for attachment in inbound_event.attachments
                if str(attachment.kind or "") == "image" and str(attachment.attachment_id or "")
            ]
        return event.get_image_file_ids()

    def _is_direct_mention(self, event: MessageEvent) -> bool:
        return event_mentions_account(event)

    def _get_reply_to_message_id(self, event: MessageEvent) -> str:
        return get_inbound_reply_to_message_id(event)

    def _get_conversation(self, key: str) -> Conversation:
        conversation = self.session_manager.get(key)
        self._sync_active_conversations_metric()
        return conversation

    def _clean_expired_conversations(self) -> None:
        self.session_manager.clean_expired()
        self._cleanup_private_batch_state()
        self._cleanup_group_repeat_state()
        self._sync_active_conversations_metric()

    def _cleanup_private_batch_state(self) -> None:
        active_keys = set(getattr(self.session_manager, "_conversations", {}).keys())
        now = time.time()
        stale_after = max(30.0, self.private_batch_window_seconds * 8)
        stale_keys: List[str] = []
        for key, items in list(self.private_pending_inputs.items()):
            if key not in active_keys:
                stale_keys.append(key)
                continue
            latest_time = max((float(item.get("inserted_at", 0.0) or 0.0) for item in items), default=0.0)
            if latest_time > 0 and now - latest_time > stale_after:
                stale_keys.append(key)
        for key in stale_keys:
            self.private_pending_inputs.pop(key, None)
            self.private_batch_versions.pop(key, None)

    def _cleanup_group_repeat_state(self) -> None:
        now = time.time()
        max_window = max(20.0, float(self.app_config.group_reply.repeat_echo_window_seconds or 20.0))
        history_cutoff = max_window * 3
        stale_groups: List[int] = []
        for group_id, history in self._group_repeat_history.items():
            while history and now - float(history[0].get("time", 0.0) or 0.0) > history_cutoff:
                history.popleft()
            if not history:
                stale_groups.append(group_id)
        for group_id in stale_groups:
            self._group_repeat_history.pop(group_id, None)

        stale_cooldowns = [
            key for key, until in self._group_repeat_cooldowns.items() if float(until or 0.0) <= now
        ]
        for key in stale_cooldowns:
            self._group_repeat_cooldowns.pop(key, None)

    def _format_group_window_context(self, reply_context: Optional[Dict[str, Any]]) -> str:
        return self.group_plan_coordinator.format_group_window_context(reply_context)

    def _build_rule_plan(
        self,
        action: MessagePlanAction,
        reason: str,
        source: str = "rule",
        reply_context: Optional[Dict[str, Any]] = None,
    ) -> MessageHandlingPlan:
        return MessageHandlingPlan(action=action.value, reason=reason, source=source, reply_context=reply_context)

    def _group_planner_available(self) -> bool:
        return is_group_reply_decision_configured(self.app_config)

    def _normalize_repeat_echo_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "").strip())

    def _is_repeat_echo_candidate(self, event: MessageEvent, text: str) -> bool:
        if event.message_type != MessageType.GROUP.value or event.group_id is None:
            return False
        if not self.app_config.group_reply.repeat_echo_enabled:
            return False
        if self._is_direct_mention(event) or self._has_image_input(event):
            return False
        normalized = self._normalize_repeat_echo_text(text)
        if not normalized or normalized.startswith("/"):
            return False
        return 2 <= len(normalized) <= 20

    async def _check_repeat_echo_trigger(self, event: MessageEvent) -> Optional[str]:
        display_text = self._normalize_repeat_echo_text(self.extract_user_message(event))
        if not self._is_repeat_echo_candidate(event, display_text):
            return None

        async with self.group_repeat_lock:
            now = time.time()
            group_id = int(event.group_id or 0)
            key = display_text.casefold()
            window_seconds = float(self.app_config.group_reply.repeat_echo_window_seconds or 20.0)
            min_count = max(2, int(self.app_config.group_reply.repeat_echo_min_count or 2))
            cooldown_seconds = max(0.0, float(self.app_config.group_reply.repeat_echo_cooldown_seconds or 0.0))

            history = self._group_repeat_history[group_id]
            while history and now - float(history[0]["time"]) > window_seconds:
                history.popleft()

            same_entries = [item for item in history if item.get("key") == key]
            unique_users = {int(item.get("user_id", 0)) for item in same_entries}
            unique_users.add(int(event.user_id))

            history.append({"time": now, "key": key, "user_id": int(event.user_id)})

            cooldown_key = (group_id, key)
            cooldown_until = float(self._group_repeat_cooldowns.get(cooldown_key, 0.0) or 0.0)
            if cooldown_until > now:
                return None

            if len(unique_users) < min_count:
                return None

            self._group_repeat_cooldowns[cooldown_key] = now + cooldown_seconds
        if self.runtime_metrics:
            self.runtime_metrics.record_group_repeat_echo()
        if not bool(getattr(self.app_config.bot_behavior, "log_full_prompt", False)):
            logger.info("触发群聊复读：群=%s，触发用户数=%s", group_id, len(unique_users))
        return display_text

    async def _plan_group_message(self, event: MessageEvent, *, trace_id: str = "") -> MessageHandlingPlan:
        return await self.group_plan_coordinator.plan_group_message(
            event=event,
            user_message=self.extract_user_message(event),
            trace_id=trace_id,
        )

    async def _plan_private_message(self, event: MessageEvent, *, trace_id: str = "") -> MessageHandlingPlan:
        conversation_key = self._get_conversation_key(event)
        conversation = self._get_conversation(conversation_key)
        user_message = self.extract_user_message(event)
        temporal_context = self._build_temporal_context(
            event=event,
            conversation=conversation,
            reply_context=None,
        )
        context = MessageContext(
            trace_id=trace_id,
            execution_key=get_execution_key(event),
            conversation_key=conversation_key,
            user_message=user_message,
            current_sender_label=self._format_identity_label(event.user_id, self._get_sender_display_name(event)),
            is_first_turn=len(conversation.messages) == 0,
            current_event_time=temporal_context.current_event_time,
            previous_message_time=temporal_context.previous_message_time,
            conversation_last_time=temporal_context.conversation_last_time,
            previous_session_time=temporal_context.previous_session_time,
            temporal_context=temporal_context,
            recent_history_text=self.reply_pipeline._build_recent_history_text(
                event=event,
                conversation=conversation,
                plan=None,
            ),
            planning_signals=self._build_private_planning_signals(
                event=event,
                user_message=user_message,
                conversation=conversation,
                temporal_context=temporal_context,
            ),
            conversation=conversation,
        )
        current_version = await self._push_private_pending_input(
            conversation_key=conversation_key,
            event=event,
            user_message=user_message,
        )
        hold_reason = self._private_hold_reason(event, user_message)
        if hold_reason:
            return MessageHandlingPlan(
                action=MessagePlanAction.WAIT.value,
                reason=hold_reason,
                source="rule",
                reply_context={"trace_id": trace_id} if trace_id else {},
            )
        await asyncio.sleep(self.private_batch_window_seconds)
        if await self._has_newer_private_input(conversation_key, current_version):
            return MessageHandlingPlan(
                action=MessagePlanAction.WAIT.value,
                reason="用户仍在短时间内连续补充，先等待本轮私聊输入稳定",
                source="rule",
                reply_context={"trace_id": trace_id} if trace_id else {},
            )
        pending_items = await self._consume_private_pending_input(conversation_key)
        merged_user_message = self._merge_private_pending_input(pending_items, user_message)
        context.user_message = merged_user_message
        context.planning_signals = self._build_private_planning_signals(
            event=event,
            user_message=merged_user_message,
            conversation=conversation,
            temporal_context=temporal_context,
            pending_items=pending_items,
        )
        plan = await self.group_reply_planner.plan(
            event=event,
            user_message=merged_user_message,
            recent_messages=[],
            context=context,
        )
        reply_context = dict(plan.reply_context or {})
        if trace_id:
            reply_context["trace_id"] = trace_id
        if plan.should_reply:
            reply_context.setdefault("reply_mode", "private")
            reply_context["merged_user_message"] = merged_user_message
        return MessageHandlingPlan(
            action=plan.action,
            reason=plan.reason,
            source=plan.source,
            raw_decision=plan.raw_decision,
            reply_context=reply_context,
            prompt_plan=plan.prompt_plan,
        )

    async def _build_group_at_reply_context(self, event: MessageEvent, *, trace_id: str = "") -> Dict[str, Any]:
        return await self.group_plan_coordinator.build_direct_reply_context(
            event=event,
            user_message=self.extract_user_message(event),
            reply_mode="at",
            planner_mode="direct_at",
            trace_id=trace_id,
        )

    async def plan_message(self, event: MessageEvent, *, trace_id: str = "") -> MessageHandlingPlan:
        self._record_background_activity()
        self._clean_expired_conversations()

        if event.user_id == event.self_id:
            return self._build_rule_plan(MessagePlanAction.IGNORE, "机器人自己的消息，跳过处理")
        if event.message_type == MessageType.PRIVATE.value:
            return await self._plan_private_message(event, trace_id=trace_id)
        if event.message_type != MessageType.GROUP.value:
            return self._build_rule_plan(MessagePlanAction.IGNORE, "当前仅处理私聊和群聊消息")

        repeat_echo_text = await self._check_repeat_echo_trigger(event)
        if repeat_echo_text:
            return self._build_rule_plan(
                MessagePlanAction.REPLY,
                "群里短时间内连续出现了相同消息，复读一次",
                source="repeat_echo",
                reply_context={"direct_reply_text": repeat_echo_text, "reply_mode": "repeat_echo"},
            )

        planner_available = self._group_planner_available()
        only_at_mode = self.app_config.group_reply.only_reply_when_at or not planner_available

        if only_at_mode:
            if self._is_direct_mention(event):
                reply_context = await self._build_group_at_reply_context(event, trace_id=trace_id)
                if planner_available and self.app_config.group_reply.only_reply_when_at:
                    return self._build_rule_plan(
                        MessagePlanAction.REPLY,
                        "群聊仅在被 @ 时回复，当前消息命中 @",
                        reply_context=reply_context,
                    )
                return self._build_rule_plan(
                    MessagePlanAction.REPLY,
                    "未配置群聊判断模型，当前仅在被 @ 时回复",
                    reply_context=reply_context,
                )
            if planner_available and self.app_config.group_reply.only_reply_when_at:
                return self._build_rule_plan(MessagePlanAction.IGNORE, "群聊仅在被 @ 时回复，跳过未 @ 消息")
            return self._build_rule_plan(MessagePlanAction.IGNORE, "未配置群聊判断模型，当前仅在被 @ 时回复")

        if self._is_direct_mention(event):
            reply_context = await self._build_group_at_reply_context(event, trace_id=trace_id)
            return self._build_rule_plan(
                MessagePlanAction.REPLY,
                "群聊消息显式 @ 了助手，直接回复",
                reply_context=reply_context,
            )

        return await self._plan_group_message(event, trace_id=trace_id)

    def should_process(self, event: MessageEvent) -> bool:
        if event.user_id == event.self_id:
            return False
        if event.message_type == MessageType.PRIVATE.value:
            return True
        if event.message_type == MessageType.GROUP.value:
            only_at_mode = self.app_config.group_reply.only_reply_when_at or not self._group_planner_available()
            return self._is_direct_mention(event) if only_at_mode else True
        return False

    def extract_user_message(self, event: MessageEvent) -> str:
        text = self._get_event_text(event)
        if event.message_type == MessageType.GROUP.value:
            text = self.at_pattern.sub("", text)
            text = text.replace(f"@{self._get_assistant_name()}", "")
        return text.strip()

    def _build_temporal_context(
        self,
        *,
        event: MessageEvent,
        conversation: Conversation,
        reply_context: Optional[Dict[str, Any]] = None,
    ):
        current_event_time = normalize_event_time(getattr(event, "time", 0.0)) or time.time()
        window_messages = list((reply_context or {}).get("window_messages") or [])
        previous_message_time = 0.0
        history_event_times: List[float] = []

        if window_messages:
            history_event_times = [float(normalize_event_time(item.get("event_time", 0.0)) or 0.0) for item in window_messages]
            previous_items = [item for item in window_messages if not bool(item.get("is_latest"))]
            if previous_items:
                previous_message_time = float(normalize_event_time(previous_items[-1].get("event_time", 0.0)) or 0.0)

        if previous_message_time <= 0 and conversation.messages:
            previous_message_time = float(conversation.last_update or 0.0)

        return build_temporal_context(
            current_event_time=current_event_time,
            chat_mode=str(getattr(event, "message_type", "private") or "private"),
            previous_message_time=previous_message_time,
            conversation_last_time=previous_message_time,
            history_event_times=history_event_times,
        )

    def _private_hold_reason(self, event: MessageEvent, user_message: str) -> str:
        text = str(user_message or "").strip()
        normalized = re.sub(r"\s+", "", text).lower()
        if any(token in normalized for token in ["等等", "等下", "稍等", "先别回", "我补充", "我再发", "还没说完"]):
            return "用户明显还在继续补充，私聊先等待下一条消息"
        if self._has_image_input(event) and not normalized:
            return "私聊当前只有图片，先等待用户补充文字或更多上下文"
        return ""

    def _build_private_planning_signals(
        self,
        *,
        event: MessageEvent,
        user_message: str,
        conversation: Conversation,
        temporal_context: Any,
        pending_items: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        text = str(user_message or "").strip()
        normalized = re.sub(r"\s+", "", text).lower()
        pending = list(pending_items or [])
        return {
            "batch_count": max(1, len(pending)),
            "merged_from_multiple_inputs": len(pending) > 1,
            "explicit_hold_signal": bool(self._private_hold_reason(event, user_message)),
            "has_image_without_text": bool(self._has_image_input(event) and not normalized),
            "looks_fragmented": bool(len(normalized) <= 6 and normalized in {"那个", "就是", "然后", "还有", "等会", "继续", "在吗"}),
            "ends_like_incomplete": bool(text.endswith(("...", "..", "。。。", "然后", "就是"))),
            "recent_gap_bucket": str(getattr(temporal_context, "recent_gap_bucket", "unknown") or "unknown"),
            "conversation_turn_count": len(conversation.messages),
        }

    async def _push_private_pending_input(
        self,
        *,
        conversation_key: str,
        event: MessageEvent,
        user_message: str,
    ) -> int:
        async with self.private_batch_lock:
            self.private_batch_versions[conversation_key] += 1
            version = self.private_batch_versions[conversation_key]
            self.private_pending_inputs[conversation_key].append(
                {
                    "version": version,
                    "message_id": str(getattr(event, "message_id", "") or ""),
                    "event_time": normalize_event_time(getattr(event, "time", 0.0)) or time.time(),
                    "inserted_at": time.time(),
                    "text": str(user_message or "").strip(),
                    "has_image": self._has_image_input(event),
                }
            )
            return version

    async def _has_newer_private_input(self, conversation_key: str, version: int) -> bool:
        async with self.private_batch_lock:
            return int(self.private_batch_versions.get(conversation_key, 0) or 0) > int(version)

    async def _consume_private_pending_input(self, conversation_key: str) -> List[Dict[str, Any]]:
        async with self.private_batch_lock:
            items = list(self.private_pending_inputs.pop(conversation_key, []))
            return items

    def _merge_private_pending_input(self, items: List[Dict[str, Any]], fallback_text: str) -> str:
        lines: List[str] = []
        for item in items:
            text = str(item.get("text") or "").strip()
            if text:
                lines.append(text)
        if not lines:
            return str(fallback_text or "").strip()
        merged: List[str] = []
        for text in lines:
            if merged and merged[-1] == text:
                continue
            merged.append(text)
        return "\n".join(merged)

    async def download_images(self, event: MessageEvent) -> List[str]:
        image_segments = event.get_image_segments()
        if not image_segments:
            return []
        base64_images = []
        for seg in image_segments:
            try:
                base64_data = await self.image_client.process_image_segment(seg.data)
                if base64_data:
                    base64_images.append(base64_data)
            except Exception as exc:
                logger.error("处理图片失败：%s", exc, exc_info=True)
        return base64_images

    async def analyze_event_images(
        self,
        event: MessageEvent,
        user_text: str,
        base64_images: Optional[List[str]] = None,
        trace_id: str = "",
    ) -> Dict[str, Any]:
        if not self._has_image_input(event) or not self.vision_enabled():
            return {}

        images = list(base64_images or [])
        if not images:
            images = await self.download_images(event)
        if not images:
            return {
                "per_image_descriptions": [],
                "merged_description": "",
                "vision_success_count": 0,
                "vision_failure_count": self._get_image_count(event),
                "vision_source": "image_download_error",
                "vision_error": "image download failed",
                "vision_available": False,
            }

        try:
            result = await self.vision_client.analyze_images(
                base64_images=images,
                user_text=user_text,
                trace_id=trace_id,
                session_key=get_execution_key(event),
                message_id=getattr(event, "message_id", 0),
            )
        except Exception as exc:
            raise wrap_image_error(exc)
        if self.runtime_metrics:
            self.runtime_metrics.record_vision_request(image_count=len(images), failure_count=result.failure_count)
        if self.emoji_manager:
            await self.emoji_manager.process_detection_result(
                event=event,
                image_segments=event.get_image_segments(),
                base64_images=images,
                analysis_result=result,
            )
        return result.to_prompt_fields()

    def check_command(self, text: str, event: MessageEvent) -> Optional[str]:
        result = self.command_handler.handle(text, event)
        self._sync_active_conversations_metric()
        return result

    def resolve_group_at_user(self, event: MessageEvent, plan: Optional[MessageHandlingPlan]) -> Optional[Any]:
        if event.message_type != MessageType.GROUP.value:
            return None
        reply_context = dict(getattr(plan, "reply_context", None) or {})
        reply_mode = str(reply_context.get("reply_mode") or "").strip().lower()
        if reply_mode == "repeat_echo":
            return None
        if reply_mode == "at":
            return event.user_id
        if reply_mode == "proactive" and self.app_config.group_reply.at_user_when_proactive_reply:
            return event.user_id
        return None

    async def record_group_reply_sent(self, event: MessageEvent, message: str) -> None:
        if event.message_type != MessageType.GROUP.value:
            return
        await self.group_plan_coordinator.record_assistant_reply(event.group_id, message)

    def _get_help_text(self) -> str:
        return self.command_handler.get_help_text()

    def _get_status_text(self) -> str:
        return self.command_handler.get_status_text()

    async def check_rate_limit(self, target_id: str) -> bool:
        async with self.rate_limit_lock:
            interval = self.app_config.bot_behavior.rate_limit_interval
            now = time.time()
            last_time = self.last_send_time.get(target_id, 0.0)
            if now - last_time < interval:
                await asyncio.sleep(interval - (now - last_time))
            self.last_send_time[target_id] = time.time()
            return True

    async def check_group_plan_rate_limit(self, group_id: Optional[int]) -> bool:
        del group_id
        return True

    async def _load_memory_context(
        self,
        event: MessageEvent,
        user_message: str,
        conversation: Conversation,
        plan: Optional[MessageHandlingPlan] = None,
    ) -> tuple[str, str, str, str, str, List[Dict[str, Any]], bool]:
        return await self.reply_pipeline.load_memory_context(
            event=event,
            user_message=user_message,
            conversation=conversation,
            prompt_plan=getattr(plan, "prompt_plan", None) if plan else None,
        )

    def _build_response_system_prompt(
        self,
        person_fact_context: str,
        persistent_memory_context: str,
        session_restore_context: str,
        precise_recall_context: str,
        dynamic_memory_context: str,
        is_first_turn: bool,
        event: Optional[MessageEvent] = None,
        plan: Optional[MessageHandlingPlan] = None,
        recent_history_text: str = "",
        current_message: str = "",
    ) -> str:
        return self.reply_pipeline.build_response_system_prompt(
            event=event,
            person_fact_context=person_fact_context,
            persistent_memory_context=persistent_memory_context,
            session_restore_context=session_restore_context,
            precise_recall_context=precise_recall_context,
            dynamic_memory_context=dynamic_memory_context,
            is_first_turn=is_first_turn,
            plan=plan,
            recent_history_text=recent_history_text,
            current_message=current_message,
        )

    def _build_response_messages(
        self,
        system_prompt: str,
        user_message: str,
        base64_images: List[str],
        related_history_messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        return self.reply_pipeline.build_response_messages(
            system_prompt=system_prompt,
            user_message=user_message,
            base64_images=base64_images,
            related_history_messages=related_history_messages,
        )

    async def get_ai_response(
        self,
        event: MessageEvent,
        plan: Optional[MessageHandlingPlan] = None,
        trace_id: str = "",
    ) -> ReplyResult:
        if plan and isinstance(plan.reply_context, dict):
            direct_reply_text = str(plan.reply_context.get("direct_reply_text") or "").strip()
            if direct_reply_text:
                return ReplyResult(text=direct_reply_text, source=plan.source or "rule")

        reply_context = dict(getattr(plan, "reply_context", None) or {})
        user_message = str(reply_context.get("merged_user_message") or self.extract_user_message(event)).strip()
        command_result = self.check_command(user_message, event)
        if command_result is not None:
            return ReplyResult(text=command_result, source="command")
        context = await self.build_message_context(
            event,
            plan=plan,
            trace_id=trace_id,
            include_memory=True,
        )
        return await self.reply_pipeline.execute(
            event=event,
            user_message=user_message,
            plan=plan,
            context=context,
        )

    async def build_message_context(
        self,
        event: MessageEvent,
        *,
        plan: Optional[MessageHandlingPlan] = None,
        trace_id: str = "",
        include_memory: bool = True,
    ) -> MessageContext:
        reply_context = dict(getattr(plan, "reply_context", None) or {})
        user_message = str(reply_context.get("merged_user_message") or self.extract_user_message(event)).strip()
        execution_key = get_execution_key(event)
        conversation_key = self._get_conversation_key(event)
        conversation = self._get_conversation(conversation_key)
        window_messages = list(reply_context.get("window_messages") or [])
        recent_history_text = self.reply_pipeline._build_recent_history_text(
            event=event,
            conversation=conversation,
            plan=plan,
        )
        temporal_context = self._build_temporal_context(
            event=event,
            conversation=conversation,
            reply_context=reply_context,
        )
        is_first_turn = len(conversation.messages) == 0
        person_fact_context = ""
        persistent_memory_context = ""
        session_restore_context = ""
        precise_recall_context = ""
        dynamic_memory_context = ""
        related_history_messages: List[Dict[str, Any]] = []
        if include_memory:
            (
                person_fact_context,
                persistent_memory_context,
                session_restore_context,
                precise_recall_context,
                dynamic_memory_context,
                related_history_messages,
                is_first_turn,
            ) = await self._load_memory_context(
                event=event,
                user_message=user_message,
                conversation=conversation,
                plan=plan,
            )

        base64_images: List[str] = []
        vision_analysis = self.reply_pipeline._extract_reusable_vision_analysis(event=event, plan=plan)
        if self._has_image_input(event) and not vision_analysis and self.vision_enabled():
            try:
                base64_images = await self.download_images(event)
                if base64_images:
                    vision_analysis = await self.analyze_event_images(
                        event,
                        user_message,
                        base64_images=base64_images,
                        trace_id=trace_id,
                    )
            except ImageProcessingError:
                raise
            except Exception as exc:
                raise wrap_image_error(exc)

        return MessageContext(
            trace_id=trace_id,
            execution_key=execution_key,
            conversation_key=conversation_key,
            user_message=user_message,
            current_sender_label=self._format_identity_label(event.user_id, self._get_sender_display_name(event)),
            is_first_turn=is_first_turn,
            current_event_time=temporal_context.current_event_time,
            previous_message_time=temporal_context.previous_message_time,
            conversation_last_time=temporal_context.conversation_last_time,
            previous_session_time=temporal_context.previous_session_time,
            temporal_context=temporal_context,
            window_messages=window_messages,
            recent_history_text=recent_history_text,
            base64_images=base64_images,
            vision_analysis=vision_analysis,
            person_fact_context=person_fact_context,
            persistent_memory_context=persistent_memory_context,
            session_restore_context=session_restore_context,
            precise_recall_context=precise_recall_context,
            dynamic_memory_context=dynamic_memory_context,
            related_history_messages=related_history_messages,
            reply_context=reply_context,
            direct_reply_text=str(reply_context.get("direct_reply_text") or "").strip(),
            prompt_plan=getattr(plan, "prompt_plan", None) if plan else None,
            conversation=conversation,
        )

    async def plan_emoji_follow_up(
        self,
        event: MessageEvent,
        reply_result: ReplyResult,
        plan: Optional[MessageHandlingPlan] = None,
    ):
        if not self.emoji_reply_service or reply_result.source != "ai":
            return None
        return await self.emoji_reply_service.plan_follow_up(
            event=event,
            user_message=self.extract_user_message(event),
            assistant_reply=reply_result.text,
            reply_context=plan.reply_context if plan else None,
            trace_id=str((plan.reply_context or {}).get("trace_id") or ""),
        )

    async def get_emoji_follow_up_image_path(self, selection) -> Optional[str]:
        if not self.emoji_reply_service or not selection:
            return None
        return await self.emoji_reply_service.get_image_path(selection)

    async def mark_emoji_follow_up_sent(self, event: MessageEvent, selection) -> None:
        if self.emoji_reply_service and selection:
            await self.emoji_reply_service.mark_follow_up_sent(event=event, selection=selection)

    def split_long_message(self, message: str) -> List[str]:
        max_length = self.app_config.bot_behavior.max_message_length
        if len(message) <= max_length:
            return [message]
        parts: List[str] = []
        current_part = ""
        for line in message.split("\n"):
            if len(line) > max_length:
                if current_part:
                    parts.append(current_part)
                    current_part = ""
                for index in range(0, len(line), max_length):
                    parts.append(line[index : index + max_length])
                continue
            if len(current_part) + len(line) + 1 > max_length:
                if current_part:
                    parts.append(current_part)
                current_part = line
            else:
                current_part = f"{current_part}\n{line}".strip("\n") if current_part else line
        if current_part:
            parts.append(current_part)
        return parts

    def get_active_conversation_count(self) -> int:
        count = self.session_manager.count_active()
        self._sync_active_conversations_metric(count)
        return count

    async def close(self) -> None:
        await self.group_plan_coordinator.close()
        await self._close_resource(self.group_reply_planner)
        await self._close_resource(self.emoji_manager)
        await self._close_resource(self.vision_client)
        await self._close_resource(self.image_client)
        await self._close_resource(self.ai_client)
        self._sync_active_conversations_metric()

    async def _close_resource(self, resource: Any) -> None:
        close_method = getattr(resource, "close", None)
        if close_method is None:
            return
        result = close_method()
        if asyncio.iscoroutine(result):
            await result

    def _sync_active_conversations_metric(self, count: Optional[int] = None) -> None:
        if self.runtime_metrics:
            self.runtime_metrics.set_state(active_conversations=self.session_manager.count_active() if count is None else count)

    def _record_background_activity(self) -> None:
        if self.emoji_manager:
            self.emoji_manager.record_activity()

    def _handle_reset_command(self, event: MessageEvent) -> None:
        if not self.memory_manager:
            return
        flush_current = getattr(self.memory_manager, "flush_conversation_session", None)
        if callable(flush_current):
            flush_current(
                user_id=str(event.user_id),
                message_type=event.message_type,
                group_id=str(event.group_id or ""),
            )
