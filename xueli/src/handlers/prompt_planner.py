from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from src.core.models import MessageEvent, MessageHandlingPlan, MessagePlanAction, PromptPlan, PromptSectionPolicy
from src.handlers.message_context import MessageContext


@dataclass
class PromptPlanner:
    """Build PromptPlan V2 defaults and parse planner-provided prompt policies."""

    def decision_output_schema(self) -> str:
        return (
            '{"action":"reply|wait|ignore","reason":"简短理由","prompt_plan":{'
            '"reply_goal":"answer|continue|comfort|clarify|recall|light_presence",'
            '"continuity_mode":"direct_continue|resume_recent_topic|resume_old_topic",'
            '"timeline_detail":"off|summary|per_message",'
            '"context_profile":"compact|standard|full",'
            '"memory_profile":"off|facts_only|relevant|rich",'
            '"tone_profile":"concise|balanced|warm|deep",'
            '"initiative":"reactive|gentle_follow|proactive_follow",'
            '"expression_profile":"plain|colloquial|companion",'
            '"policy":{'
            '"include_recent_history":true,'
            '"include_person_facts":true,'
            '"include_session_restore":true,'
            '"include_precise_recall":true,'
            '"include_dynamic_memory":true,'
            '"include_vision_context":true,'
            '"include_reply_scope":true,'
            '"include_style_guide":true},'
            '"notes":"可选说明"}}'
        )

    def parse_prompt_plan(
        self,
        decision: Dict[str, Any],
        *,
        event: MessageEvent,
        action: str,
        context: Optional[MessageContext],
    ) -> Optional[PromptPlan]:
        if action != MessagePlanAction.REPLY.value:
            return None

        default_plan = self.default_prompt_plan(event=event, action=action, context=context)
        raw_plan = decision.get("prompt_plan")
        if not isinstance(raw_plan, dict):
            return default_plan

        raw_policy = raw_plan.get("policy") if isinstance(raw_plan.get("policy"), dict) else {}
        policy = PromptSectionPolicy(
            include_recent_history=self._bool_value(raw_policy.get("include_recent_history"), default_plan.policy.include_recent_history),
            include_person_facts=self._bool_value(raw_policy.get("include_person_facts"), default_plan.policy.include_person_facts),
            include_session_restore=self._bool_value(raw_policy.get("include_session_restore"), default_plan.policy.include_session_restore),
            include_precise_recall=self._bool_value(raw_policy.get("include_precise_recall"), default_plan.policy.include_precise_recall),
            include_dynamic_memory=self._bool_value(raw_policy.get("include_dynamic_memory"), default_plan.policy.include_dynamic_memory),
            include_vision_context=self._bool_value(raw_policy.get("include_vision_context"), default_plan.policy.include_vision_context),
            include_reply_scope=self._bool_value(raw_policy.get("include_reply_scope"), default_plan.policy.include_reply_scope),
            include_style_guide=self._bool_value(raw_policy.get("include_style_guide"), default_plan.policy.include_style_guide),
        )
        return PromptPlan(
            reply_goal=self._normalize_choice(
                raw_plan.get("reply_goal"),
                allowed={"answer", "continue", "comfort", "clarify", "recall", "light_presence"},
                default=default_plan.reply_goal,
            ),
            continuity_mode=self._normalize_choice(
                raw_plan.get("continuity_mode"),
                allowed={"direct_continue", "resume_recent_topic", "resume_old_topic"},
                default=default_plan.continuity_mode,
            ),
            timeline_detail=self._normalize_choice(
                raw_plan.get("timeline_detail"),
                allowed={"off", "summary", "per_message"},
                default=default_plan.timeline_detail,
            ),
            context_profile=self._normalize_choice(
                raw_plan.get("context_profile"),
                allowed={"compact", "standard", "full"},
                default=default_plan.context_profile,
            ),
            memory_profile=self._normalize_choice(
                raw_plan.get("memory_profile"),
                allowed={"off", "facts_only", "relevant", "rich"},
                default=default_plan.memory_profile,
            ),
            tone_profile=self._normalize_choice(
                raw_plan.get("tone_profile"),
                allowed={"concise", "balanced", "warm", "deep"},
                default=default_plan.tone_profile,
            ),
            initiative=self._normalize_choice(
                raw_plan.get("initiative"),
                allowed={"reactive", "gentle_follow", "proactive_follow"},
                default=default_plan.initiative,
            ),
            expression_profile=self._normalize_choice(
                raw_plan.get("expression_profile"),
                allowed={"plain", "colloquial", "companion"},
                default=default_plan.expression_profile,
            ),
            policy=policy,
            notes=str(raw_plan.get("notes") or default_plan.notes or "").strip(),
        )

    def default_prompt_plan(
        self,
        *,
        event: MessageEvent,
        action: str,
        context: Optional[MessageContext],
    ) -> PromptPlan:
        chat_mode = str(event.message_type or "").strip().lower() or "private"
        continuity_hint = str((context.temporal_context.continuity_hint if context else "") or "unknown")
        signals = dict(getattr(context, "planning_signals", {}) or {}) if context else {}
        reply_goal = self._default_reply_goal(chat_mode=chat_mode, continuity_hint=continuity_hint, signals=signals)
        continuity_mode = {
            "strong_continuation": "direct_continue",
            "soft_continuation": "resume_recent_topic",
            "resume_after_break": "resume_recent_topic",
            "old_topic_resume": "resume_old_topic",
        }.get(continuity_hint, "direct_continue")
        timeline_detail = "summary"
        if continuity_hint == "old_topic_resume":
            timeline_detail = "per_message"
        elif continuity_hint == "unknown":
            timeline_detail = "off"
        context_profile = "compact" if chat_mode == "group" else "standard"
        if continuity_hint in {"resume_after_break", "old_topic_resume"}:
            context_profile = "full"
        memory_profile = "relevant"
        if reply_goal == "clarify" and chat_mode == "group":
            memory_profile = "off"
        elif reply_goal == "answer" and continuity_hint == "old_topic_resume":
            memory_profile = "rich"
        elif reply_goal == "comfort":
            memory_profile = "facts_only" if chat_mode == "group" else "relevant"
        elif continuity_hint == "old_topic_resume":
            memory_profile = "rich"
        tone_profile = "concise" if chat_mode == "group" else "balanced"
        if reply_goal == "comfort":
            tone_profile = "warm"
        elif continuity_hint == "old_topic_resume":
            tone_profile = "deep"
        elif reply_goal == "clarify":
            tone_profile = "concise" if chat_mode == "group" else "balanced"
        initiative = "reactive" if chat_mode == "group" else "gentle_follow"
        if reply_goal in {"continue", "recall"}:
            initiative = "gentle_follow"
        if bool(signals.get("follow_up_after_assistant")):
            initiative = "proactive_follow" if chat_mode == "private" else "gentle_follow"
        expression_profile = "plain"
        if reply_goal == "comfort":
            expression_profile = "companion"
        elif reply_goal in {"continue", "light_presence"}:
            expression_profile = "colloquial"

        should_reply = action == MessagePlanAction.REPLY.value
        policy = PromptSectionPolicy(
            include_recent_history=should_reply,
            include_person_facts=should_reply and chat_mode == "private" and memory_profile in {"facts_only", "relevant", "rich"},
            include_session_restore=should_reply and continuity_hint in {"resume_after_break", "old_topic_resume"},
            include_precise_recall=should_reply and continuity_hint == "old_topic_resume",
            include_dynamic_memory=should_reply and memory_profile in {"relevant", "rich"},
            include_vision_context=should_reply,
            include_reply_scope=should_reply,
            include_style_guide=should_reply,
        )
        return PromptPlan(
            reply_goal=reply_goal,
            continuity_mode=continuity_mode,
            timeline_detail=timeline_detail,
            context_profile=context_profile,
            memory_profile=memory_profile,
            tone_profile=tone_profile,
            initiative=initiative,
            expression_profile=expression_profile,
            policy=policy,
            notes=self._default_notes(reply_goal=reply_goal, continuity_hint=continuity_hint, chat_mode=chat_mode),
        )

    def _default_reply_goal(self, *, chat_mode: str, continuity_hint: str, signals: Dict[str, Any]) -> str:
        if bool(signals.get("care_cue_detected")):
            return "comfort"
        if bool(signals.get("follow_up_after_assistant")) or bool(signals.get("continuation_cue_detected")):
            return "continue"
        if continuity_hint == "old_topic_resume":
            return "recall"
        if chat_mode == "group":
            return "light_presence"
        return "answer"

    def _default_notes(self, *, reply_goal: str, continuity_hint: str, chat_mode: str) -> str:
        notes: list[str] = []
        if reply_goal == "comfort":
            notes.append("先轻轻接住对方，再决定是否建议。")
        if reply_goal == "clarify":
            notes.append("优先澄清，不要外延。")
        if reply_goal == "light_presence":
            notes.append("接一句就够时不要写满。")
        if continuity_hint == "old_topic_resume":
            notes.append("像自然重提旧话题，不要背资料。")
        if chat_mode == "group":
            notes.append("群聊优先轻一点，不抢话。")
        return " ".join(notes).strip()

    def _normalize_choice(self, value: Any, *, allowed: set[str], default: str) -> str:
        text = str(value or "").strip().lower()
        return text if text in allowed else default

    def _bool_value(self, value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes", "on"}:
                return True
            if lowered in {"false", "0", "no", "off"}:
                return False
        return default
