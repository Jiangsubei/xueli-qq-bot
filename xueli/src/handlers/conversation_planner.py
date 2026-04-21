from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional

from src.core.config import AppConfig, config, is_group_reply_decision_configured
from src.core.message_trace import format_trace_log, get_execution_key
from src.core.model_invocation_router import ModelInvocationRouter, ModelInvocationType
from src.core.models import MessageEvent, MessageHandlingPlan, MessagePlanAction
from src.core.platform_normalizers import event_mentions_account
from src.core.prompt_templates import PromptTemplateLoader
from src.handlers.message_context import MessageContext
from src.handlers.prompt_planner import PromptPlanner
from src.services.ai_client import AIAPIError, AIClient

logger = logging.getLogger(__name__)


class ConversationPlanner:
    """Use a dedicated model to plan reply/wait/ignore for private and group chats."""

    def __init__(
        self,
        ai_client: Optional[AIClient] = None,
        *,
        app_config: Optional[AppConfig] = None,
        model_invocation_router: Optional[ModelInvocationRouter] = None,
    ) -> None:
        self.app_config = app_config or config.app
        self.ai_client = ai_client or self._create_ai_client()
        self._owns_ai_client = ai_client is None
        self.model_invocation_router = model_invocation_router
        self.prompt_planner = PromptPlanner()
        self.template_loader = PromptTemplateLoader()

    def _create_ai_client(self) -> Optional[AIClient]:
        if not is_group_reply_decision_configured(self.app_config):
            logger.debug("未配置会话规划模型，统一规划器已禁用")
            return None

        decision = self.app_config.group_reply_decision
        client_config = {
            "api_base": decision.api_base,
            "api_key": decision.api_key,
            "model": decision.model,
            "extra_params": dict(decision.extra_params or {}),
            "extra_headers": dict(decision.extra_headers or {}),
            "response_path": decision.response_path or "choices.0.message.content",
        }
        logger.debug("初始化会话规划模型：模型=%s", client_config.get("model"))
        return AIClient(log_label="planner", app_config=self.app_config, **client_config)

    def _assistant_name(self) -> str:
        return self.app_config.assistant_profile.name.strip() or config.get_assistant_name()

    def _assistant_alias(self) -> str:
        return self.app_config.assistant_profile.alias.strip()

    def _assistant_names(self) -> List[str]:
        names: List[str] = []
        for value in (self._assistant_name(), self._assistant_alias()):
            if value and value not in names:
                names.append(value)
        return names

    def _format_identity_label(self, user_id: Any, display_name: str = "") -> str:
        identifier = str(user_id or "").strip() or "unknown"
        name = str(display_name or "").strip()
        if name and name != identifier:
            return f"{identifier}（{name}）"
        return identifier

    def _build_assistant_identity_text(self) -> str:
        assistant_name = self._assistant_name()
        assistant_alias = self._assistant_alias()
        if assistant_alias:
            return f"助手“{assistant_name}”，别名“{assistant_alias}”"
        return f"助手“{assistant_name}”"

    def _describe_message_shape(self, message_shape: str) -> str:
        mapping = {
            "text_only": "纯文本消息",
            "image_only": "纯图片消息",
            "text_with_image": "图文消息",
        }
        return mapping.get((message_shape or "").strip(), "未知消息")

    def _decision_output_schema(self) -> str:
        return self.prompt_planner.decision_output_schema()

    def _build_system_prompt(self, chat_mode: str) -> str:
        chat_mode_label = "私聊" if chat_mode == "private" else "群聊"
        scene_guidance = (
            "私聊里请更关注：这句是否完整、是否像直接提问、是否像情绪暴露、是否像隔了一段时间重新接话。"
            "如果只是首次开场或自然打招呼，不要默认写成强连续续聊。"
            if chat_mode == "private"
            else "群聊里请更关注：当前消息是否自然指向助手、现在接话会不会打断别人、是否只是轻轻接一句就够。"
        )
        return self.template_loader.render(
            "planner.prompt",
            chat_mode_label=chat_mode_label,
            scene_guidance=scene_guidance,
            decision_output_schema=self._decision_output_schema(),
        )

    def _format_window_messages(self, window_messages: List[Dict[str, Any]]) -> str:
        if not window_messages:
            return "无"
        lines = []
        for item in window_messages:
            role = str(item.get("speaker_role") or "user").strip().lower()
            speaker = (
                f"助手 {str(item.get('speaker_name') or self._assistant_name()).strip() or self._assistant_name()}"
                if role == "assistant"
                else f"用户 {self._format_identity_label(item.get('user_id'), str(item.get('speaker_name') or ''))}"
            )
            text = self._window_display_text(item)
            lines.append(f"{speaker}: {text}")

            merged_description = str(item.get("merged_description") or "").strip()
            if merged_description:
                lines.append(f"图片摘要: {merged_description}")
            for image_index, description in enumerate(item.get("per_image_descriptions") or [], 1):
                lines.append(f"第{image_index}张图片: {description}")
            if item.get("has_image") and not item.get("vision_available"):
                failure_count = int(item.get("vision_failure_count", 0) or 0)
                if failure_count > 0:
                    lines.append(f"图片理解失败数: {failure_count}")
                vision_error = str(item.get("vision_error") or "").strip()
                if vision_error:
                    lines.append(f"图片理解错误: {vision_error}")
        return "\n".join(lines)

    def _window_display_text(self, item: Dict[str, Any]) -> str:
        text = str(item.get("display_text") or item.get("text") or item.get("raw_text") or "").strip()
        raw_image_count = int(item.get("raw_image_count", item.get("image_count", 0)) or 0)
        has_image_indicator = bool(item.get("raw_has_image")) or raw_image_count > 0 or bool(item.get("image_description"))
        image_desc = str(item.get("image_description") or item.get("merged_description") or "").strip()
        # 如果有图片描述，即使 text 非空也追加描述
        if has_image_indicator and image_desc:
            if text and text != "[空]":
                return f"{text}[图片描述：{image_desc}]"
            return f"[图片描述：{image_desc}]"
        if text and text != "[空]":
            return text
        if has_image_indicator:
            return "[图片]" if raw_image_count <= 1 else f"[图片 x{raw_image_count}]"
        return text or "[空]"

    def _build_recent_history_text(self, window_messages: List[Dict[str, Any]]) -> str:
        history_items = [item for item in window_messages if not bool(item.get("is_latest"))]
        if not history_items:
            return "在当前这条群消息之前，群里刚刚聊过的内容暂时还没有。"
        return (
            "在当前这条群消息之前，群里刚刚聊了这些内容：\n"
            + self._format_window_messages(history_items)
        )

    def _latest_window_message(self, window_messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        return next(
            (item for item in reversed(window_messages) if item.get("is_latest")),
            window_messages[-1] if window_messages else {},
        )

    def _resolve_prompt_raw_text(self, latest_message: Dict[str, Any], event: MessageEvent, user_message: str) -> str:
        if str(latest_message.get("raw_text") or "").strip():
            return str(latest_message.get("raw_text") or "").strip()
        if str(latest_message.get("display_text") or "").strip():
            return str(latest_message.get("display_text") or "").strip()
        if str(user_message or "").strip():
            return str(user_message or "").strip()
        return str(event.extract_text().strip() or "[空]")

    def _resolve_prompt_clean_text(self, latest_message: Dict[str, Any], event: MessageEvent, user_message: str) -> str:
        if str(latest_message.get("text_content") or "").strip():
            return str(latest_message.get("text_content") or "").strip()
        if str(user_message or "").strip():
            return str(user_message or "").strip()
        return str(event.extract_text().strip() or "[空]")

    def _resolve_prompt_has_image(self, latest_message: Dict[str, Any], event: MessageEvent) -> bool:
        if "has_image" in latest_message:
            return bool(latest_message.get("has_image"))
        if "raw_has_image" in latest_message:
            return bool(latest_message.get("raw_has_image"))
        return bool(event.has_image())

    def _resolve_prompt_image_count(self, latest_message: Dict[str, Any], event: MessageEvent) -> int:
        if latest_message.get("image_count") is not None:
            return int(latest_message.get("image_count", 0) or 0)
        if latest_message.get("raw_image_count") is not None:
            return int(latest_message.get("raw_image_count", 0) or 0)
        return len(event.get_image_segments())

    def _resolve_prompt_sender_label(
        self,
        latest_message: Dict[str, Any],
        event: MessageEvent,
        context: Optional[MessageContext],
    ) -> str:
        if context and context.current_sender_label:
            return str(context.current_sender_label)
        speaker_name = str(latest_message.get("speaker_name") or "").strip()
        speaker_user_id = str(latest_message.get("user_id") or event.user_id).strip()
        if speaker_name or speaker_user_id:
            return self._format_identity_label(speaker_user_id, speaker_name)
        return self._format_identity_label(event.user_id, event.get_sender_display_name())

    def _build_companionship_hint_block(self, context: Optional[MessageContext]) -> str:
        signals = dict(getattr(context, "planning_signals", {}) or {}) if context else {}
        hints: List[str] = []
        if bool(signals.get("care_cue_detected")):
            hints.append("- 观察到当前消息可能包含状态或情绪暴露倾向")
        if bool(signals.get("continuation_cue_detected")):
            hints.append("- 观察到当前消息可能在顺着刚才的话题往下说")
        if bool(signals.get("follow_up_after_assistant")):
            hints.append("- 观察到用户像是在顺着助手上一句继续聊")
        if not hints:
            return ""
        return "附加观察：\n" + "\n".join(hints)

    def _build_user_prompt(
        self,
        event: MessageEvent,
        user_message: str,
        recent_messages: List[Dict[str, str]],
        window_messages: Optional[List[Dict[str, Any]]] = None,
        context: Optional[MessageContext] = None,
    ) -> str:
        del recent_messages
        window_messages = list((context.window_messages if context else window_messages) or [])
        latest_message = self._latest_window_message(window_messages)

        raw_text = self._resolve_prompt_raw_text(latest_message, event, user_message) or "[空]"
        clean_text = self._resolve_prompt_clean_text(latest_message, event, user_message) or "[空]"
        planner_text = user_message.strip() or "[空]"
        has_image_flag = self._resolve_prompt_has_image(latest_message, event)
        image_count = self._resolve_prompt_image_count(latest_message, event)
        message_shape = str(
            latest_message.get("message_shape")
            or ("image_only" if has_image_flag and clean_text == "[空]" else "text_only")
        )
        merged_description = str(latest_message.get("merged_description") or "").strip() or "无"
        per_image_descriptions = latest_message.get("per_image_descriptions") or []
        per_image_text = "\n".join(
            f"- 第{index}张: {description}" for index, description in enumerate(per_image_descriptions, 1)
        ) or "无"
        vision_available = "是" if latest_message.get("vision_available") else "否"
        sender_label = self._resolve_prompt_sender_label(latest_message, event, context)
        mentioned_names = [
            name for name in self._assistant_names() if name and (name in raw_text or name in clean_text)
        ]
        mentioned_names_text = "、".join(mentioned_names) if mentioned_names else "无"
        recent_history_text = context.recent_history_text if context and context.recent_history_text else self._build_recent_history_text(window_messages)
        temporal_summary = str((context.temporal_context.summary_text if context else "") or "").strip() or "当前缺少足够的时间跨度信息来判断对话连续性。"
        temporal_context = context.temporal_context if context else None
        planning_signals = dict(getattr(context, "planning_signals", {}) or {}) if context else {}
        temporal_lines = [f"时间观察：{temporal_summary}"]
        if temporal_context:
            temporal_lines.append(f"最近消息时间分层：{str(temporal_context.recent_gap_bucket or 'unknown')}")
            temporal_lines.append(f"当前会话时间分层：{str(temporal_context.conversation_gap_bucket or 'unknown')}")
            if str(temporal_context.session_gap_bucket or "unknown") != "unknown":
                temporal_lines.append(f"上一轮会话时间分层：{str(temporal_context.session_gap_bucket)}")
            temporal_lines.append(f"连续性信号标签：{str(temporal_context.continuity_hint or 'unknown')}")
        temporal_block = "\n".join(temporal_lines)
        signal_lines = []
        for key, value in planning_signals.items():
            signal_lines.append(f"- {key}: {value}")
        signal_block = "\n".join(signal_lines) if signal_lines else "- 无"
        companionship_hint_block = self._build_companionship_hint_block(context)

        image_context_lines = []
        if has_image_flag:
            image_context_lines.append(f"这条消息的内容形态：{self._describe_message_shape(message_shape)}。")
            image_context_lines.append(f"图片数量：{image_count}。")
            image_context_lines.append(f"图片摘要：{merged_description}")
            if per_image_text != "无":
                image_context_lines.append(f"逐图描述：\n{per_image_text}")
            if vision_available == "否":
                image_context_lines.append("这条消息里的图片信息目前不完整，理解结果有限。")
        else:
            image_context_lines.append(f"这条消息的内容形态：{self._describe_message_shape(message_shape)}。")

        if event.message_type == "private":
            parts = [
                "下面是当前这条私聊消息的判断上下文。",
                f"当前会话：\n这是和用户 {sender_label} 的私聊。",
                f"当前消息来自用户 {sender_label}：\n{planner_text}",
                f"原始文本：{raw_text}\n清洗后文本：{clean_text}\n" + "\n".join(image_context_lines) + f"\n{temporal_block}",
                "运行时观察信号：\n" + signal_block,
            ]
            if companionship_hint_block:
                parts.append(companionship_hint_block)
            parts.extend(
                [
                    recent_history_text,
                    "补充判断提醒：\n"
                    "- 上面的时间和上下文信息只是观察结果，不是最终结论\n"
                    "- 如果运行时信号显示用户仍在补充、消息可能未完整、或需要等待更多信息，可以自行判断是否 wait\n"
                    "- 只有在明显无需响应时才考虑 ignore\n"
                    "- 如果你选择 reply，请同时输出 prompt_plan，告诉下游回复模型该启用哪些上下文层\n"
                    "- 如果你选择 reply，请额外提供一段自然语言 reply_reference，告诉下游回复模型这次更适合怎么接话，但不要直接替它写完整回复\n"
                    "- 请只输出 JSON，不要输出解释。",
                ]
            )
            return "\n\n".join(part for part in parts if str(part or "").strip()).strip()

        parts = [
            "下面是当前这条群消息的判断上下文。",
            f"当前会话：\n这是群 {event.group_id} 里的消息。",
            f"当前消息来自用户 {sender_label}：\n{planner_text}",
            f"原始文本：{raw_text}\n清洗后文本：{clean_text}\n消息里提到了这些名字或别名：{mentioned_names_text}\n" + "\n".join(image_context_lines) + f"\n{temporal_block}",
            "运行时观察信号：\n" + signal_block,
        ]
        if companionship_hint_block:
            parts.append(companionship_hint_block)
        parts.extend(
            [
                recent_history_text,
                "补充判断提醒：\n"
                "- 上面的最近群聊记录和时间信息只是帮助你判断当前消息的语境\n"
                "- 请围绕当前消息做判断，不要把前面的话当成当前要回复的内容\n"
                "- 如果最近记录显示助手刚刚接过话，这只是一个事实信号，不代表一定不能再回\n"
                "- 如果你选择 reply，请同时输出 prompt_plan，告诉下游回复模型该启用哪些上下文层\n"
                "- 如果你选择 reply，请额外提供一段自然语言 reply_reference，告诉下游回复模型这次更适合怎么接话，但不要直接替它写完整回复\n"
                "- 请只输出 JSON，不要输出解释。",
            ]
        )
        return "\n\n".join(part for part in parts if str(part or "").strip()).strip()

    def _extract_json_object(self, content: str) -> Dict[str, Any]:
        text = content.strip()
        if not text:
            raise ValueError("empty planner response")

        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

        fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S | re.I)
        if fenced_match:
            return json.loads(fenced_match.group(1))

        json_match = re.search(r"\{.*\}", text, re.S)
        if json_match:
            return json.loads(json_match.group(0))

        raise ValueError(f"invalid planner response: {text[:200]}")

    def _normalize_action(self, action: str) -> str:
        value = action.strip().lower()
        mapping = {
            "reply": MessagePlanAction.REPLY.value,
            "respond": MessagePlanAction.REPLY.value,
            "回复": MessagePlanAction.REPLY.value,
            "wait": MessagePlanAction.WAIT.value,
            "等待": MessagePlanAction.WAIT.value,
            "暂缓": MessagePlanAction.WAIT.value,
            "observe": MessagePlanAction.WAIT.value,
            "ignore": MessagePlanAction.IGNORE.value,
            "no_reply": MessagePlanAction.IGNORE.value,
            "noreply": MessagePlanAction.IGNORE.value,
            "不回复": MessagePlanAction.IGNORE.value,
            "无需回复": MessagePlanAction.IGNORE.value,
            "忽略": MessagePlanAction.IGNORE.value,
            "skip": MessagePlanAction.IGNORE.value,
        }
        normalized = mapping.get(value, value)
        if normalized not in {
            MessagePlanAction.REPLY.value,
            MessagePlanAction.WAIT.value,
            MessagePlanAction.IGNORE.value,
        }:
            raise ValueError(f"unsupported planner action: {action}")
        return normalized

    def _parse_plan(self, content: str, *, event: MessageEvent, context: Optional[MessageContext] = None) -> MessageHandlingPlan:
        decision = self._extract_json_object(content)
        action = self._normalize_action(str(decision.get("action", "")))
        reason = str(decision.get("reason", "")).strip() or "模型未提供理由"
        return MessageHandlingPlan(
            action=action,
            reason=reason,
            source="planner",
            raw_decision=decision,
            prompt_plan=self.prompt_planner.parse_prompt_plan(decision, event=event, action=action, context=context),
            reply_reference=self.prompt_planner.parse_reply_reference(decision, event=event, action=action, context=context),
        )

    def _build_rule_plan(
        self,
        action: MessagePlanAction,
        reason: str,
        source: str = "rule",
    ) -> MessageHandlingPlan:
        return MessageHandlingPlan(action=action.value, reason=reason, source=source)

    def _build_fallback_plan(self, event: MessageEvent, error: str) -> MessageHandlingPlan:
        if event.message_type == "private":
            return MessageHandlingPlan(
                action=MessagePlanAction.REPLY.value,
                reason="规划模型异常，私聊回退为直接回复",
                source="fallback",
                raw_decision={"error": error},
                prompt_plan=self.prompt_planner.default_prompt_plan(
                    event=event,
                    action=MessagePlanAction.REPLY.value,
                    context=None,
                ),
                reply_reference="先围绕当前这句自然回应，轻一点，不要假装一直在连续聊。",
            )
        if event_mentions_account(event):
            return MessageHandlingPlan(
                action=MessagePlanAction.REPLY.value,
                reason=f"规划模型异常，但消息显式 @ 了 {self._assistant_name()}，回退为回复",
                source="fallback",
                raw_decision={"error": error},
                prompt_plan=self.prompt_planner.default_prompt_plan(
                    event=event,
                    action=MessagePlanAction.REPLY.value,
                    context=None,
                ),
                reply_reference="先回应当前被点名的内容，简洁一点，不要展开太多。",
            )
        return MessageHandlingPlan(
            action=MessagePlanAction.IGNORE.value,
            reason=f"规划模型异常，且消息未明确指向 {self._assistant_name()}，回退为忽略",
            source="fallback",
            raw_decision={"error": error},
        )

    async def plan(
        self,
        event: MessageEvent,
        user_message: str,
        recent_messages: Optional[List[Dict[str, str]]] = None,
        window_messages: Optional[List[Dict[str, Any]]] = None,
        context: Optional[MessageContext] = None,
    ) -> MessageHandlingPlan:
        if event.message_type not in {"group", "private"}:
            return MessageHandlingPlan(
                action=MessagePlanAction.IGNORE.value,
                reason="当前仅支持私聊和群聊规划",
                source="rule",
            )

        recent_messages = recent_messages or []
        window_messages = list((context.window_messages if context else window_messages) or [])

        if self.ai_client is None:
            if event.message_type == "private":
                return MessageHandlingPlan(
                    action=MessagePlanAction.REPLY.value,
                    reason="未配置规划模型，私聊回退为直接回复",
                    source="rule",
                    prompt_plan=self.prompt_planner.default_prompt_plan(
                        event=event,
                        action=MessagePlanAction.REPLY.value,
                        context=context,
                    ),
                    reply_reference="围绕当前私聊消息自然回应，先答当前这一句，不要强行续旧话题。",
                )
            return self._build_rule_plan(
                MessagePlanAction.IGNORE,
                "未配置群聊判断模型，当前仅在被 @ 时回复",
                source="rule",
            )

        messages = [
            self.ai_client.build_text_message("system", self._build_system_prompt(str(event.message_type or "").strip().lower())),
            self.ai_client.build_text_message(
                "user",
                self._build_user_prompt(
                    event,
                    context.user_message if context and context.user_message else user_message,
                    recent_messages,
                    window_messages=window_messages,
                    context=context,
                ),
            ),
        ]

        try:
            execution_key = context.execution_key if context else get_execution_key(event)
            if context and context.trace_id:
                logger.info(
                    "开始会话规划：%s",
                    format_trace_log(trace_id=context.trace_id, session_key=execution_key, message_id=event.message_id),
                )

            async def run_chat():
                return await self.ai_client.chat_completion(messages=messages, temperature=0.1)

            if self.model_invocation_router is not None:
                response = await self.model_invocation_router.submit(
                    purpose=ModelInvocationType.GROUP_PLAN,
                    trace_id=context.trace_id if context else "",
                    session_key=execution_key,
                    message_id=event.message_id,
                    label="会话规划",
                    runner=run_chat,
                )
            else:
                response = await run_chat()
            plan = self._parse_plan(response.content, event=event, context=context)
            if context and context.trace_id:
                temporal = getattr(context, "temporal_context", None)
                logger.info(
                    "会话规划完成：%s action=%s recent_gap_bucket=%s conversation_gap_bucket=%s session_gap_bucket=%s continuity_hint=%s reply_reference=%s",
                    format_trace_log(trace_id=context.trace_id, session_key=execution_key, message_id=event.message_id),
                    plan.action,
                    str(getattr(temporal, "recent_gap_bucket", "unknown") or "unknown"),
                    str(getattr(temporal, "conversation_gap_bucket", "unknown") or "unknown"),
                    str(getattr(temporal, "session_gap_bucket", "unknown") or "unknown"),
                    str(getattr(temporal, "continuity_hint", "unknown") or "unknown"),
                    str(plan.reply_reference or "").strip(),
                )
            return plan
        except (AIAPIError, asyncio.TimeoutError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("会话规划失败，改用回退策略：%s", exc)
            return self._build_fallback_plan(event, str(exc))

    async def close(self) -> None:
        if self._owns_ai_client and self.ai_client:
            await self.ai_client.close()


__all__ = ["ConversationPlanner"]
