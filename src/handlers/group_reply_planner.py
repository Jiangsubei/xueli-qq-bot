from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from src.core.config import AppConfig, config, is_group_reply_decision_configured
from src.core.models import MessageEvent, MessageHandlingPlan, MessagePlanAction
from src.services.ai_client import AIAPIError, AIClient

logger = logging.getLogger(__name__)


class GroupReplyPlanner:
    """Use a dedicated model to decide reply/wait/ignore for group chat."""

    def __init__(
        self,
        ai_client: Optional[AIClient] = None,
        *,
        app_config: Optional[AppConfig] = None,
    ) -> None:
        self.app_config = app_config or config.app
        self.ai_client = ai_client or self._create_ai_client()
        self._owns_ai_client = ai_client is None

    def _create_ai_client(self) -> Optional[AIClient]:
        if not is_group_reply_decision_configured(self.app_config):
            logger.info("未配置群聊判断模型，群聊规划器已禁用")
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
        logger.info("初始化群聊规划模型：模型=%s", client_config.get("model"))
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
        identity_text = self._build_assistant_identity_text()
        names_text = "、".join(f"“{name}”" for name in self._assistant_names()) or "无"
        interest_rule = ""
        if self.app_config.group_reply.interest_reply_enabled:
            interest_rule = "7. 对情绪表达、日常分享、吐槽、求安慰这类消息，如果接话自然，可以更积极地选择 reply。\n"

        return (
            f"你是一个群聊回复规划器，不直接生成回复内容，只负责判断 {identity_text} 此刻要不要在群里接一句话。\n"
            "这是聊天软件里的日常聊天，不是客服问答，也不是正式讨论。群里出现短句、碎句、半截话、口头语、玩笑、表情配一句话都很正常，不能因为消息短、信息少、语法不完整，就默认不适合回复。\n"
            "这个助手更像群里的熟人搭子：会接梗、接情绪、陪聊，语气轻松自然，有人味，但不会硬凑热闹或尬聊刷存在感。\n"
            "你只能输出 JSON：\n"
            '{"action":"reply|wait|ignore","reason":"简短理由"}\n\n'
            f"这个助手可能会被以下称呼提及：{names_text}。\n"
            "你会看到“最近群聊记录”，其中可能同时包含群成员消息和助手自己之前在群里的回复。请把这些都视为真实上下文，用来判断当前这句插话是否自然。\n"
            "判断重点不是“理论上要不要回”，而是“这时候接一句是否自然、合适、不突兀”。只要接话顺气氛、不明显打断别人，就可以更积极地选择 reply，不必卡得太严。\n"
            "判断标准：\n"
            "1. reply：只要当前消息有自然接话空间，回一句会更顺、更轻松、更有陪伴感，就选 reply。不要因为字少就保守。\n"
            "2. wait：可以接，但现在不是最顺的时机，就选 wait。比如话题还在快速展开、多人接力聊天、图文信息还不够完整。wait 是“再看看”，不是“不适合回”。\n"
            "3. ignore：只有在明显不适合插话，或者插话会显得生硬、扫兴、抢戏、强行刷存在感时才选 ignore。比如纯事务通知、工作对接、报数、确认、安排、同步进度等。\n"
            "4. 优先像一个自然的群友，而不是一个过度谨慎的工具。\n"
            "5. 不要动不动讲道理、提建议、做分析；很多时候大家只是想有人接一句。\n"
            "6. 如果消息带图，请结合文字和图片描述一起判断；纯图片且语义不清时优先 wait。\n"
            f"{interest_rule}\n"
            "额外要求：\n"
            "1. reason 必须简短明确。\n"
            "2. 最近群聊记录如果已经显示助手刚接过话，要注意继续追问是否自然，避免连续刷屏。\n"
            "3. 不要输出 markdown，不要输出 JSON 以外的解释。"
        ).strip()

    def _format_window_messages(self, window_messages: List[Dict[str, Any]]) -> str:
        if not window_messages:
            return "无"
        lines = []
        for index, item in enumerate(window_messages, 1):
            role = str(item.get("speaker_role") or "user").strip().lower()
            speaker = (
                f"助手 {str(item.get('speaker_name') or self._assistant_name()).strip() or self._assistant_name()}"
                if role == "assistant"
                else f"用户 {str(item.get('user_id') or 'unknown')}"
            )
            text = str(item.get("text") or item.get("raw_text") or "").strip() or "[空]"
            shape_text = self._describe_message_shape(str(item.get("message_shape", "")))
            image_note = f" [图片 {item.get('image_count', 1)} 张]" if item.get("has_image") else ""
            latest_note = " [当前消息]" if item.get("is_latest") else ""
            lines.append(f"{index}. {speaker}: {text}{image_note} [{shape_text}]{latest_note}")

            merged_description = str(item.get("merged_description") or "").strip()
            if merged_description:
                lines.append(f"   图片摘要: {merged_description}")
            for image_index, description in enumerate(item.get("per_image_descriptions") or [], 1):
                lines.append(f"   第{image_index}张: {description}")
            if item.get("has_image") and not item.get("vision_available"):
                failure_count = int(item.get("vision_failure_count", 0) or 0)
                if failure_count > 0:
                    lines.append(f"   图片理解失败数: {failure_count}")
                vision_error = str(item.get("vision_error") or "").strip()
                if vision_error:
                    lines.append(f"   图片理解错误: {vision_error}")
        return "\n".join(lines)

    def _build_user_prompt(
        self,
        event: MessageEvent,
        user_message: str,
        recent_messages: List[Dict[str, str]],
        window_messages: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        del recent_messages
        identity_text = self._build_assistant_identity_text()
        window_messages = window_messages or []
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
        vision_failure_count = int(latest_message.get("vision_failure_count", 0) or 0)
        is_image_only = "是" if latest_message.get("is_image_only") else "否"
        text_present = "是" if latest_message.get("text_present") else "否"
        at_self = "是" if event.is_at(event.self_id) else "否"
        has_image = "是" if has_image_flag else "否"

        mentioned_names = [
            name for name in self._assistant_names() if name and (name in raw_text or name in clean_text)
        ]
        mentioned_names_text = "、".join(mentioned_names) if mentioned_names else "无"

        return (
            f"请判断这条群消息是否应该由 {identity_text} 立即回复。\n"
            f"群号: {event.group_id}\n"
            f"发送者: {event.user_id}\n"
            f"是否 @ 助手: {at_self}\n"
            f"消息里提到了哪些名字/别名: {mentioned_names_text}\n"
            f"是否含图片: {has_image}\n"
            f"图片数量: {image_count}\n"
            f"消息形态: {self._describe_message_shape(message_shape)}\n"
            f"是否纯图片消息: {is_image_only}\n"
            f"是否含文字内容: {text_present}\n"
            f"视觉结果可用: {vision_available}\n"
            f"视觉失败数: {vision_failure_count}\n"
            f"原始文本: {raw_text}\n"
            f"清洗后文本: {clean_text}\n"
            f"提供给规划器的消息文本: {planner_text}\n"
            f"图片合并摘要: {merged_description}\n"
            f"逐图描述:\n{per_image_text}\n"
            f"最近群聊记录（包含助手消息）:\n{self._format_window_messages(window_messages)}"
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
    ) -> MessageHandlingPlan:
        if event.message_type != "group":
            return MessageHandlingPlan(
                action=MessagePlanAction.IGNORE.value,
                reason="仅群聊消息需要规划",
                source="rule",
            )

        recent_messages = recent_messages or []
        window_messages = window_messages or []

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
                    user_message,
                    recent_messages,
                    window_messages=window_messages,
                ),
            ),
        ]

        try:
            response = await self.ai_client.chat_completion(messages=messages, temperature=0.1)
            return self._parse_plan(response.content)
        except (AIAPIError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("群聊规划失败，改用回退策略：%s", exc)
            return self._build_fallback_plan(event, str(exc))

    async def close(self) -> None:
        if self._owns_ai_client and self.ai_client:
            await self.ai_client.close()
