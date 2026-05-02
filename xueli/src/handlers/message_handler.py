from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from src.core.config import (
    AppConfig,
    CharacterGrowthConfig,
    MemoryDisputeConfig,
    PlanningWindowConfig,
    config,
    get_vision_service_status,
    is_group_reply_decision_configured,
    is_vision_service_configured,
)
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
from src.handlers.conversation_context_builder import ConversationContextBuilder
from src.handlers.conversation_plan_coordinator import ConversationPlanCoordinator
from src.handlers.conversation_planner import ConversationPlanner
from src.handlers.conversation_session_manager import ConversationSessionManager
from src.handlers.character_card_service import CharacterCardService
from src.handlers.message_context import MessageContext
from src.handlers.planning_window_service import PlanningWindowService
from src.handlers.conversation_window_models import BufferedWindow, WindowDispatchResult
from src.handlers.reply_pipeline import ReplyPipeline, ReplyResult
from src.handlers.repeat_echo_service import RepeatEchoService
from src.handlers.temporal_context import build_temporal_context, normalize_event_time
from src.handlers.timing_gate_service import TimingGateService
from src.memory.memory_flow_service import MemoryFlowService
from src.memory.storage.fact_evidence_store import FactEvidenceStore
from src.services.ai_client import AIClient, AIResponse
from src.services.image_client import ImageClient
from src.services.vision_client import VisionClient

logger = logging.getLogger(__name__)


class StaleWindowError(Exception):
    """Raised when a buffered window is expired and should be skipped."""
    pass


class MessageHandler:
    """High-level message orchestration layer."""

    def __init__(
        self,
        ai_client: Optional[AIClient] = None,
        image_client: Optional[ImageClient] = None,
        vision_client: Optional[VisionClient] = None,
        memory_manager: Optional[Any] = None,
        conversation_planner: Optional[ConversationPlanner] = None,
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
        self.conversation_planner = conversation_planner or ConversationPlanner(
            app_config=self.app_config,
            model_invocation_router=self.model_invocation_router,
        )

        self.session_manager = ConversationSessionManager(
            conversation_store=getattr(memory_manager, "conversation_store", None) if memory_manager else None
        )
        self.conversation_plan_coordinator = ConversationPlanCoordinator(
            planner=self.conversation_planner,
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
            conversation_store=getattr(memory_manager, "conversation_store", None) if memory_manager else None,
        )
        self.command_handler = CommandHandler(
            self.session_manager,
            status_provider=status_provider,
            runtime_metrics=self.runtime_metrics,
            app_config=self.app_config,
            reset_callback=self._handle_reset_command,
        )
        memory_base_path = self._get_memory_base_path()
        self.fact_evidence_store = FactEvidenceStore(os.path.join(memory_base_path, "_fact_evidence"))
        self.character_card_service = CharacterCardService(
            os.path.join(memory_base_path, "_character_cards"),
            self.app_config.character_growth,
        )
        self.memory_flow_service = MemoryFlowService(
            self.memory_manager,
            dispute_config=self.app_config.memory_dispute,
            evidence_store=self.fact_evidence_store,
            character_card_service=self.character_card_service,
        )
        self.context_builder = ConversationContextBuilder(self)
        self.timing_gate_service = TimingGateService(
            app_config=self.app_config,
            model_invocation_router=self.model_invocation_router,
        )
        self.reply_pipeline = ReplyPipeline(self)
        self.planning_window_service = PlanningWindowService(self, self.app_config.planning_window)

        self.repeat_echo_service = RepeatEchoService(self.app_config, self.runtime_metrics)
        self.repeat_echo_service.set_lock(asyncio.Lock())

        self.last_send_time: Dict[str, float] = {}
        self.rate_limit_lock = asyncio.Lock()
        self.private_batch_window_seconds = float(getattr(self.app_config.planning_window, "private_window_seconds", 1.2) or 0.0)
        self.group_proactive_window_seconds = float(getattr(self.app_config.planning_window, "group_proactive_window_seconds", 0.45) or 0.0)

        self._sync_active_conversations_metric()

    async def initialize(self) -> None:
        if self.emoji_manager:
            await self.emoji_manager.initialize()

    def set_status_provider(self, status_provider: Any) -> None:
        self.command_handler.set_status_provider(status_provider)

    def _create_ai_client(self) -> AIClient:
        logger.debug("[消息处理器] 初始化回复模型")
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

    def _get_memory_base_path(self) -> str:
        if self.memory_manager is not None and hasattr(self.memory_manager, "config"):
            base_path = str(getattr(self.memory_manager.config, "storage_base_path", "") or "").strip()
            if base_path:
                return base_path
        memory_config = getattr(self.app_config, "memory", None)
        base_path = str(getattr(memory_config, "storage_path", "") or "").strip()
        if base_path:
            return base_path
        return str(Path(self.app_config._config_path).parent / "data" / "memories") if hasattr(self.app_config, "_config_path") else "../data/memories"

    def _ensure_extended_services(self) -> None:
        planning_window_config = getattr(self.app_config, "planning_window", PlanningWindowConfig())
        memory_dispute_config = getattr(self.app_config, "memory_dispute", MemoryDisputeConfig())
        character_growth_config = getattr(self.app_config, "character_growth", CharacterGrowthConfig())
        if not hasattr(self, "fact_evidence_store") or self.fact_evidence_store is None:
            self.fact_evidence_store = FactEvidenceStore(os.path.join(self._get_memory_base_path(), "_fact_evidence"))
        if not hasattr(self, "character_card_service") or self.character_card_service is None:
            self.character_card_service = CharacterCardService(
                os.path.join(self._get_memory_base_path(), "_character_cards"),
                character_growth_config,
            )
        if not hasattr(self, "memory_flow_service") or self.memory_flow_service is None:
            self.memory_flow_service = MemoryFlowService(
                self.memory_manager,
                dispute_config=memory_dispute_config,
                evidence_store=self.fact_evidence_store,
                character_card_service=self.character_card_service,
            )
        if not hasattr(self, "planning_window_service") or self.planning_window_service is None:
            self.planning_window_service = PlanningWindowService(self, planning_window_config)
        if not hasattr(self, "private_batch_window_seconds"):
            self.private_batch_window_seconds = float(getattr(planning_window_config, "private_window_seconds", 1.2) or 0.0)
        if not hasattr(self, "group_proactive_window_seconds"):
            self.group_proactive_window_seconds = float(getattr(planning_window_config, "group_proactive_window_seconds", 0.45) or 0.0)

    @property
    def protocol_adapter(self) -> Any:
        """Return the protocol adapter from the host, or a no-op if not available."""
        host = getattr(self, "host", None)
        if host is None:
            return None
        adapter = getattr(host, "protocol_adapter", None)
        if adapter is not None:
            return adapter
        as_adapter = getattr(host, "as_protocol_adapter", None)
        if as_adapter is not None:
            return as_adapter()
        return None

    def _format_memory_prompt_entry(self, content: str, owner_user_id: str = "") -> str:
        return str(content or "").strip()

    def _format_identity_label(self, user_id: Any, display_name: str = "") -> str:
        from src.handlers.identity_utils import format_identity_label

        return format_identity_label(user_id, display_name)

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

    async def _get_or_restore_conversation(self, key: str) -> Conversation:
        """获取会话，新建时同步从数据库恢复历史消息。"""
        conversation = await self.session_manager.get_or_restore(key)
        self._sync_active_conversations_metric()
        return conversation

    async def _clean_expired_conversations(self) -> None:
        self._ensure_extended_services()
        self.session_manager.clean_expired()
        await self._cleanup_private_batch_state()
        if hasattr(self, "repeat_echo_service") and self.repeat_echo_service is not None:
            self.repeat_echo_service.cleanup()
        self._sync_active_conversations_metric()

    async def _cleanup_private_batch_state(self) -> None:
        active_keys = list(getattr(self.session_manager, "_conversations", {}).keys())
        await self.planning_window_service.cleanup(active_keys=active_keys)

    def _format_window_context(self, reply_context: Optional[Dict[str, Any]]) -> str:
        return self.conversation_plan_coordinator.format_window_context(reply_context)

    def _build_rule_plan(
        self,
        action: MessagePlanAction,
        reason: str,
        source: str = "rule",
        reply_context: Optional[Dict[str, Any]] = None,
        event: Optional[MessageEvent] = None,
    ) -> MessageHandlingPlan:
        prompt_plan = None
        if event is not None and action == MessagePlanAction.REPLY:
            prompt_plan = self.conversation_planner.prompt_planner.default_prompt_plan(
                event=event,
                action=action.value,
                context=None,
            )
        return MessageHandlingPlan(
            action=action.value,
            reason=reason,
            source=source,
            reply_context=reply_context,
            prompt_plan=prompt_plan,
        )

    def _planner_available(self) -> bool:
        return is_group_reply_decision_configured(self.app_config)

    async def _check_repeat_echo_trigger(self, event: MessageEvent) -> Optional[str]:
        display_text = self.repeat_echo_service._normalize_text(self.extract_user_message(event))
        if not self.repeat_echo_service.is_candidate(
            event, display_text,
            is_direct_mention=self._is_direct_mention(event),
            has_image=self._has_image_input(event),
        ):
            return None
        return await self.repeat_echo_service.check_trigger(event, display_text)

    async def _plan_message(self, event: MessageEvent, *, trace_id: str = "") -> MessageHandlingPlan:
        self._ensure_extended_services()
        dispatch = await self.planning_window_service.submit_event(event=event, trace_id=trace_id)
        if dispatch.status == "bypassed":
            return await self.conversation_plan_coordinator.plan_message(
                event=event,
                user_message=self.extract_user_message(event),
                trace_id=trace_id,
            )
        if dispatch.status != "dispatch_window" or dispatch.window is None:
            return self._build_rule_plan(
                MessagePlanAction.WAIT,
                "缓冲窗口仍在收集消息，先等待当前批次封窗",
                reply_context={"trace_id": trace_id, "window_reason": dispatch.reason} if trace_id else {"window_reason": dispatch.reason},
                event=event,
            )
        return await self._plan_window(event, dispatch.window, trace_id=trace_id)

    async def _plan_private_message(self, event: MessageEvent, *, trace_id: str = "") -> MessageHandlingPlan:
        self._ensure_extended_services()
        dispatch = await self.planning_window_service.submit_private_event(event=event, trace_id=trace_id)
        if dispatch.status == "dropped":
            return self._build_rule_plan(MessagePlanAction.IGNORE, "私聊缓冲窗口排队超时，本轮直接丢弃")
        if dispatch.status != "dispatch_window" or dispatch.window is None:
            return MessageHandlingPlan(
                action=MessagePlanAction.WAIT.value,
                reason="私聊缓冲窗口仍在收集消息，先等待当前批次封窗",
                source="window_buffer",
                reply_context={"trace_id": trace_id, "window_reason": dispatch.reason} if trace_id else {"window_reason": dispatch.reason},
            )
        return await self._plan_private_window(event, dispatch.window, trace_id=trace_id)

    async def _plan_private_window(
        self,
        event: MessageEvent,
        window: BufferedWindow,
        *,
        trace_id: str = "",
    ) -> MessageHandlingPlan:
        dispatch_event = window.latest_event if isinstance(window.latest_event, MessageEvent) else event
        conversation_key = self._get_conversation_key(event)
        conversation = self._get_conversation(conversation_key)
        if not conversation.messages:
            await self.session_manager.restore(conversation, conversation_key)
        merged_user_message = str(window.merged_user_message or self.extract_user_message(dispatch_event)).strip()
        temporal_context = self._build_temporal_context(
            event=dispatch_event,
            conversation=conversation,
            reply_context={"window_messages": list(window.messages or [])},
        )
        context = MessageContext(
            trace_id=trace_id,
            execution_key=get_execution_key(dispatch_event),
            conversation_key=conversation_key,
            user_message=merged_user_message,
            current_sender_label=self._format_identity_label(dispatch_event.user_id, self._get_sender_display_name(dispatch_event)),
            is_first_turn=len(conversation.messages) == 0,
            current_event_time=temporal_context.current_event_time,
            previous_message_time=temporal_context.previous_message_time,
            conversation_last_time=temporal_context.conversation_last_time,
            previous_session_time=temporal_context.previous_session_time,
            temporal_context=temporal_context,
            recent_history_text=self.reply_pipeline._build_recent_history_text(
                event=dispatch_event,
                conversation=conversation,
                plan=None,
            ),
            planning_signals=dict(window.planning_signals or {}),
            conversation=conversation,
        )
        pending_items = list(window.messages or [])
        # 从 conversation.messages 提取 image_description，注入到 pending_items
        msg_id_to_image_desc: Dict[str, str] = {}
        for msg in conversation.messages:
            mid = str(msg.get("message_id") or "").strip()
            if mid:
                desc = str(msg.get("image_description") or "").strip()
                if desc:
                    msg_id_to_image_desc[mid] = desc
        for item in pending_items:
            mid = str(item.get("message_id") or "").strip()
            if mid and mid in msg_id_to_image_desc:
                item["image_description"] = msg_id_to_image_desc[mid]
        context.user_message = merged_user_message
        context.window_messages = pending_items
        context.window_reason = str(window.window_reason or "dispatched")
        context.planning_signals = dict(window.planning_signals or {})
        plan = await self.conversation_planner.plan(
            event=dispatch_event,
            user_message=merged_user_message,
            recent_messages=[],
            context=context,
        )
        reply_context = dict(plan.reply_context or {})
        if trace_id:
            reply_context["trace_id"] = trace_id
        reply_context.update(self._build_window_reply_context(window=window, event=dispatch_event))
        if plan.should_reply:
            reply_context.setdefault("reply_mode", "private")
            reply_context["merged_user_message"] = merged_user_message
            reply_goal = str(getattr(getattr(plan, "prompt_plan", None), "reply_goal", "") or "").strip()
            if reply_goal:
                reply_context["reply_goal"] = reply_goal
            if context.planning_signals:
                reply_context["planning_signals"] = dict(context.planning_signals)
        return MessageHandlingPlan(
            action=plan.action,
            reason=plan.reason,
            source=plan.source,
            raw_decision=plan.raw_decision,
            reply_context=reply_context,
            prompt_plan=plan.prompt_plan,
            reply_reference=plan.reply_reference,
        )

    async def _plan_window(
        self,
        event: MessageEvent,
        window: BufferedWindow,
        *,
        trace_id: str = "",
    ) -> MessageHandlingPlan:
        dispatch_event = window.latest_event if isinstance(window.latest_event, MessageEvent) else event
        plan = await self.conversation_plan_coordinator.plan_buffered_window(
            event=dispatch_event,
            window=window,
            trace_id=trace_id,
        )
        reply_context = dict(plan.reply_context or {})
        reply_context.update(self._build_window_reply_context(window=window, event=dispatch_event))
        return MessageHandlingPlan(
            action=plan.action,
            reason=plan.reason,
            source=plan.source,
            raw_decision=plan.raw_decision,
            reply_context=reply_context,
            prompt_plan=plan.prompt_plan,
            reply_reference=plan.reply_reference,
        )

    async def _build_at_reply_context(self, event: MessageEvent, *, trace_id: str = "") -> Dict[str, Any]:
        return await self.conversation_plan_coordinator.build_direct_reply_context(
            event=event,
            user_message=self.extract_user_message(event),
            reply_mode="at",
            planner_mode="direct_at",
            trace_id=trace_id,
        )

    async def plan_message(self, event: MessageEvent, *, trace_id: str = "") -> MessageHandlingPlan:
        self._ensure_extended_services()
        self._record_background_activity()
        await self._clean_expired_conversations()
        if hasattr(self, "character_card_service") and self.character_card_service is not None:
            self.character_card_service.record_explicit_feedback(str(event.user_id), self.extract_user_message(event))

        if event.user_id == event.self_id:
            return self._build_rule_plan(MessagePlanAction.IGNORE, "机器人自己的消息，跳过处理")
        if self.emoji_manager is not None:
            await self.emoji_manager.capture_native_emoji_references(event=event)
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
                event=event,
            )

        planner_available = self._planner_available()
        only_at_mode = self.app_config.group_reply.only_reply_when_at or not planner_available

        if only_at_mode:
            if self._is_direct_mention(event):
                reply_context = await self._build_at_reply_context(event, trace_id=trace_id)
                if planner_available and self.app_config.group_reply.only_reply_when_at:
                    return self._build_rule_plan(
                        MessagePlanAction.REPLY,
                        "群聊仅在被 @ 时回复，当前消息命中 @",
                        reply_context=reply_context,
                        event=event,
                    )
                return self._build_rule_plan(
                    MessagePlanAction.REPLY,
                    "未配置群聊判断模型，当前仅在被 @ 时回复",
                    reply_context=reply_context,
                    event=event,
                )
            if planner_available and self.app_config.group_reply.only_reply_when_at:
                return self._build_rule_plan(MessagePlanAction.IGNORE, "群聊仅在被 @ 时回复，跳过未 @ 消息")
            return self._build_rule_plan(MessagePlanAction.IGNORE, "未配置群聊判断模型，当前仅在被 @ 时回复")

        if self._is_direct_mention(event):
            reply_context = await self._build_at_reply_context(event, trace_id=trace_id)
            return self._build_rule_plan(
                MessagePlanAction.REPLY,
                "群聊消息显式 @ 了助手，直接回复",
                reply_context=reply_context,
                event=event,
            )

        return await self._plan_message(event, trace_id=trace_id)

    def _build_window_reply_context(self, *, window: BufferedWindow, event: MessageEvent) -> Dict[str, Any]:
        sanitized_window_messages = []
        for item in list(window.messages or []):
            copied = dict(item)
            copied.pop("_event", None)
            sanitized_window_messages.append(copied)
        return {
            "window_reason": str(window.window_reason or "dispatched"),
            "window_messages": sanitized_window_messages,
            "window_seq": int(window.seq or 0),
            "window_conversation_key": str(window.conversation_key or ""),
            "window_chat_mode": str(window.chat_mode or getattr(event, "message_type", "private") or "private"),
            "window_event": event,
            "merged_user_message": str(window.merged_user_message or ""),
            "planning_signals": dict(window.planning_signals or {}),
            "expires_at": float(window.expires_at or 0.0),
        }

    def _extract_dispatched_window_info(self, plan: Optional[MessageHandlingPlan]) -> tuple[str, int]:
        reply_context = dict(getattr(plan, "reply_context", None) or {})
        conversation_key = str(reply_context.get("window_conversation_key") or "").strip()
        seq = int(reply_context.get("window_seq", 0) or 0)
        return conversation_key, seq

    async def complete_window_dispatch(self, plan: Optional[MessageHandlingPlan]) -> WindowDispatchResult:
        conversation_key, seq = self._extract_dispatched_window_info(plan)
        if not conversation_key or seq <= 0:
            return WindowDispatchResult(status="accepted_only", reason="no_window_dispatch")
        return await self.planning_window_service.mark_window_complete(conversation_key, seq)

    def is_window_stale(self, window: BufferedWindow, *, now: Optional[float] = None) -> bool:
        """检查窗口是否已过期（stale）。"""
        if now is None:
            now = time.time()
        expires_at = float(window.expires_at or 0.0)
        if expires_at > 0 and expires_at <= now:
            return True
        return False

    async def plan_dispatched_window(self, window: BufferedWindow, *, trace_id: str = "") -> tuple[MessageEvent, MessageHandlingPlan]:
        # stale 检查：窗口已过期则不再处理
        if self.is_window_stale(window):
            logger.warning("[消息处理器] 窗口已过期，跳过处理")
            raise StaleWindowError(f"window {window.seq} is stale")
        dispatch_event = window.latest_event if isinstance(window.latest_event, MessageEvent) else None
        if dispatch_event is None:
            raise ValueError("buffered window is missing latest_event")
        if str(window.chat_mode or "").strip().lower() == MessageType.GROUP.value:
            plan = await self._plan_window(dispatch_event, window, trace_id=trace_id)
        else:
            plan = await self._plan_private_window(dispatch_event, window, trace_id=trace_id)
        return dispatch_event, plan

    def should_process(self, event: MessageEvent) -> bool:
        if event.user_id == event.self_id:
            return False
        if event.message_type == MessageType.PRIVATE.value:
            return True
        if event.message_type == MessageType.GROUP.value:
            only_at_mode = self.app_config.group_reply.only_reply_when_at or not self._planner_available()
            return self._is_direct_mention(event) if only_at_mode else True
        return False

    def extract_user_message(self, event: MessageEvent) -> str:
        text = self._get_event_text(event)
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
        previous_session_time = 0.0
        history_event_times: List[float] = []

        if window_messages:
            history_event_times = [float(normalize_event_time(item.get("event_time", 0.0)) or 0.0) for item in window_messages]
            previous_items = [item for item in window_messages if not bool(item.get("is_latest"))]
            if previous_items:
                previous_message_time = float(normalize_event_time(previous_items[-1].get("event_time", 0.0)) or 0.0)

        if previous_message_time <= 0 and conversation.messages:
            # Use the second-to-last message as the actual "previous" message.
            # conversation.messages[-1] is the current message (just added, its time == current_event_time),
            # so last_update == current_event_time — we must skip it and get the real previous timestamp.
            msgs = conversation.messages
            if len(msgs) >= 2:
                previous_message_time = float(msgs[-2].get("timestamp") or 0.0)
            else:
                previous_message_time = float(conversation.last_update or 0.0)

        if getattr(conversation, "restored_session_pending", False):
            previous_session_time = float(getattr(conversation, "restored_previous_session_time", 0.0) or 0.0)
            # NOTE: previous_message_time already correctly reflects the last historical
            # message's event time from conversation.messages[-2]. Do NOT overwrite it
            # with session-level timestamps (restored_last_message_time / closed_at).
            # previous_session_time is only used for session_gap_bucket calculation.

        return build_temporal_context(
            current_event_time=current_event_time,
            chat_mode=str(getattr(event, "message_type", "private") or "private"),
            previous_message_time=previous_message_time,
            conversation_last_time=previous_message_time,
            previous_session_time=previous_session_time,
            history_event_times=history_event_times,
        )

    def get_character_card_snapshot(self, user_id: str):
        if not hasattr(self, "character_card_service") or self.character_card_service is None:
            return None
        return self.character_card_service.get_snapshot(str(user_id))

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
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[消息处理器] 处理图片失败")
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
        except asyncio.CancelledError:
            raise
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

    def resolve_at_user(self, event: MessageEvent, plan: Optional[MessageHandlingPlan]) -> Optional[Any]:
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

    async def record_reply_sent(self, event: MessageEvent, message: str) -> None:
        if event.message_type != MessageType.GROUP.value:
            return
        group_id = event.raw_data.get("group_id", "")
        await self.conversation_plan_coordinator.record_assistant_reply(group_id, message)

    def _get_help_text(self) -> str:
        return self.command_handler.get_help_text()

    def _get_status_text(self) -> str:
        return self.command_handler.get_status_text()

    async def check_rate_limit(self, target_id: str) -> bool:
        interval = self.app_config.bot_behavior.rate_limit_interval
        now = time.time()
        async with self.rate_limit_lock:
            last_time = self.last_send_time.get(target_id, 0.0)
            sleep_time = interval - (now - last_time)
        if sleep_time > 0:
            await asyncio.sleep(sleep_time)
        async with self.rate_limit_lock:
            self.last_send_time[target_id] = time.time()
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



    async def get_ai_response(
        self,
        event: MessageEvent,
        plan: Optional[MessageHandlingPlan] = None,
        trace_id: str = "",
    ) -> ReplyResult:
        # stale 检查：回复生成前检查窗口是否已过期
        reply_context = dict(getattr(plan, "reply_context", None) or {})
        window_expires_at = float(reply_context.get("expires_at", 0.0) or 0.0)
        if window_expires_at > 0 and time.time() > window_expires_at:
            logger.warning("[消息处理器] 窗口已过期")
            return ReplyResult(text="", segments=[], source="stale_suppressed")
        if plan and isinstance(plan.reply_context, dict):
            direct_reply_text = str(plan.reply_context.get("direct_reply_text") or "").strip()
            if direct_reply_text:
                return ReplyResult(text=direct_reply_text, segments=[direct_reply_text], source=plan.source or "rule")

        reply_context = dict(getattr(plan, "reply_context", None) or {})
        user_message = str(reply_context.get("merged_user_message") or self.extract_user_message(event)).strip()
        command_result = self.check_command(user_message, event)
        if command_result is not None:
            return ReplyResult(text=command_result, segments=[command_result], source="command")
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
        if not hasattr(self, "context_builder") or self.context_builder is None:
            self.context_builder = ConversationContextBuilder(self)
        context = await self.context_builder.build(
            event,
            plan=plan,
            trace_id=trace_id,
            include_memory=include_memory,
        )
        if not context.planning_signals:
            reply_context = dict(getattr(plan, "reply_context", None) or {})
            if isinstance(reply_context.get("planning_signals"), dict):
                context.planning_signals = dict(reply_context.get("planning_signals") or {})
        return context

    async def apply_timing_gate(
        self,
        event: MessageEvent,
        *,
        plan: MessageHandlingPlan,
        trace_id: str = "",
    ) -> MessageHandlingPlan:
        if not plan.should_reply:
            return plan
        if not hasattr(self, "timing_gate_service") or self.timing_gate_service is None:
            self.timing_gate_service = TimingGateService(
                app_config=self.app_config,
                model_invocation_router=self.model_invocation_router,
            )
        reply_context = dict(getattr(plan, "reply_context", None) or {})
        if str(reply_context.get("direct_reply_text") or "").strip():
            return plan
        if plan.source in {"command", "repeat_echo"}:
            return plan
        context = await self.build_message_context(
            event,
            plan=plan,
            trace_id=trace_id,
            include_memory=False,
        )
        decision = await self.timing_gate_service.decide(event=event, plan=plan, context=context)
        reply_context["timing_decision"] = decision.decision
        reply_context["timing_reason"] = decision.reason
        if decision.decision == "continue":
            return MessageHandlingPlan(
                action=plan.action,
                reason=plan.reason,
                source=plan.source,
                raw_decision=plan.raw_decision,
                reply_context=reply_context,
                prompt_plan=plan.prompt_plan,
                reply_reference=plan.reply_reference,
            )
        mapped_action = MessagePlanAction.WAIT.value if decision.decision == "wait" else MessagePlanAction.IGNORE.value
        return MessageHandlingPlan(
            action=mapped_action,
            reason=decision.reason or plan.reason,
            source=decision.source,
            raw_decision=decision.raw_decision,
            reply_context=reply_context,
            prompt_plan=plan.prompt_plan,
            reply_reference=plan.reply_reference,
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

    def build_emoji_follow_up_action(self, selection, session):
        if not self.emoji_reply_service or not selection:
            return None
        return self.emoji_reply_service.build_follow_up_action(
            selection=selection,
            session=session,
        )

    async def mark_emoji_follow_up_sent(self, event: MessageEvent, selection) -> None:
        if self.emoji_reply_service and selection:
            await self.emoji_reply_service.mark_follow_up_sent(event=event, selection=selection)

    def split_by_sentence(self, message: str) -> List[str]:
        """按句末标点（。！？）切分为语义独立的短句。"""
        enabled = getattr(self.app_config.bot_behavior, "sentence_split_enabled", True)
        if not enabled:
            return [message]
        if not message or len(message) < 5:
            return [message]
        parts = re.split(r"(?<=[。！？])", message)
        if len(parts) < 2:
            return [message]
        filtered = [p.strip() for p in parts if len(p.strip()) >= 2]
        if len(filtered) < 2:
            return [message]
        return filtered

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
        await self.conversation_plan_coordinator.close()
        await self._close_resource(self.planning_window_service)
        await self._close_resource(self.conversation_planner)
        await self._close_resource(self.timing_gate_service)
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
                group_id=str(event.raw_data.get("group_id", "") or ""),
            )
