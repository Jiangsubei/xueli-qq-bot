from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from src.core.models import MessageEvent, MessageHandlingPlan, MessagePlanAction, PromptLayerPolicy, PromptPlan
from src.handlers.message_context import MessageContext


@dataclass
class PromptPlanner:
    """Build default prompt plans and parse planner-provided prompt policies."""

    def decision_output_schema(self) -> str:
        return (
            '{"action":"reply|wait|ignore","reason":"简短理由","prompt_plan":{'
            '"continuity_mode":"direct_continue|resume_recent_topic|resume_old_topic|memory_query|casual_chat|clarification",'
            '"temporal_mode":"off|light|explicit",'
            '"reply_style":"concise|normal|deep",'
            '"context_budget":"low|normal|high",'
            '"restore_intensity":"off|light|normal|high",'
            '"recall_intensity":"off|light|normal|high",'
            '"dynamic_intensity":"off|light|normal|high",'
            '"policy":{'
            '"enable_temporal_context":true,'
            '"enable_recent_context":true,'
            '"enable_person_facts":true,'
            '"enable_session_restore":true,'
            '"enable_precise_recall":true,'
            '"enable_dynamic_memory":true,'
            '"enable_reply_scope":true},'
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
        policy = PromptLayerPolicy(
            enable_temporal_context=self._bool_value(raw_policy.get("enable_temporal_context"), default_plan.policy.enable_temporal_context),
            enable_recent_context=self._bool_value(raw_policy.get("enable_recent_context"), default_plan.policy.enable_recent_context),
            enable_person_facts=self._bool_value(raw_policy.get("enable_person_facts"), default_plan.policy.enable_person_facts),
            enable_session_restore=self._bool_value(raw_policy.get("enable_session_restore"), default_plan.policy.enable_session_restore),
            enable_precise_recall=self._bool_value(raw_policy.get("enable_precise_recall"), default_plan.policy.enable_precise_recall),
            enable_dynamic_memory=self._bool_value(raw_policy.get("enable_dynamic_memory"), default_plan.policy.enable_dynamic_memory),
            enable_reply_scope=self._bool_value(raw_policy.get("enable_reply_scope"), default_plan.policy.enable_reply_scope),
        )
        return PromptPlan(
            continuity_mode=self._normalize_choice(
                raw_plan.get("continuity_mode"),
                allowed={"direct_continue", "resume_recent_topic", "resume_old_topic", "memory_query", "casual_chat", "clarification"},
                default=default_plan.continuity_mode,
            ),
            temporal_mode=self._normalize_choice(
                raw_plan.get("temporal_mode"),
                allowed={"off", "light", "explicit"},
                default=default_plan.temporal_mode,
            ),
            reply_style=self._normalize_choice(
                raw_plan.get("reply_style"),
                allowed={"concise", "normal", "deep"},
                default=default_plan.reply_style,
            ),
            context_budget=self._normalize_choice(
                raw_plan.get("context_budget"),
                allowed={"low", "normal", "high"},
                default=default_plan.context_budget,
            ),
            restore_intensity=self._normalize_choice(
                raw_plan.get("restore_intensity"),
                allowed={"off", "light", "normal", "high"},
                default=default_plan.restore_intensity,
            ),
            recall_intensity=self._normalize_choice(
                raw_plan.get("recall_intensity"),
                allowed={"off", "light", "normal", "high"},
                default=default_plan.recall_intensity,
            ),
            dynamic_intensity=self._normalize_choice(
                raw_plan.get("dynamic_intensity"),
                allowed={"off", "light", "normal", "high"},
                default=default_plan.dynamic_intensity,
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
        continuity_mode = {
            "strong_continuation": "direct_continue",
            "soft_continuation": "resume_recent_topic",
            "resume_after_break": "resume_recent_topic",
            "old_topic_resume": "resume_old_topic",
        }.get(continuity_hint, "casual_chat" if chat_mode == "group" else "direct_continue")
        temporal_mode = "light"
        if continuity_hint in {"resume_after_break", "old_topic_resume"}:
            temporal_mode = "explicit"
        elif continuity_hint == "unknown":
            temporal_mode = "off"

        should_reply = action == MessagePlanAction.REPLY.value
        restore_intensity = "high" if continuity_hint == "old_topic_resume" else ("normal" if continuity_hint == "resume_after_break" else "off")
        recall_intensity = "normal" if continuity_hint == "old_topic_resume" else "off"
        dynamic_intensity = "normal" if should_reply else "off"
        policy = PromptLayerPolicy(
            enable_temporal_context=should_reply and temporal_mode != "off",
            enable_recent_context=should_reply,
            enable_person_facts=should_reply and chat_mode == "private",
            enable_session_restore=should_reply and restore_intensity != "off",
            enable_precise_recall=should_reply and recall_intensity != "off",
            enable_dynamic_memory=should_reply,
            enable_reply_scope=should_reply and chat_mode == "group" and continuity_hint not in {"resume_after_break", "old_topic_resume"},
        )
        return PromptPlan(
            continuity_mode=continuity_mode,
            temporal_mode=temporal_mode,
            reply_style="normal",
            context_budget="high" if continuity_hint == "old_topic_resume" else "normal",
            restore_intensity=restore_intensity,
            recall_intensity=recall_intensity,
            dynamic_intensity=dynamic_intensity,
            policy=policy,
        )

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
