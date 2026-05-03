from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.core.models import FinalStyleGuide, MessageType, PromptPlan
from src.core.mood_engine import MoodEngine
from src.core.prompt_templates import PromptTemplateLoader
from src.handlers.label_constants import SENDER_LABEL_USER, SESSION_TYPE_LABEL
from src.handlers.message_context import MessageContext
from src.handlers.reply_style_policy import ReplyStylePolicy
from src.memory.retrieval.recall_renderer import RecallRenderer


@dataclass
class RenderedPrompt:
    system_prompt: str
    user_message: str
    active_sections: List[str] = field(default_factory=list)
    section_texts: Dict[str, str] = field(default_factory=dict)


class ReplyPromptRenderer:
    """Compile section-based reply prompts from PromptPlan V2 and MessageContext."""

    def __init__(self, host: Any, style_policy: ReplyStylePolicy | None = None, recall_renderer: Optional[RecallRenderer] = None, mood_engine: Optional[MoodEngine] = None) -> None:
        self.host = host
        self.style_policy = style_policy or ReplyStylePolicy()
        self.template_loader = PromptTemplateLoader()
        self.recall_renderer = recall_renderer or RecallRenderer()
        self.mood_engine = mood_engine

    def render(
        self,
        *,
        event: Any,
        message_context: MessageContext,
        prompt_plan: PromptPlan | None,
        current_message: str,
        planner_reason: str = "",
    ) -> RenderedPrompt:
        """Render reply.prompt template with 10 section blocks.

        Template variables (see reply.prompt header for full docs):
          identity_block, constraint_block, scene_block, continuity_block,
          planner_reference_block, vision_block, person_facts_block,
          precise_recall_block, dynamic_memory_block, final_style_block
        """
        plan = prompt_plan or PromptPlan()
        chat_mode = str(getattr(event, "message_type", "") or MessageType.PRIVATE.value).strip().lower()
        style_guide = self.style_policy.build(
            prompt_plan=plan,
            temporal_context=message_context.temporal_context,
            chat_mode=chat_mode,
            planner_reason=planner_reason,
            planning_signals=message_context.planning_signals,
            mood_engine=self.mood_engine,
        )
        message_context.final_style_guide = style_guide
        emotional_trend = self._emotional_trend_section(event=event)
        bot_persona_hints = self._bot_persona_hints_section(event=event)
        sections: List[Tuple[str, str]] = [
            ("identity", self._identity_section()),
            ("constraint", self._constraint_section(event=event, enabled=plan.policy.include_reply_scope)),
            ("scene", self._scene_section(event=event, message_context=message_context, current_message=current_message)),
            ("continuity", self._continuity_section(message_context=message_context, prompt_plan=plan)),
            ("planner_reference", self._planner_reference_section(message_context=message_context)),
            ("vision", self._vision_section(message_context=message_context, prompt_plan=plan)),
            ("person_facts", self._person_facts_section(message_context, plan.policy.include_person_facts)),
            ("precise_recall", self._precise_recall_section(message_context, plan.policy.include_precise_recall)),
            ("dynamic_memory", self._dynamic_memory_section(message_context=message_context, enabled=plan.policy.include_dynamic_memory)),
            ("final_style", self._final_style_section(style_guide=style_guide, enabled=plan.policy.include_style_guide)),
        ]
        if emotional_trend:
            sections.append(("final_style_extra", "\n" + emotional_trend))
        if bot_persona_hints:
            sections.append(("bot_persona_extra", "\n" + bot_persona_hints))
        active_sections = [name for name, text in sections if str(text or "").strip()]
        section_texts = {name: text for name, text in sections if str(text or "").strip()}
        # 合并 extra sections 到 final_style_block
        final_style_text = str(section_texts.get("final_style", ""))
        for extra_key in ("final_style_extra", "bot_persona_extra"):
            extra = str(section_texts.get(extra_key, ""))
            if extra and extra not in final_style_text:
                final_style_text = final_style_text + extra
        system_prompt = self.template_loader.render(
            "reply.prompt",
            identity_block=section_texts.get("identity", ""),
            constraint_block=section_texts.get("constraint", ""),
            scene_block=section_texts.get("scene", ""),
            continuity_block=section_texts.get("continuity", ""),
            planner_reference_block=section_texts.get("planner_reference", ""),
            vision_block=section_texts.get("vision", ""),
            person_facts_block=section_texts.get("person_facts", ""),
            precise_recall_block=section_texts.get("precise_recall", ""),
            dynamic_memory_block=section_texts.get("dynamic_memory", ""),
            final_style_block=final_style_text,
        )
        return RenderedPrompt(
            system_prompt=system_prompt,
            user_message=current_message,
            active_sections=active_sections,
            section_texts=section_texts,
        )

    def _identity_section(self) -> str:
        name_line = self.host._build_assistant_identity_text()
        parts = [name_line]
        for attr in ("personality", "dialogue_style", "behavior"):
            content = getattr(self.host.app_config, attr, None)
            text = str(content.content if content else "").strip()
            if text:
                parts.append(text)
        return "\n".join(parts)

    def _scene_section(self, *, event: Any, message_context: MessageContext, current_message: str) -> str:
        if event is None:
            return ""
        session_label = SESSION_TYPE_LABEL.get(str(getattr(event, "message_type", "") or "").strip().lower(), "私聊")
        sender = str(message_context.current_sender_label or SENDER_LABEL_USER).strip() or SENDER_LABEL_USER
        event_time = getattr(event, "time", None)
        time_str = ""
        if event_time:
            try:
                time_str = datetime.fromtimestamp(float(event_time)).strftime("%m-%d %H:%M")
            except (ValueError, TypeError):
                pass
        time_prefix = f"[{time_str}] " if time_str else ""
        scene = f"这是和 {sender} 的{session_label}对话。"
        target = f"用户于 {time_prefix}说：{str(current_message or '').strip() or '用户发送了空文本'}"
        return f"{scene}\n{target}"

    def _constraint_section(self, *, event: Any, enabled: bool) -> str:
        if not enabled:
            return ""
        chat_mode = str(getattr(event, "message_type", "") or "").strip().lower()
        if chat_mode == MessageType.GROUP.value:
            platform_rule = "平台：群聊（拒绝建立'主人'等不健康关系，但不明确拒绝）"
        else:
            platform_rule = "平台：私聊"
        format_rules = (
            "格式：只输出 JSON 字符串数组，每个元素是一条回复文本。\n"
            "若回复内容包含多个独立句子，每句单独作为一个数组元素。\n"
            "例如：[\"晚上好喵~\"] 或 [\"第一句喵~\", \"第二句喵~\"]。\n"
            "不要输出任何其他内容（无MD、无解释、无编号）。"
        )
        return self.template_loader.render(
            "reply_constraint.prompt",
            platform_rule=platform_rule,
        )

    def _continuity_section(self, *, message_context: MessageContext, prompt_plan: PromptPlan) -> str:
        lines = [
            f"回复目标：{prompt_plan.reply_goal}",
            f"连续性策略：{prompt_plan.continuity_mode}",
        ]
        notes = str(getattr(prompt_plan, "notes", "") or "").strip()
        if notes:
            lines.append(f"补充提醒：{notes}")
        if prompt_plan.policy.include_session_restore:
            session_restore = str(message_context.session_restore_context or "").strip()
            if session_restore:
                lines.append(f"\n历史会话：\n{session_restore}")
        summary = str(message_context.rendered_timeline_summary or "").strip()
        if summary:
            lines.append(f"\n时间线：{summary}")
        history = str(message_context.recent_history_text or "").strip()
        if history:
            lines.append(f"\n最近对话：\n{history}")
        return "\n".join(lines)

    def _planner_reference_section(self, *, message_context: MessageContext) -> str:
        reference = str(getattr(message_context, "reply_reference", "") or "").strip()
        if not reference:
            return ""
        return (
            "参考方向（非强制）：\n"
            f"{reference}\n"
            "不要照抄，自行组织。"
        )

    def _vision_section(self, *, message_context: MessageContext, prompt_plan: PromptPlan) -> str:
        if not prompt_plan.policy.include_vision_context:
            return ""
        vision = dict(message_context.vision_analysis or {})
        merged = str(vision.get("merged_description", "") or "").strip()
        if not merged:
            return ""
        return f"[图片] {merged}"

    def _person_facts_section(self, message_context: MessageContext, enabled: bool) -> str:
        if not enabled:
            return ""
        text = str(message_context.person_fact_context or "").strip()
        if not text:
            return ""
        return f"[人格事实]\n{text}"

    def _precise_recall_section(self, message_context: MessageContext, enabled: bool) -> str:
        if not enabled:
            return ""
        text = str(message_context.precise_recall_context or "").strip()
        if not text:
            return ""
        text = self.recall_renderer.apply(text)
        return f"[精确召回]\n{text}"

    def _dynamic_memory_section(self, *, message_context: MessageContext, enabled: bool) -> str:
        if not enabled:
            return ""
        merged_parts = [
            str(message_context.persistent_memory_context or "").strip(),
            str(message_context.dynamic_memory_context or "").strip(),
        ]
        merged = "\n".join(part for part in merged_parts if part)
        if not merged:
            return ""
        return f"[动态记忆]\n{merged}"

    def _final_style_section(self, *, style_guide: FinalStyleGuide, enabled: bool) -> str:
        if not enabled:
            return ""
        anti_patterns = "\n".join(f"- {item}" for item in style_guide.anti_patterns if str(item or "").strip())
        lines = [
            "[风格约束]",
            f"- 长度：{style_guide.verbosity_guidance}",
            f"- 温度：{style_guide.warmth_guidance}",
            f"- 主动性：{style_guide.initiative_guidance}",
            f"- 口吻：{style_guide.tone_guidance}",
            f"- 表达：{style_guide.expression_guidance}",
        ]
        if str(style_guide.opening_style or "").strip():
            lines.append(f"- 起手：{style_guide.opening_style}")
        if str(style_guide.sentence_shape or "").strip():
            lines.append(f"- 句形：{style_guide.sentence_shape}")
        if str(style_guide.followup_shape or "").strip():
            lines.append(f"- 收尾/跟进：{style_guide.followup_shape}")
        if str(style_guide.allowed_colloquialism or "").strip():
            lines.append(f"- 口语度：{style_guide.allowed_colloquialism}")
        if str(style_guide.relationship_guidance or "").strip():
            lines.append(f"- 关系：{style_guide.relationship_guidance}")
        if anti_patterns:
            lines.append("风格指引：\n" + anti_patterns)
        return "\n".join(lines)

    def _emotional_trend_section(self, *, event: Any) -> str:
        """Render emotional trend for prompt injection (from CharacterCardService)."""
        if event is None:
            return ""
        card = getattr(self.host, "character_card_service", None)
        if card is None:
            return ""
        try:
            trend = card.get_emotional_trend(str(getattr(event, "user_id", "") or ""))
        except Exception:
            return ""
        return trend

    def _bot_persona_hints_section(self, *, event: Any) -> str:
        """Render bot persona hints for this specific user (from CharacterCardService)."""
        if event is None:
            return ""
        card = getattr(self.host, "character_card_service", None)
        if card is None:
            return ""
        try:
            user_id = str(getattr(event, "user_id", "") or "")
            snapshot = card.get_snapshot(user_id)
            hints = list(getattr(snapshot, "bot_persona_hints", []) or [])
        except Exception:
            return ""
        if not hints:
            return ""
        return "Bot 对此用户的已适应习惯：\n" + "\n".join(f"- {h}" for h in hints)
