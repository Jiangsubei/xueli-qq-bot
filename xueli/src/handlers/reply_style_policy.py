from __future__ import annotations

from src.core.models import CharacterCardSnapshot, FinalStyleGuide, PromptPlan, SoftUncertaintySignal, TemporalContext


class ReplyStylePolicy:
    """Build final reply style guidance from PromptPlan V2 and runtime context."""

    def build(
        self,
        *,
        prompt_plan: PromptPlan | None,
        temporal_context: TemporalContext | None,
        chat_mode: str,
        planner_reason: str = "",
        planning_signals: dict | None = None,
        soft_uncertainty_signals: list[SoftUncertaintySignal] | None = None,
        character_card_snapshot: CharacterCardSnapshot | None = None,
    ) -> FinalStyleGuide:
        plan = prompt_plan or PromptPlan()
        signals = dict(planning_signals or {})
        uncertainty_signals = list(soft_uncertainty_signals or [])
        character_snapshot = character_card_snapshot or CharacterCardSnapshot()
        normalized_mode = str(chat_mode or "private").strip().lower() or "private"
        continuity_hint = str(getattr(temporal_context, "continuity_hint", "") or "")
        reply_goal = str(plan.reply_goal or "continue").strip().lower()
        tone_profile = str(plan.tone_profile or "balanced").strip().lower()
        initiative = str(plan.initiative or "gentle_follow").strip().lower()
        expression_profile = str(plan.expression_profile or "plain").strip().lower()

        verbosity_guidance = {
            "concise": "尽量短一点，够用就收，不要写满。",
            "balanced": "自然均衡，有回应感但不要啰嗦。",
            "warm": "可以稍微展开一点，让承接感更明显。",
            "deep": "允许适度展开，但仍然避免长篇说教。",
        }.get(tone_profile, "自然均衡，有回应感但不要啰嗦。")

        warmth_guidance = "保持自然礼貌，不要过冷。"
        if normalized_mode == "group":
            warmth_guidance = "群聊里保持轻一点的温度，不要过度投入或抢戏。"
        if reply_goal == "comfort" or bool(signals.get("care_cue_detected")):
            warmth_guidance = "先轻轻接住对方的状态，再决定是否补建议。"
        if uncertainty_signals:
            warmth_guidance += " 这次保留一点余地，别把话说得太满。"

        initiative_guidance = {
            "reactive": "优先回应当前消息本身，不主动拉长话题。",
            "gentle_follow": "可以顺着当前话题轻轻往下接半步。",
            "proactive_follow": "可以自然追问或补一小步延展，但不要变成盘问。",
        }.get(initiative, "可以顺着当前话题轻轻往下接半步。")
        if any("少一点主动追问" in item for item in character_snapshot.behavior_habits):
            initiative_guidance = "优先回应当前消息本身，谨慎追加追问。"

        tone_guidance = "口吻自然，像在正常聊天。"
        if normalized_mode == "group":
            tone_guidance = "群聊里优先轻、短、自然，不要像在发表长意见。"
        if reply_goal == "answer":
            tone_guidance = "优先把问题答清楚，别为了陪聊把答案拖散。"
        elif reply_goal == "clarify":
            tone_guidance = "优先澄清和校正，表达干净，不要外延。"
        elif reply_goal == "recall":
            tone_guidance = "像自然想起之前聊过的事，不要背档案。"
        elif reply_goal == "light_presence":
            tone_guidance = "保持存在感就够，不要抢话或总结全场。"
        elif reply_goal == "comfort":
            tone_guidance = "重点是接住情绪，少一点工具感和说教感。"

        if continuity_hint == "old_topic_resume":
            tone_guidance += " 这次像是隔了一段时间重新接上旧话题。"
        if uncertainty_signals:
            tone_guidance += " 表达更谨慎一点，像自然留有余地，而不是直接下结论。"

        expression_guidance = {
            "plain": "措辞干净自然，不要故意装饰。",
            "colloquial": "可以更口语一点，但别堆叠语气词。",
            "companion": "可以更像陪伴式续聊，但不要模板化卖萌。",
        }.get(expression_profile, "措辞干净自然，不要故意装饰。")
        if character_snapshot.tone_preferences:
            expression_guidance += f" 同时参考这些稳定偏好：{'；'.join(character_snapshot.tone_preferences)}。"
        if uncertainty_signals:
            expression_guidance += " 可以用更柔和的限定表达，但不要显得心虚。"

        anti_patterns = [
            "不要自称提示词或记忆来源",
            "不要复读大段历史原文",
            "不要用客服腔或总结报告腔",
            "不要直接说你记错了或数据库显示",
        ]
        if expression_profile == "companion":
            anti_patterns.append("不要模板化卖萌")
        if normalized_mode == "group":
            anti_patterns.append("不要抢别人的话头")
        if reply_goal == "comfort":
            anti_patterns.append("不要一上来讲道理")
        if planner_reason.strip():
            anti_patterns.append(f"不要偏离这次回复意图：{planner_reason.strip()}")

        return FinalStyleGuide(
            verbosity_guidance=verbosity_guidance,
            warmth_guidance=warmth_guidance,
            initiative_guidance=initiative_guidance,
            tone_guidance=tone_guidance,
            expression_guidance=expression_guidance,
            anti_patterns=anti_patterns,
        )
