from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from src.core.config import AppConfig, config, is_group_reply_decision_configured
from src.core.message_trace import format_trace_log
from src.core.model_invocation_router import ModelInvocationRouter, ModelInvocationType
from src.core.models import MessageEvent, MessageHandlingPlan, MessagePlanAction
from src.handlers.message_context import MessageContext
from src.services.ai_client import AIAPIError, AIClient

logger = logging.getLogger(__name__)


class GroupReplyPlanner:
    """Use a dedicated model to decide reply/wait/ignore for group chat."""

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

    def _create_ai_client(self) -> Optional[AIClient]:
        if not is_group_reply_decision_configured(self.app_config):
            logger.debug("未配置群聊判断模型，群聊规划器已禁用")
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
        logger.debug("初始化群聊规划模型：模型=%s", client_config.get("model"))
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

    def _build_system_prompt(self) -> str:
        return (
            "你现在的任务，不是生成回复内容，而是判断这个群聊助手此刻要不要接当前这条群消息。\n\n"
            "请把自己当成群里的自然成员，而不是客服或问答机器人。你的判断重点不是“理论上能不能回”，而是“现在接这一句是否自然、合适、不突兀”。\n\n"
            "你会看到当前消息，以及这条消息之前群里刚刚聊过的内容。这些上下文只是帮助你判断当前这一轮要不要接话。"
            "你必须把注意力放在“当前消息”上，不要转而去回应前面其他人的旧消息。\n\n"
            "一般来说：\n"
            "- 如果当前消息很适合自然接话，回一句会更顺，就选 reply\n"
            "- 如果当前消息暂时能接，但现在还不是最顺的时机，就选 wait\n"
            "- 只有在明显不适合插话、插话会显得打断、抢戏、刷存在感时，才选 ignore\n\n"
            "如果当前消息明显是在对助手说话，应该更积极地考虑 reply。\n"
            "如果群里最近已经显示助手刚接过话，要注意避免连续刷屏。\n"
            "如果当前消息带图，可以结合图片摘要一起判断；如果只有图片且信息不足，优先考虑 wait。\n\n"
            "你只允许输出 JSON，格式必须是：\n"
            '{"action":"reply|wait|ignore","reason":"简短理由"}\n\n'
            "不要输出 JSON 以外的任何内容。"
        ).strip()

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
            image_note = f" [图片 {item.get('image_count', 1)} 张]" if item.get("has_image") else ""
            lines.append(f"{speaker}: {text}{image_note}")

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
        if text and text != "[空]":
            return text
        raw_image_count = int(item.get("raw_image_count", item.get("image_count", 0)) or 0)
        if bool(item.get("raw_has_image")) or raw_image_count > 0:
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
        latest_message = next(
            (item for item in reversed(window_messages) if item.get("is_latest")),
            window_messages[-1] if window_messages else {},
        )

        raw_text = str(latest_message.get("raw_text") or event.extract_text().strip() or "[空]")
        clean_text = str(latest_message.get("text_content") or "").strip() or "[空]"
        planner_text = user_message.strip() or "[空]"
        has_image_flag = bool(latest_message.get("has_image", event.has_image()))
        image_count = int(latest_message.get("image_count", len(event.get_image_segments())) or 0)
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
        sender_label = context.current_sender_label if context and context.current_sender_label else self._format_identity_label(event.user_id, event.get_sender_display_name())
        mentioned_names = [
            name for name in self._assistant_names() if name and (name in raw_text or name in clean_text)
        ]
        mentioned_names_text = "、".join(mentioned_names) if mentioned_names else "无"
        recent_history_text = context.recent_history_text if context and context.recent_history_text else self._build_recent_history_text(window_messages)

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

        return (
            "下面是当前这条群消息的判断上下文。\n\n"
            f"当前会话：\n这是群 {event.group_id} 里的消息。\n\n"
            f"当前消息来自用户 {sender_label}：\n{planner_text}\n\n"
            f"原始文本：{raw_text}\n"
            f"清洗后文本：{clean_text}\n"
            f"消息里提到了这些名字或别名：{mentioned_names_text}\n"
            + "\n".join(image_context_lines)
            + "\n\n"
            + recent_history_text
            + "\n\n补充判断提醒：\n"
            + "- 上面的最近群聊记录只是帮助你理解当前消息是在接什么话\n"
            + "- 请只判断“当前消息”现在要不要接\n"
            + "- 不要转而回复前面其他用户之前说的话\n"
            + "- 如果最近记录里显示你刚刚已经接过话，要考虑是否会显得连续刷屏\n"
            + "- 请只输出 JSON，不要输出解释。"
        ).strip()

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

    def _parse_plan(self, content: str) -> MessageHandlingPlan:
        decision = self._extract_json_object(content)
        action = self._normalize_action(str(decision.get("action", "")))
        reason = str(decision.get("reason", "")).strip() or "模型未提供理由"
        return MessageHandlingPlan(
            action=action,
            reason=reason,
            source="planner",
            raw_decision=decision,
        )

    def _build_rule_plan(
        self,
        action: MessagePlanAction,
        reason: str,
        source: str = "rule",
    ) -> MessageHandlingPlan:
        return MessageHandlingPlan(action=action.value, reason=reason, source=source)

    def _build_fallback_plan(self, event: MessageEvent, error: str) -> MessageHandlingPlan:
        if event.is_at(event.self_id):
            return MessageHandlingPlan(
                action=MessagePlanAction.REPLY.value,
                reason=f"规划模型异常，但消息显式 @ 了 {self._assistant_name()}，回退为回复",
                source="fallback",
                raw_decision={"error": error},
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
        if event.message_type != "group":
            return MessageHandlingPlan(
                action=MessagePlanAction.IGNORE.value,
                reason="仅群聊消息需要规划",
                source="rule",
            )

        recent_messages = recent_messages or []
        window_messages = list((context.window_messages if context else window_messages) or [])

        if self.ai_client is None:
            return self._build_rule_plan(
                MessagePlanAction.IGNORE,
                "未配置群聊判断模型，当前仅在被 @ 时回复",
                source="rule",
            )

        messages = [
            self.ai_client.build_text_message("system", self._build_system_prompt()),
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
            if context and context.trace_id:
                logger.info(
                    "开始群聊规划：%s",
                    format_trace_log(trace_id=context.trace_id, session_key=context.execution_key or f"group:{event.group_id}", message_id=event.message_id),
                )
            async def run_chat():
                return await self.ai_client.chat_completion(messages=messages, temperature=0.1)

            if self.model_invocation_router is not None:
                response = await self.model_invocation_router.submit(
                    purpose=ModelInvocationType.GROUP_PLAN,
                    trace_id=context.trace_id if context else "",
                    session_key=context.execution_key if context else f"group:{event.group_id}",
                    message_id=event.message_id,
                    label="群聊规划",
                    runner=run_chat,
                )
            else:
                response = await run_chat()
            plan = self._parse_plan(response.content)
            if context and context.trace_id:
                logger.info(
                    "群聊规划完成：%s action=%s",
                    format_trace_log(trace_id=context.trace_id, session_key=context.execution_key or f"group:{event.group_id}", message_id=event.message_id),
                    plan.action,
                )
            return plan
        except (AIAPIError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("群聊规划失败，改用回退策略：%s", exc)
            return self._build_fallback_plan(event, str(exc))

    async def close(self) -> None:
        if self._owns_ai_client and self.ai_client:
            await self.ai_client.close()
