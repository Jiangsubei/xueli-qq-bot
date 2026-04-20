from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from src.core.models import FinalStyleGuide, MessageType, PromptPlan
from src.handlers.message_context import MessageContext
from src.handlers.reply_style_policy import ReplyStylePolicy


@dataclass
class RenderedPrompt:
    system_prompt: str
    user_message: str
    active_sections: List[str] = field(default_factory=list)
    section_texts: Dict[str, str] = field(default_factory=dict)


class ReplyPromptRenderer:
    """Compile section-based reply prompts from PromptPlan V2 and MessageContext."""

    def __init__(self, host: Any, style_policy: ReplyStylePolicy | None = None) -> None:
        self.host = host
        self.style_policy = style_policy or ReplyStylePolicy()

    def render(
        self,
        *,
        event: Any,
        message_context: MessageContext,
        prompt_plan: PromptPlan | None,
        current_message: str,
        planner_reason: str = "",
    ) -> RenderedPrompt:
        plan = prompt_plan or PromptPlan()
        chat_mode = str(getattr(event, "message_type", "") or MessageType.PRIVATE.value).strip().lower()
        style_guide = self.style_policy.build(
            prompt_plan=plan,
            temporal_context=message_context.temporal_context,
            chat_mode=chat_mode,
            planner_reason=planner_reason,
            planning_signals=message_context.planning_signals,
        )
        message_context.final_style_guide = style_guide
        sections: List[Tuple[str, str]] = [
            ("identity", self._identity_section()),
            ("session", self._session_section(event=event, message_context=message_context)),
            ("reply_target", self._reply_target_section(event=event, current_message=current_message)),
            ("continuity", self._continuity_section(message_context=message_context, prompt_plan=plan)),
            ("timeline", self._timeline_section(message_context=message_context, prompt_plan=plan)),
            ("recent_history", self._recent_history_section(message_context=message_context, prompt_plan=plan)),
            ("person_facts", self._simple_section("这些是当前用户的长期事实：", message_context.person_fact_context, enabled=plan.policy.include_person_facts)),
            ("session_restore", self._simple_section("这是上一轮相关会话的恢复摘要：", message_context.session_restore_context, enabled=plan.policy.include_session_restore)),
            ("precise_recall", self._simple_section("这是和当前话题直接相关的旧对话定位：", message_context.precise_recall_context, enabled=plan.policy.include_precise_recall)),
            ("dynamic_memory", self._dynamic_memory_section(message_context=message_context, enabled=plan.policy.include_dynamic_memory)),
            ("vision_context", self._vision_section(message_context=message_context, prompt_plan=plan)),
            ("reply_scope", self._reply_scope_section(event=event, enabled=plan.policy.include_reply_scope)),
            ("final_style", self._final_style_section(style_guide=style_guide, enabled=plan.policy.include_style_guide)),
        ]
        active_sections = [name for name, text in sections if str(text or "").strip()]
        section_texts = {name: text for name, text in sections if str(text or "").strip()}
        system_prompt = "\n\n".join(section_texts[name] for name in active_sections)
        return RenderedPrompt(
            system_prompt=system_prompt,
            user_message=current_message,
            active_sections=active_sections,
            section_texts=section_texts,
        )

    def _identity_section(self) -> str:
        return "\n\n".join(
            part for part in [self.host._build_assistant_identity_prompt(), self.host._build_system_prompt()] if str(part or "").strip()
        )

    def _session_section(self, *, event: Any, message_context: MessageContext) -> str:
        if event is None:
            return ""
        session_label = "群聊" if str(getattr(event, "message_type", "") or "").strip().lower() == MessageType.GROUP.value else "私聊"
        sender = str(message_context.current_sender_label or "用户").strip() or "用户"
        return f"当前场景：这是和 {sender} 的{session_label}对话。"

    def _reply_target_section(self, *, event: Any, current_message: str) -> str:
        sender = "用户"
        if event is not None:
            sender = str(getattr(event, "user_id", "") or "用户")
        return f"当前要回复的目标消息来自 {sender}：\n{str(current_message or '').strip() or '[空]'}"

    def _continuity_section(self, *, message_context: MessageContext, prompt_plan: PromptPlan) -> str:
        summary = str(getattr(message_context.temporal_context, "summary_text", "") or "").strip()
        lines = [
            f"回复目标：{prompt_plan.reply_goal}",
            f"连续性策略：{prompt_plan.continuity_mode}",
        ]
        if summary:
            lines.append(f"时间连续性观察：{summary}")
        notes = str(getattr(prompt_plan, "notes", "") or "").strip()
        if notes:
            lines.append(f"补充提醒：{notes}")
        return "\n".join(lines)

    def _timeline_section(self, *, message_context: MessageContext, prompt_plan: PromptPlan) -> str:
        detail = str(prompt_plan.timeline_detail or "summary").strip().lower()
        if detail == "off":
            return ""
        summary = str(message_context.rendered_timeline_summary or "").strip()
        recent_history = str(message_context.rendered_recent_history or "").strip()
        if detail == "summary":
            return f"时间线摘要：{summary}" if summary else ""
        return "\n".join(part for part in ["时间线信息：", summary, recent_history] if str(part or "").strip())

    def _recent_history_section(self, *, message_context: MessageContext, prompt_plan: PromptPlan) -> str:
        if not prompt_plan.policy.include_recent_history:
            return ""
        history = str(message_context.rendered_recent_history or message_context.recent_history_text or "").strip()
        if not history:
            return ""
        return "最近上下文：\n" + history

    def _simple_section(self, title: str, content: str, *, enabled: bool) -> str:
        if not enabled:
            return ""
        text = str(content or "").strip()
        if not text:
            return ""
        return f"{title}\n{text}"

    def _vision_section(self, *, message_context: MessageContext, prompt_plan: PromptPlan) -> str:
        if not prompt_plan.policy.include_vision_context:
            return ""
        vision = dict(message_context.vision_analysis or {})
        merged = str(vision.get("merged_description", "") or "").strip()
        if not merged:
            return ""
        return f"图片上下文：\n{merged}"

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
        return "需要注意的相关记忆：\n" + merged

    def _reply_scope_section(self, *, event: Any, enabled: bool) -> str:
        if not enabled:
            return ""
        chat_mode = str(getattr(event, "message_type", "") or "").strip().lower()
        if chat_mode == MessageType.GROUP.value:
            return "回复范围：从当前消息开始接话，历史与记忆只用于理解，不要转而回复别人的旧消息。"
        return "回复范围：围绕当前消息回复，历史与记忆只用于理解，不要暴露它们来自提示信息。"

    def _final_style_section(self, *, style_guide: FinalStyleGuide, enabled: bool) -> str:
        if not enabled:
            return ""
        anti_patterns = "\n".join(f"- {item}" for item in style_guide.anti_patterns if str(item or "").strip())
        parts = [
            "最终回复风格：",
            f"- 长度：{style_guide.verbosity_guidance}",
            f"- 温度：{style_guide.warmth_guidance}",
            f"- 主动性：{style_guide.initiative_guidance}",
            f"- 口吻：{style_guide.tone_guidance}",
            f"- 表达：{style_guide.expression_guidance}",
        ]
        if anti_patterns:
            parts.append("避免：\n" + anti_patterns)
        return "\n".join(parts)
