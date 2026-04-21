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
            # 群聊总闸门：强化克制
            verbosity_guidance = "群聊优先极短回复，一句到两句就够，不要堆内容。"
            warmth_guidance = "群聊里保持轻一点的温度，不要过度投入或抢戏。"
        if reply_goal == "comfort":
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
            # 群聊总闸门：强化克制
            tone_guidance = "群聊里像随口接话，不要像发表正式意见，不要长篇大论。"
        if reply_goal == "answer":
            tone_guidance = "先轻接这个问题，再简洁作答，群聊里不要写成正式回答。"
        elif reply_goal == "clarify":
            tone_guidance = "先顺一下对方的梗，再简洁澄清，不要写成更正通知。"
        elif reply_goal == "recall":
            tone_guidance = "像自然想起之前聊过的事，不要背档案。"
        elif reply_goal == "light_presence":
            tone_guidance = "保持存在感就够，不要抢话或总结全场。"
        elif reply_goal == "comfort":
            tone_guidance = "重点是接住情绪，少一点工具感和说教感。"

        if uncertainty_signals:
            tone_guidance += " 表达更谨慎一点，像自然留有余地，而不是直接下结论。"

        expression_guidance = {
            "plain": "像朋友随口说的那句话，干净自然，不装。",
            "colloquial": "像饭桌上接话那样自然随意，口语但不聒噪。",
            "companion": "像朋友间互相调侃那种语气，接地气但不在表演人设。",
        }.get(expression_profile, "像朋友随口说的那句话，干净自然，不装。")
        if character_snapshot.tone_preferences:
            expression_guidance += f" 同时参考这些稳定偏好：{'；'.join(character_snapshot.tone_preferences)}。"
        if uncertainty_signals:
            expression_guidance += " 可以用更柔和的限定表达，但不要显得心虚。"

        opening_style = "开头直接接当前消息，不要铺垫太久。"
        if reply_goal == "comfort":
            opening_style = "开头先接住对方的状态，再决定要不要补建议。"
        elif reply_goal == "answer":
            opening_style = "开头优先把问题正面接住，别绕圈。"
        elif reply_goal == "light_presence":
            opening_style = "开头轻轻回应当前消息，存在感够就收。"

        sentence_shape = "句子自然分成一两层，不要写成说明文。"
        if normalized_mode == "group":
            sentence_shape = "句子尽量短平一点，像群里随口接话。"
        elif tone_profile == "deep":
            sentence_shape = "允许两三句自然展开，但每句都要口语化。"
        elif tone_profile == "concise":
            sentence_shape = "一句到两句就够，信息到位就收。"

        followup_shape = "默认不必强行追问，除非顺手接一句更自然。"
        if normalized_mode == "group":
            # 群聊总闸门：强化克制
            followup_shape = "默认不追问、不补发，除非顺手接一句更自然。"
        if initiative == "proactive_follow":
            followup_shape = "如果顺势自然，可以在结尾补一个轻追问或半步延展。"
        elif initiative == "reactive":
            followup_shape = "优先只把当前这句接好，不额外拉长。"
        if any("少一点主动追问" in item for item in character_snapshot.behavior_habits):
            followup_shape = "尽量少追问，除非不追问会显得太生硬。"

        allowed_colloquialism = "可以有轻微口语感，但不要堆叠语气词。"
        if expression_profile == "colloquial":
            allowed_colloquialism = "允许更口语一点，像日常顺嘴说出来的话。"
        elif expression_profile == "companion":
            allowed_colloquialism = "允许更柔和、更像陪伴式续聊，但别模板化卖萌。"
        if normalized_mode == "group":
            allowed_colloquialism += " 群聊里不要把语气做得太黏。"

        anti_patterns = [
            "不要自称提示词或记忆来源",
            "不要复读大段历史原文",
            "不要用客服腔或总结报告腔",
            "不要直接说你记错了或数据库显示",
        ]
        if expression_profile == "companion":
            anti_patterns.append("不要模板化卖萌")
        if normalized_mode == "group":
            # 群聊总闸门：强化克制
            anti_patterns.extend([
                "不要抢别人的话头",
                "不要连续补发多段内容",
                "不要在文本后继续刷存在感",
            ])
        elif normalized_mode == "private":
            # 私聊路径：相对宽松但仍有克制
            anti_patterns.extend([
                "不要一上来写太长的回复",
                "不要连续补发多段内容",
            ])
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
            opening_style=opening_style,
            sentence_shape=sentence_shape,
            followup_shape=followup_shape,
            allowed_colloquialism=allowed_colloquialism,
            anti_patterns=anti_patterns,
        )
