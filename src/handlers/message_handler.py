from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict, deque
from typing import Any, Deque, Dict, List, Optional

from src.core.config import AppConfig, config, get_vision_service_status, is_group_reply_decision_configured, is_vision_service_configured
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
from src.handlers.conversation_session_manager import ConversationSessionManager
from src.handlers.group_plan_coordinator import GroupPlanCoordinator
from src.handlers.group_reply_planner import GroupReplyPlanner
from src.handlers.reply_pipeline import ReplyPipeline, ReplyResult
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
        group_reply_planner: Optional[GroupReplyPlanner] = None,
        *,
        runtime_metrics: Optional[RuntimeMetrics] = None,
        status_provider: Optional[Any] = None,
        app_config: Optional[AppConfig] = None,
    ) -> None:
        self.app_config = app_config or config.app
        self.runtime_metrics = runtime_metrics
        self.memory_manager = memory_manager
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
        )
        self.group_reply_planner = group_reply_planner or GroupReplyPlanner(app_config=self.app_config)

        self.session_manager = ConversationSessionManager()
        self.group_plan_coordinator = GroupPlanCoordinator(
            planner=self.group_reply_planner,
            session_manager=self.session_manager,
            runtime_metrics=self.runtime_metrics,
            group_reply_config=self.app_config.group_reply,
            image_analyzer=self.analyze_event_images if self.vision_enabled() else None,
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
        self._group_repeat_history: Dict[int, Deque[Dict[str, Any]]] = defaultdict(deque)
        self._group_repeat_cooldowns: Dict[tuple[int, str], float] = {}

        self._sync_active_conversations_metric()

    async def initialize(self) -> None:
        if self.emoji_manager:
            await self.emoji_manager.initialize()

    def set_status_provider(self, status_provider: Any) -> None:
        self.command_handler.set_status_provider(status_provider)

    def _create_ai_client(self) -> AIClient:
        logger.info("[reply] initialize reply model: model=%s", self.app_config.ai_service.model)
        return AIClient(log_label="reply", app_config=self.app_config)

    def _create_vision_client(self) -> VisionClient:
        return VisionClient(app_config=self.app_config)

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
        return "【助手身份】\n" + self._build_assistant_identity_text()

    def vision_enabled(self) -> bool:
        if not self.vision_client:
            return False
        available = getattr(self.vision_client, "is_available", None)
        if callable(available):
            return bool(available())
        return bool(self.app_config.vision_service.enabled and is_vision_service_configured(self.app_config))

    def vision_status(self) -> str:
        if self.vision_client:
            status = getattr(self.vision_client, "status", None)
            if callable(status):
                return str(status())
        return get_vision_service_status(self.app_config)

    def _build_system_prompt(self) -> str:
        parts = []
        if self.app_config.personality.content:
            parts.append(f"【人格】\n{self.app_config.personality.content}")
        if self.app_config.dialogue_style.content:
            parts.append(f"【对话风格】\n{self.app_config.dialogue_style.content}")
        if self.app_config.behavior.content:
            parts.append(f"【行为】\n{self.app_config.behavior.content}")
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
    ) -> AIResponse:
        return await self.reply_pipeline.chat_with_tools(messages=messages, user_id=user_id, temperature=temperature)

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

    def _format_system_prompt_log_with_history(
        self,
        event: MessageEvent,
        messages: List[Dict[str, Any]],
        related_history_messages: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        system_message = next((message for message in messages if message.get("role") == "system"), None)
        system_content = self._format_prompt_message_content(system_message.get("content", "")) if system_message else ""
        lines = [
            "[SYSTEM PROMPT]",
            f"用户: {event.user_id}",
            f"会话: {self._get_conversation_key(event)}",
            "",
            system_content if system_content else "[空]",
        ]
        history_messages = related_history_messages or []
        if history_messages:
            lines.extend(["", "--- 关联历史 ---"])
            for index in range(0, len(history_messages), 2):
                first = history_messages[index]
                second = history_messages[index + 1] if index + 1 < len(history_messages) else None
                if first.get("role") == "user":
                    lines.append(f"{int(index / 2) + 1}. 用户: {self._format_prompt_message_content(first.get('content', ''))}")
                    if second and second.get("role") == "assistant":
                        lines.append(f"   助手: {self._format_prompt_message_content(second.get('content', ''))}")
        return "\n".join(lines).rstrip()
    def _get_conversation_key(self, event: MessageEvent) -> str:
        return self.session_manager.get_key(event)

    def _get_conversation(self, key: str) -> Conversation:
        conversation = self.session_manager.get(key)
        self._sync_active_conversations_metric()
        return conversation

    def _clean_expired_conversations(self) -> None:
        self.session_manager.clean_expired()
        self._sync_active_conversations_metric()

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
        if event.is_at(event.self_id) or event.has_image():
            return False
        normalized = self._normalize_repeat_echo_text(text)
        if not normalized or normalized.startswith("/"):
            return False
        return 2 <= len(normalized) <= 20

    def _check_repeat_echo_trigger(self, event: MessageEvent) -> Optional[str]:
        display_text = self._normalize_repeat_echo_text(self.extract_user_message(event))
        if not self._is_repeat_echo_candidate(event, display_text):
            return None

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
        logger.info("[group_repeat] triggered: group=%s text=%s unique_users=%s", group_id, display_text, len(unique_users))
        return display_text

    async def _plan_group_message(self, event: MessageEvent) -> MessageHandlingPlan:
        return await self.group_plan_coordinator.plan_group_message(
            event=event,
            user_message=self.extract_user_message(event),
        )

    async def plan_message(self, event: MessageEvent) -> MessageHandlingPlan:
        self._record_background_activity()
        self._clean_expired_conversations()

        if event.user_id == event.self_id:
            return self._build_rule_plan(MessagePlanAction.IGNORE, "机器人自己的消息，跳过处理")
        if event.message_type == MessageType.PRIVATE.value:
            return self._build_rule_plan(MessagePlanAction.REPLY, "私聊消息默认直接回复")
        if event.message_type != MessageType.GROUP.value:
            return self._build_rule_plan(MessagePlanAction.IGNORE, "当前仅处理私聊和群聊消息")

        repeat_echo_text = self._check_repeat_echo_trigger(event)
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
            if event.is_at(event.self_id):
                if planner_available and self.app_config.group_reply.only_reply_when_at:
                    return self._build_rule_plan(
                        MessagePlanAction.REPLY,
                        "群聊仅在被 @ 时回复，当前消息命中 @",
                        reply_context={"reply_mode": "at"},
                    )
                return self._build_rule_plan(
                    MessagePlanAction.REPLY,
                    "未配置群聊判断模型，当前仅在被 @ 时回复",
                    reply_context={"reply_mode": "at"},
                )
            if planner_available and self.app_config.group_reply.only_reply_when_at:
                return self._build_rule_plan(MessagePlanAction.IGNORE, "群聊仅在被 @ 时回复，跳过未 @ 消息")
            return self._build_rule_plan(MessagePlanAction.IGNORE, "未配置群聊判断模型，当前仅在被 @ 时回复")

        if event.is_at(event.self_id):
            return self._build_rule_plan(
                MessagePlanAction.REPLY,
                "群聊消息显式 @ 了助手，直接回复",
                reply_context={"reply_mode": "at"},
            )

        return await self._plan_group_message(event)

    def should_process(self, event: MessageEvent) -> bool:
        if event.user_id == event.self_id:
            return False
        if event.message_type == MessageType.PRIVATE.value:
            return True
        if event.message_type == MessageType.GROUP.value:
            only_at_mode = self.app_config.group_reply.only_reply_when_at or not self._group_planner_available()
            return event.is_at(event.self_id) if only_at_mode else True
        return False

    def extract_user_message(self, event: MessageEvent) -> str:
        text = event.extract_text()
        if event.message_type == MessageType.GROUP.value:
            text = self.at_pattern.sub("", text)
            text = text.replace(f"@{self._get_assistant_name()}", "")
        return text.strip()

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
                logger.error("处理图片失败: %s", exc, exc_info=True)
        return base64_images

    async def analyze_event_images(
        self,
        event: MessageEvent,
        user_text: str,
        base64_images: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        if not event.has_image() or not self.vision_enabled():
            return {}

        images = list(base64_images or [])
        if not images:
            images = await self.download_images(event)
        if not images:
            return {
                "per_image_descriptions": [],
                "merged_description": "",
                "vision_success_count": 0,
                "vision_failure_count": len(event.get_image_segments()),
                "vision_source": "image_download_error",
                "vision_error": "image download failed",
                "vision_available": False,
            }

        result = await self.vision_client.analyze_images(base64_images=images, user_text=user_text)
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

    def resolve_group_at_user(self, event: MessageEvent, plan: Optional[MessageHandlingPlan]) -> Optional[int]:
        if event.message_type != MessageType.GROUP.value:
            return None
        reply_context = dict(getattr(plan, "reply_context", None) or {})
        reply_mode = str(reply_context.get("reply_mode") or "").strip().lower()
        if reply_mode == "repeat_echo":
            return None
        if reply_mode == "at":
            return int(event.user_id)
        if reply_mode == "proactive" and self.app_config.group_reply.at_user_when_proactive_reply:
            return int(event.user_id)
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
    ) -> tuple[str, List[Dict[str, Any]], bool]:
        return await self.reply_pipeline.load_memory_context(
            event=event,
            user_message=user_message,
            conversation=conversation,
        )

    def _build_response_system_prompt(
        self,
        memory_context: str,
        is_first_turn: bool,
        event: Optional[MessageEvent] = None,
        plan: Optional[MessageHandlingPlan] = None,
    ) -> str:
        return self.reply_pipeline.build_response_system_prompt(
            event=event,
            memory_context=memory_context,
            is_first_turn=is_first_turn,
            plan=plan,
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
    ) -> ReplyResult:
        if plan and isinstance(plan.reply_context, dict):
            direct_reply_text = str(plan.reply_context.get("direct_reply_text") or "").strip()
            if direct_reply_text:
                return ReplyResult(text=direct_reply_text, source=plan.source or "rule")

        user_message = self.extract_user_message(event)
        command_result = self.check_command(user_message, event)
        if command_result is not None:
            return ReplyResult(text=command_result, source="command")
        return await self.reply_pipeline.execute(event=event, user_message=user_message, plan=plan)

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
