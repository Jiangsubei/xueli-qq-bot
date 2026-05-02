from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from ..storage.markdown_store import MemoryItem, MarkdownMemoryStore

logger = logging.getLogger(__name__)


@dataclass
class ExtractionConfig:
    """Memory extraction settings."""

    extract_every_n_turns: int = 3
    max_dialogue_length: int = 10
    min_memory_quality: float = 0.7
    reflection_enabled: bool = True
    reflection_candidate_limit: int = 3
    reflection_min_topic_overlap: float = 0.45
    system_prompt: str = (
        "你现在的任务，是从一段用户和助手之间的对话里，整理出值得记住的、关于用户的信息。\n"
        "你会同时看到用户发言和助手发言，但你需要注意：助手发言只用于帮助你理解上下文，不能直接当成记忆来源。"
        "你只能提取用户自己表达过、明确确认过，或者在上下文里已经表达得足够清楚的信息。\n"
        "你要把记忆分成两类：\n"
        "重要记忆：\n"
        "用于那些长期稳定、以后长期都有明显帮助的信息。例如用户长期稳定的偏好、习惯、背景、身份关系、边界、长期计划、称呼要求，"
        "或者用户明确要求你长期记住的事情。\n"
        "普通记忆：\n"
        "用于那些不一定是长期稳定事实，但对后续陪伴式对话有明显帮助的信息。例如用户最近在做什么、最近遇到了什么事、最近在推进什么任务、"
        "最近关注什么、最近情绪或状态如何，或者接下来几轮到最近一段时间里大概率还会继续聊到的内容。\n"
        "如果用户只是说\u201c对\u201d\u201c就是这个\u201d\u201c按你刚刚说的来\u201d这种依赖上下文的话，你可以结合前面的助手发言来理解，"
        "但最后提取出来的记忆，仍然必须是关于用户的事实、状态、近况或需求，而不是助手的建议本身。\n"
        "但你仍然要控制提取质量：\n"
        "不是所有聊天内容都值得记忆。不要把寒暄、口头禅、一次性应答、没有后续价值的零碎句子、纯流水账内容提取成记忆。"
        "只有当一段内容能够概括成\u201c以后继续聊天时可能有帮助的信息\u201d时，才考虑提取。\n"
        "你会在对话里看到稳定的 turn 标记，例如 T12、T13。你输出的每一条记忆，都必须标注它来自哪个 turn，可以写成 Tn，或者 Tn-Tm。\n"
        "如果这段对话里没有值得保存的内容，你只需要输出\u201c无\u201d。\n"
        "另外，如果对话中能感受到明确的情绪基调，你可以在记忆内容之前单独输出一行 [TONE:标签] 来标记这段对话的整体情绪氛围。"
        "标签从以下选择：[开心, 喜欢, 惊讶, 无语, 委屈, 生气, 伤心, 嘲讽, 害怕, 困惑, 平静]。"
        "同一段对话可以多次改变 TONE，后面的记忆会继承最近的一个 TONE。如果不需要特别标注情绪，不输出 TONE 行即可。\n"
        "输出时不要解释，不要分析，也不要加多余的话。每条记忆单独占一行，并严格使用下面两种格式之一：\n"
        "普通记忆：[NORMAL:1-5][Tn] 用户123: 记忆内容\n"
        "普通记忆：[NORMAL:1-5][Tn-Tm] 用户123: 记忆内容\n"
        "重要记忆：[IMPORTANT][Tn] 用户123: 记忆内容\n"
        "重要记忆：[IMPORTANT][Tn-Tm] 用户123: 记忆内容\n"
        "TONE 行（可选）：[TONE:开心]\n"
        "TONE 行（可选）：[TONE:伤心]"
    )
    reflection_system_prompt: str = (
        "你是一个冷静的记忆反思器。你的任务不是生成新记忆，而是判断一条新记忆与旧记忆之间是否存在真正冲突。\n"
        "你必须严格基于提供的证据判断，不要脑补。\n"
        "如果只是时间变化、阶段性状态、场景限制、表达修正，也要明确指出，不要简单视为长期事实反转。\n"
        "你必须输出一个 JSON 对象，不要输出任何额外说明。\n"
        "JSON 字段要求：\n"
        "has_conflict: boolean，是否存在需要记录的记忆冲突或修补关系\n"
        "conflict_type: string，可选值为 none / preference_change / temporary_state / scope_specific / factual_correction / ambiguous\n"
        "action: string，可选值为 keep_both / keep_both_prefer_recent / prefer_new / prefer_existing / merge_context\n"
        "summary: string，给后续提示词使用的中性总结；如果没有冲突则为空字符串\n"
        "reason: string，说明你的判断依据\n"
        "confidence: number，0 到 1 之间\n"
    )


@dataclass
class ExtractedMemory:
    content: str
    source_turn_start: int
    source_turn_end: int
    is_important: bool = False
    importance: int = 3
    emotional_tone: str = ""


@dataclass
class LLMExtractionResponse:
    content: str
    provider: str = ""
    model: str = ""


@dataclass
class MemoryReflectionResult:
    has_conflict: bool = False
    conflict_type: str = "none"
    action: str = "keep_both"
    summary: str = ""
    reason: str = ""
    confidence: float = 0.0
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    targets: List[Dict[str, Any]] = field(default_factory=list)


class MemoryExtractor:
    """Extract and persist memories from session-scoped dialogue buffers."""

    def __init__(
        self,
        memory_store: MarkdownMemoryStore,
        llm_callback: Callable[[str, List[Dict[str, str]]], Any],
        config: Optional[ExtractionConfig] = None,
        important_memory_store: Any = None,
    ) -> None:
        self.memory_store = memory_store
        self.llm_callback = llm_callback
        self.config = config or ExtractionConfig()
        self.important_memory_store = important_memory_store

        self._session_turns: Dict[str, List[Dict[str, Any]]] = {}
        self._session_owner: Dict[str, str] = {}
        self._session_dialogue_key: Dict[str, str] = {}
        self._session_extracted_upto: Dict[str, int] = {}

        self._reflection_negative_cues = (
            "不喜欢",
            "讨厌",
            "不想",
            "不要",
            "不喝",
            "不吃",
            "不再",
            "不愿意",
            "拒绝",
            "别再",
            "不能接受",
            "没兴趣",
        )
        self._reflection_stance_terms = (
            "喜欢",
            "讨厌",
            "不喜欢",
            "不想",
            "想",
            "想要",
            "不要",
            "愿意",
            "不愿意",
            "爱",
            "不爱",
            "偏好",
            "习惯",
            "最近",
            "现在",
            "目前",
            "今天",
            "这几天",
            "暂时",
            "先",
            "喝",
            "吃",
            "用",
        )

    def add_dialogue_turn(
        self,
        user_id: str,
        user_message: str,
        assistant_message: str,
        *,
        session_id: str,
        turn_id: int,
        dialogue_key: str,
        message_type: str = "private",
        group_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> None:
        session_key = str(session_id or "").strip()
        if not session_key:
            return

        turns = self._session_turns.setdefault(session_key, [])
        if len(turns) >= 200:
            turns[:-199] = []
        self._session_owner[session_key] = str(user_id)
        self._session_dialogue_key[session_key] = str(dialogue_key or "")
        turns.append(
            {
                "turn_id": int(turn_id),
                "user": str(user_message or ""),
                "assistant": str(assistant_message or ""),
                "timestamp": datetime.now().isoformat(),
                "source_message_type": str(message_type or "private"),
                "source_group_id": str(group_id or ""),
                "source_message_id": str(message_id or ""),
                "owner_user_id": str(user_id),
                "dialogue_key": str(dialogue_key or ""),
                "session_id": session_key,
            }
        )

    def should_extract(self, session_id: str) -> bool:
        interval = max(1, int(self.config.extract_every_n_turns))
        return self.get_pending_turn_count(session_id) >= interval

    def get_turn_count(self, session_id: str) -> int:
        return len(self._session_turns.get(str(session_id or "").strip(), []))

    def get_pending_turn_count(self, session_id: str) -> int:
        return len(self._get_pending_turns(session_id))

    async def extract_memories(self, user_id: str, *, session_id: str, force: bool = False) -> List[MemoryItem]:
        """从会话pending队列中提取用户记忆。

        流程分为5个阶段：
        1. [准备] 获取待提取对话，格式化文本
        2. [LLM调用] 调用记忆提取模型
        3. [解析] 解析LLM返回，构建锚点
        4. [保存] 遍历每条记忆 → 构建元数据 → 处理反射 → 保存普通/重要记忆 → 处理patch
        5. [收尾] 标记会话checkpoint已提取，返回结果

        Args:
            user_id: 用户ID
            session_id: 会话ID
            force: 是否强制提取（当前未使用，del force保留接口）

        Returns:
            保存成功的记忆列表（MemoryItem）
        """
        del force
        session_key = str(session_id or "").strip()
        pending_turns = self._get_pending_turns(session_key)
        if not pending_turns:
            return []

        # ─────────────────────────────────────────────────────────────
        # 阶段1：准备
        # ─────────────────────────────────────────────────────────────
        # 取最近max_dialogue_length轮对话送LLM，latest_turn_id用于更新checkpoint
        visible_turns = pending_turns[-self.config.max_dialogue_length :]
        latest_turn_id = max(int(turn.get("turn_id", 0) or 0) for turn in pending_turns)
        pending_count = len(pending_turns)
        dialogue_text = self._format_dialogue(visible_turns, user_id)
        logger.info(
            "记忆提取已触发：用户=%s，会话=%s，待提取轮次=%s，送模轮次=%s",
            user_id,
            session_key,
            pending_count,
            len(visible_turns),
        )

        # 无有效用户消息时仍推进checkpoint，避免重复提取
        if dialogue_text.strip() == "无":
            self._mark_session_extracted(session_key, latest_turn_id)
            logger.info("记忆提取跳过：会话=%s 当前没有可供提取的用户消息，checkpoint 已推进到 T%s", session_key, latest_turn_id)
            return []

        # ─────────────────────────────────────────────────────────────
        # 阶段2：LLM调用
        # ─────────────────────────────────────────────────────────────
        try:
            llm_response = await self._call_llm_for_extraction(user_id=user_id, dialogue_text=dialogue_text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._is_rate_limit_error(exc):
                logger.warning("记忆提取触发限流：用户=%s，会话=%s", user_id, session_key)
                return []
            logger.error("记忆提取失败：用户=%s，会话=%s，错误=%s", user_id, session_key, exc, exc_info=True)
            return []

        provider = llm_response.provider or "unknown"
        model = llm_response.model or ""
        logger.info("记忆提取本轮使用模型：provider=%s model=%s", provider, model)

        # LLM明确回复"无记忆"时也推进checkpoint
        if self._is_explicit_no_memory_response(llm_response.content):
            self._mark_session_extracted(session_key, latest_turn_id)
            logger.info("记忆提取结果为空：用户=%s，会话=%s，checkpoint 已推进到 T%s", user_id, session_key, latest_turn_id)
            return []

        # ─────────────────────────────────────────────────────────────
        # 阶段3：解析LLM返回
        # allowed_turn_ids用于过滤锚点无效的记忆；existing_records用于冲突检测
        # ─────────────────────────────────────────────────────────────
        extracted = self._parse_extraction_response(llm_response.content)
        if not extracted:
            logger.warning(
                "记忆提取返回了内容但没有解析出有效记忆：用户=%s，会话=%s，provider=%s，checkpoint 未推进",
                user_id,
                session_key,
                provider,
            )
            return []

        allowed_turn_ids = {int(turn["turn_id"]) for turn in visible_turns}
        related_dialogue = self._build_related_dialogue_snapshot(visible_turns)
        existing_records = await self._load_existing_memory_records(user_id)
        saved_memories: List[MemoryItem] = []
        important_count = 0
        ordinary_count = 0
        valid_anchor_count = 0

        # ─────────────────────────────────────────────────────────────
        # 阶段4：遍历记忆 → 保存 + 冲突处理 + 晋升 + patch
        # ─────────────────────────────────────────────────────────────
        for item in extracted:
            # 4a. 锚点验证：记忆来源轮次必须在visible_turns范围内
            if not self._is_valid_anchor(item, allowed_turn_ids):
                logger.debug(
                    "丢弃来源锚点无效的记忆：会话=%s，锚点=%s-%s，内容=%s",
                    session_key,
                    item.source_turn_start,
                    item.source_turn_end,
                    item.content[:80],
                )
                continue

            # 4b. 提取锚点关联的对话轮次，用于构建记忆元数据
            anchor_turns = [
                turn
                for turn in visible_turns
                if item.source_turn_start <= int(turn["turn_id"]) <= item.source_turn_end
            ]
            if not anchor_turns:
                continue
            valid_anchor_count += 1

            # 4c. 构建元数据（包含来源、关联对话、情绪标签等）
            metadata = self._build_memory_metadata(
                owner_user_id=str(user_id),
                session_id=session_key,
                dialogue_key=self._session_dialogue_key.get(session_key, ""),
                anchor_turns=anchor_turns,
                related_dialogue=related_dialogue,
            )
            if item.emotional_tone:
                metadata["emotional_tone"] = item.emotional_tone

            # 4d. 冲突检测：与历史记忆比对，判断是否存在矛盾
            reflection = await self._reflect_on_memory_conflict(
                user_id=user_id,
                item=item,
                anchor_turns=anchor_turns,
                existing_records=existing_records,
            )
            # 有冲突时合并冲突信息到元数据
            if reflection and reflection.has_conflict:
                metadata = self._merge_reflection_into_metadata(
                    metadata=metadata,
                    reflection=reflection,
                    conflict_candidates=reflection.evidence,
                )

            # 4e. 保存到普通记忆库（memory_store）
            memory_type = "important" if item.is_important else "ordinary"
            importance = 5 if item.is_important else max(1, min(int(item.importance), 5))
            mem = await self.memory_store.add_memory(
                content=item.content,
                user_id=user_id,
                source=f"extraction_{session_key}",
                tags=["auto_extracted", memory_type],
                metadata={
                    "memory_type": memory_type,
                    "importance": importance,
                    "decay_exempt": item.is_important,
                    **metadata,
                },
            )
            if mem:
                saved_memories.append(mem)
                # 收集到existing_records供后续记忆比对（用于下一轮冲突检测）
                if not self._is_suppressed_memory(mem.metadata):
                    existing_records.append(
                        {
                            "id": mem.id,
                            "kind": memory_type,
                            "content": mem.content,
                            "metadata": dict(mem.metadata or {}),
                        }
                    )
                if item.is_important:
                    important_count += 1
                else:
                    ordinary_count += 1

            # 4f. 重要记忆同步写入重要记忆持久化文件
            important_memory = None
            if item.is_important:
                important_memory = await self._sync_important_memory(
                    user_id=user_id,
                    content=item.content,
                    source="extraction",
                    priority=3,
                    metadata=metadata,
                )
            # 4g. 普通记忆达到晋升条件时也写入重要记忆
            elif mem and self._should_promote_to_important(mem):
                important_memory = await self._sync_important_memory(
                    user_id=user_id,
                    content=mem.content,
                    source="promoted_from_ordinary",
                    priority=4,
                    metadata=metadata,
                )

            # 4h. 对有冲突的记忆执行patch：对历史冲突记忆打上标记
            if reflection and mem:
                successor_memory_id = str((important_memory.id if important_memory and item.is_important else mem.id) or "")
                successor_memory_type = "important" if important_memory and item.is_important else memory_type
                await self._apply_patch_merge(
                    user_id=user_id,
                    successor_memory_id=successor_memory_id,
                    successor_memory_type=successor_memory_type,
                    reflection=reflection,
                )

        # ─────────────────────────────────────────────────────────────
        # 阶段5：收尾
        # ─────────────────────────────────────────────────────────────
        if saved_memories:
            self._mark_session_extracted(session_key, latest_turn_id)
            logger.info(
                "记忆提取完成：用户=%s，会话=%s，写入=%s，重要=%s，普通=%s，checkpoint 已推进到 T%s",
                user_id,
                session_key,
                len(saved_memories),
                important_count,
                ordinary_count,
                latest_turn_id,
            )
            return saved_memories

        if valid_anchor_count == 0:
            logger.warning(
                "记忆提取解析成功但锚点全部无效：用户=%s，会话=%s，checkpoint 未推进",
                user_id,
                session_key,
            )
        else:
            logger.warning(
                "记忆提取未写入任何有效记忆：用户=%s，会话=%s，provider=%s，checkpoint 未推进",
                user_id,
                session_key,
                provider,
            )
        return []

    async def trigger_extraction(
        self,
        user_id: str,
        force: bool = False,
        *,
        session_id: Optional[str] = None,
    ) -> List[MemoryItem]:
        session_key = str(session_id or "").strip()
        if not session_key:
            return []
        if not force and not self.should_extract(session_key):
            return []
        return await self.extract_memories(user_id, session_id=session_key)

    def clear_buffer(self, *, session_id: Optional[str] = None) -> None:
        if session_id is None:
            self._session_turns.clear()
            self._session_owner.clear()
            self._session_dialogue_key.clear()
            self._session_extracted_upto.clear()
            return

        session_key = str(session_id or "").strip()
        self._session_turns.pop(session_key, None)
        self._session_owner.pop(session_key, None)
        self._session_dialogue_key.pop(session_key, None)
        self._session_extracted_upto.pop(session_key, None)

    def _get_pending_turns(self, session_id: str) -> List[Dict[str, Any]]:
        session_key = str(session_id or "").strip()
        turns = self._session_turns.get(session_key, [])
        if not turns:
            return []
        extracted_upto = int(self._session_extracted_upto.get(session_key, 0) or 0)
        return [turn for turn in turns if int(turn.get("turn_id", 0) or 0) > extracted_upto]

    def _mark_session_extracted(self, session_id: str, turn_id: int) -> None:
        session_key = str(session_id or "").strip()
        if not session_key:
            return
        current_value = int(self._session_extracted_upto.get(session_key, 0) or 0)
        self._session_extracted_upto[session_key] = max(current_value, int(turn_id or 0))

    def _is_valid_anchor(self, item: ExtractedMemory, allowed_turn_ids: set[int]) -> bool:
        if item.source_turn_start <= 0 or item.source_turn_end <= 0:
            return False
        if item.source_turn_start > item.source_turn_end:
            return False
        return set(range(item.source_turn_start, item.source_turn_end + 1)).issubset(allowed_turn_ids)

    def _build_memory_metadata(
        self,
        *,
        owner_user_id: str,
        session_id: str,
        dialogue_key: str,
        anchor_turns: List[Dict[str, Any]],
        related_dialogue: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        first_turn = anchor_turns[0]
        last_turn = anchor_turns[-1]
        message_ids = [
            str(turn.get("source_message_id") or "")
            for turn in anchor_turns
            if str(turn.get("source_message_id") or "").strip()
        ]
        return {
            "owner_user_id": owner_user_id,
            "dialogue_key": dialogue_key,
            "source_session_id": session_id,
            "source_dialogue_key": dialogue_key,
            "source_turn_start": int(first_turn["turn_id"]),
            "source_turn_end": int(last_turn["turn_id"]),
            "source_message_ids": message_ids,
            "source_message_id": message_ids[-1] if message_ids else "",
            "source_message_type": str(last_turn.get("source_message_type", "") or ""),
            "source_group_id": str(last_turn.get("source_group_id", "") or ""),
            "group_id": str(last_turn.get("source_group_id", "") or ""),
            "related_dialogue": related_dialogue,
        }

    def _build_related_dialogue_snapshot(self, turns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            {
                "turn_id": int(turn.get("turn_id", 0) or 0),
                "user": str(turn.get("user", "") or ""),
                "assistant": str(turn.get("assistant", "") or ""),
                "timestamp": str(turn.get("timestamp", "") or ""),
                "source_message_type": str(turn.get("source_message_type", "") or ""),
                "source_group_id": str(turn.get("source_group_id", "") or ""),
                "source_message_id": str(turn.get("source_message_id", "") or ""),
                "owner_user_id": str(turn.get("owner_user_id", "") or ""),
            }
            for turn in turns
        ]

    def _format_dialogue(self, dialogue: List[Dict[str, Any]], user_id: str) -> str:
        session_type = str(dialogue[-1].get("source_message_type", "") or "").strip().lower() if dialogue else ""
        session_label = "群聊" if session_type == "group" else "私聊"
        lines = [
            f"=== 用户 {user_id} 的{session_label}对话记录 ===",
            "下面这些内容同时包含用户发言和助手发言。",
            "用户发言是判断记忆的主要来源，助手发言只用于帮助理解上下文，不能直接当成记忆来源。",
            "每条前缀里的 Tn 是稳定 turn 标签，输出时必须引用它。",
            "",
        ]
        has_user_content = False

        for turn in dialogue:
            user_content = str(turn.get("user", "") or "").strip()
            assistant_content = str(turn.get("assistant", "") or "").strip()
            turn_label = f"T{int(turn.get('turn_id', 0) or 0)}"
            if user_content:
                has_user_content = True
                lines.append(f"{turn_label}: 用户{user_id}: {user_content}")
            if assistant_content:
                lines.append(f"{turn_label}: 助手: {assistant_content}")
            if user_content or assistant_content:
                lines.append("")

        if not has_user_content:
            return "无"
        return "\n".join(lines).rstrip()

    async def _call_llm_for_extraction(self, *, user_id: str, dialogue_text: str) -> LLMExtractionResponse:
        messages = [
            {
                "role": "user",
                "content": (
                    f"下面是用户 {user_id} 最近几轮和助手的对话记录。\n"
                    "这些内容里，用户发言是你判断记忆的主要来源，助手发言只是帮助你理解用户当时在回应什么。\n"
                    "你最终提取的记忆，必须是关于这个用户本人的稳定信息，不能直接照搬助手说的话。\n"
                    "如果没有值得长期保存的内容，只输出“无”。\n\n"
                    f"{dialogue_text}"
                ),
            }
        ]

        system_prompt = self.config.system_prompt.replace("用户123", f"用户{user_id}")
        response: Any = None
        for attempt in range(1, 3):
            try:
                response = await self.llm_callback(system_prompt, messages)
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._is_rate_limit_error(exc) and attempt < 2:
                    await asyncio.sleep(attempt)
                    continue
                raise

        return self._normalize_llm_response(response)

    def _normalize_llm_response(self, response: Any) -> LLMExtractionResponse:
        if isinstance(response, LLMExtractionResponse):
            return response
        if isinstance(response, str):
            return LLMExtractionResponse(content=response)
        if hasattr(response, "content"):
            return LLMExtractionResponse(content=str(response.content or ""))
        if isinstance(response, dict):
            return LLMExtractionResponse(
                content=str(response.get("content", "") or response.get("text", "") or ""),
                provider=str(response.get("provider", "") or ""),
                model=str(response.get("model", "") or ""),
            )
        return LLMExtractionResponse(content=str(response or ""))

    def _parse_extraction_response(self, content: str) -> List[ExtractedMemory]:
        memories: List[ExtractedMemory] = []
        lines = content.split("|") if "|" in content else content.splitlines()
        current_tone = ""
        for raw_line in lines:
            line = str(raw_line or "").strip()
            if not line or line.startswith("#"):
                continue
            if self._is_explicit_no_memory_response(line):
                continue

            tone_match = re.match(r"^\[TONE:([^\]]+)\]$", line, re.IGNORECASE)
            if tone_match:
                tone_label = tone_match.group(1).strip()
                valid_tones = {"开心", "喜欢", "惊讶", "无语", "委屈", "生气", "伤心", "嘲讽", "害怕", "困惑", "平静"}
                if tone_label in valid_tones:
                    current_tone = tone_label
                continue

            line = re.sub(r"^(?:普通记忆|重要记忆)\s*[：:]\s*", "", line)
            line = re.sub(r"^(?:[\-\*\u2022]\s*|\d+[\.\)、]\s*)+", "", line)
            line = re.sub(r"^(?:普通记忆|重要记忆)\s*[：:]\s*", "", line)
            line = re.sub(r"^(?:[\-\*\u2022]\s*|\d+[\.\)、)]\s*)+", "", line)

            important_match = re.match(r"^\[(?:IMPORTANT|重要)\]\[(T\d+(?:-T?\d+)?)\]\s*(.+)$", line, re.IGNORECASE)
            normal_match = re.match(r"^\[(?:NORMAL|普通):([1-5])\]\[(T\d+(?:-T?\d+)?)\]\s*(.+)$", line, re.IGNORECASE)

            is_important = False
            importance = 3
            anchor = ""
            content_text = ""

            if important_match:
                is_important = True
                importance = 5
                anchor = important_match.group(1)
                content_text = important_match.group(2)
            elif normal_match:
                importance = int(normal_match.group(1))
                anchor = normal_match.group(2)
                content_text = normal_match.group(3)
            else:
                continue

            anchor_range = self._parse_anchor(anchor)
            if anchor_range is None:
                continue

            content_text = re.sub(r"^用户\w+:\s*", "", content_text).strip()
            content_text = re.sub(
                r"^(?:用户|user)\s*[^\r\n：:]{1,40}[：:]\s*",
                "",
                content_text,
                flags=re.IGNORECASE,
            ).strip()
            if not content_text or len(content_text) <= 2:
                continue

            memories.append(
                ExtractedMemory(
                    content=content_text,
                    source_turn_start=anchor_range[0],
                    source_turn_end=anchor_range[1],
                    is_important=is_important,
                    importance=importance,
                    emotional_tone=current_tone,
                )
            )
        return memories

    def _is_explicit_no_memory_response(self, content: str) -> bool:
        text = str(content or "").strip()
        if not text:
            return False
        normalized_simple = re.sub(r"[\s`'\"“”‘’。．.!！？?、,:：;；\-\*]+", "", text).lower()
        if normalized_simple in {
            "无",
            "无可提取内容",
            "没有可提取内容",
            "暂无可提取内容",
            "none",
            "nomemory",
            "nomemories",
            "noextractablememory",
            "noextractablememories",
        }:
            return True
        normalized = re.sub(r"[\s`'\"“”‘’。．.!！？?、,:：;；\-\*]+", "", text).lower()
        return normalized in {
            "无",
            "无可提取内容",
            "没有可提取内容",
            "暂无可提取内容",
            "none",
            "nomemory",
            "nomemories",
            "noextractablememory",
            "noextractablememories",
        }

    def _parse_anchor(self, anchor: str) -> Optional[tuple[int, int]]:
        match = re.fullmatch(r"T(\d+)(?:-T?(\d+))?", str(anchor or "").strip(), re.IGNORECASE)
        if not match:
            return None
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start <= 0 or end <= 0 or start > end:
            return None
        return start, end

    async def _load_existing_memory_records(self, user_id: str) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        ordinary_memories = await self.memory_store.get_user_memories(user_id)
        for memory in ordinary_memories:
            if self._is_suppressed_memory(memory.metadata):
                continue
            records.append(
                {
                    "id": memory.id,
                    "kind": "ordinary",
                    "content": memory.content,
                    "metadata": dict(memory.metadata or {}),
                }
            )

        if self.important_memory_store:
            important_memories = await self.important_memory_store.get_memories(user_id, min_priority=1)
            for memory in important_memories:
                if self._is_suppressed_memory(memory.metadata):
                    continue
                records.append(
                    {
                        "id": memory.id,
                        "kind": "important",
                        "content": memory.content,
                        "metadata": dict(memory.metadata or {}),
                    }
                )
        return records

    async def _reflect_on_memory_conflict(
        self,
        *,
        user_id: str,
        item: ExtractedMemory,
        anchor_turns: List[Dict[str, Any]],
        existing_records: List[Dict[str, Any]],
    ) -> Optional[MemoryReflectionResult]:
        if not self.config.reflection_enabled or not existing_records:
            return None

        candidates = self._find_conflict_candidates(item.content, existing_records)
        if not candidates:
            return None

        evidence = self._build_reflection_evidence(anchor_turns=anchor_turns, candidates=candidates)
        response = await self._call_llm_for_reflection(
            user_id=user_id,
            new_memory=item.content,
            anchor_turns=anchor_turns,
            candidates=candidates,
        )
        reflection = self._parse_reflection_response(response.content)
        if reflection is None or not reflection.has_conflict:
            return None
        reflection.evidence = evidence
        reflection.targets = [
            {
                "memory_id": str(item.get("id") or ""),
                "memory_type": str(item.get("kind") or "ordinary"),
                "content": str(item.get("content") or ""),
            }
            for item in candidates
        ]
        return reflection

    def _find_conflict_candidates(self, content: str, existing_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        scored_candidates: List[Dict[str, Any]] = []
        for record in existing_records:
            existing_content = str(record.get("content") or "").strip()
            if not existing_content:
                continue
            score = self._estimate_conflict_score(content, existing_content)
            if score <= 0:
                continue
            scored_candidates.append(
                {
                    "score": score,
                    "id": str(record.get("id") or ""),
                    "kind": str(record.get("kind") or "ordinary"),
                    "content": existing_content,
                    "metadata": dict(record.get("metadata") or {}),
                }
            )
        scored_candidates.sort(key=lambda item: (item.get("score", 0.0), item.get("id", "")), reverse=True)
        return scored_candidates[: max(1, int(self.config.reflection_candidate_limit or 3))]

    def _estimate_conflict_score(self, left: str, right: str) -> float:
        normalized_left = self._normalize_reflection_text(left)
        normalized_right = self._normalize_reflection_text(right)
        if not normalized_left or not normalized_right or normalized_left == normalized_right:
            return 0.0

        topic_left = self._strip_stance_terms(normalized_left)
        topic_right = self._strip_stance_terms(normalized_right)
        overlap = self._topic_overlap(topic_left, topic_right)
        if overlap < float(self.config.reflection_min_topic_overlap or 0.45):
            return 0.0

        left_negative = self._has_negative_stance(normalized_left)
        right_negative = self._has_negative_stance(normalized_right)
        if left_negative == right_negative:
            return 0.0

        temporal_bonus = 0.0
        if any(marker in normalized_left or marker in normalized_right for marker in ("最近", "现在", "目前", "今天", "这几天", "暂时")):
            temporal_bonus = 0.1
        return min(1.0, overlap + 0.25 + temporal_bonus)

    def _normalize_reflection_text(self, text: str) -> str:
        return re.sub(r"\s+", "", str(text or "").strip().lower())

    def _strip_stance_terms(self, text: str) -> str:
        normalized = str(text or "")
        for term in self._reflection_stance_terms:
            normalized = normalized.replace(term, "")
        normalized = re.sub(r"[^\w\u4e00-\u9fff]", "", normalized)
        return normalized

    def _topic_overlap(self, left: str, right: str) -> float:
        if not left or not right:
            return 0.0
        left_chars = set(left)
        right_chars = set(right)
        if not left_chars or not right_chars:
            return 0.0
        return len(left_chars & right_chars) / min(len(left_chars), len(right_chars))

    def _has_negative_stance(self, text: str) -> bool:
        normalized = str(text or "")
        return any(cue in normalized for cue in self._reflection_negative_cues)

    def _build_reflection_evidence(
        self,
        *,
        anchor_turns: List[Dict[str, Any]],
        candidates: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        evidence: List[Dict[str, Any]] = []
        turn_start = int(anchor_turns[0].get("turn_id", 0) or 0)
        turn_end = int(anchor_turns[-1].get("turn_id", 0) or 0)
        user_quotes = [str(turn.get("user", "") or "").strip() for turn in anchor_turns if str(turn.get("user", "") or "").strip()]
        evidence.append(
            {
                "kind": "new_memory",
                "turn_range": f"T{turn_start}" if turn_start == turn_end else f"T{turn_start}-T{turn_end}",
                "source_session_id": str(anchor_turns[-1].get("session_id", "") or ""),
                "source_group_id": str(anchor_turns[-1].get("source_group_id", "") or ""),
                "quote": " / ".join(user_quotes[:3]),
            }
        )
        for candidate in candidates:
            metadata = dict(candidate.get("metadata") or {})
            evidence.append(
                {
                    "kind": "existing_memory",
                    "memory_id": str(candidate.get("id") or ""),
                    "memory_type": str(candidate.get("kind") or "ordinary"),
                    "content": str(candidate.get("content") or ""),
                    "source_session_id": str(metadata.get("source_session_id") or ""),
                    "source_turn_start": int(metadata.get("source_turn_start", 0) or 0),
                    "source_turn_end": int(metadata.get("source_turn_end", 0) or 0),
                    "source_group_id": str(metadata.get("source_group_id", metadata.get("group_id", "")) or ""),
                }
            )
        return evidence

    async def _call_llm_for_reflection(
        self,
        *,
        user_id: str,
        new_memory: str,
        anchor_turns: List[Dict[str, Any]],
        candidates: List[Dict[str, Any]],
    ) -> LLMExtractionResponse:
        anchor_lines = []
        for turn in anchor_turns:
            turn_id = int(turn.get("turn_id", 0) or 0)
            user_text = str(turn.get("user", "") or "").strip()
            if user_text:
                anchor_lines.append(f"T{turn_id}: 用户{user_id}: {user_text}")
        candidate_lines = []
        for index, candidate in enumerate(candidates, 1):
            metadata = dict(candidate.get("metadata") or {})
            source_anchor = ""
            source_turn_start = int(metadata.get("source_turn_start", 0) or 0)
            source_turn_end = int(metadata.get("source_turn_end", 0) or 0)
            if source_turn_start > 0 and source_turn_end > 0:
                source_anchor = f" T{source_turn_start}" if source_turn_start == source_turn_end else f" T{source_turn_start}-T{source_turn_end}"
            candidate_lines.append(
                f"{index}. id={candidate.get('id', '')}; kind={candidate.get('kind', 'ordinary')}; content={candidate.get('content', '')}; source_session={metadata.get('source_session_id', '')}; anchor={source_anchor.strip()}"
            )

        messages = [
            {
                "role": "user",
                "content": (
                    f"用户 {user_id} 刚提取出一条新记忆，请判断它和旧记忆是否形成需要记录的冲突或修补关系。\n\n"
                    f"新记忆：{new_memory}\n"
                    f"新记忆证据：\n" + "\n".join(anchor_lines) + "\n\n"
                    f"候选旧记忆：\n" + "\n".join(candidate_lines)
                ),
            }
        ]
        response = await self.llm_callback(self.config.reflection_system_prompt, messages)
        return self._normalize_llm_response(response)

    def _parse_reflection_response(self, content: str) -> Optional[MemoryReflectionResult]:
        payload = self._extract_json_object(content)
        if not payload:
            return None
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None

        has_conflict = bool(data.get("has_conflict", False))
        confidence_value = data.get("confidence", 0.0)
        try:
            confidence = max(0.0, min(float(confidence_value), 1.0))
        except (TypeError, ValueError):
            confidence = 0.0
        return MemoryReflectionResult(
            has_conflict=has_conflict,
            conflict_type=str(data.get("conflict_type") or "none").strip() or "none",
            action=str(data.get("action") or "keep_both").strip() or "keep_both",
            summary=str(data.get("summary") or "").strip(),
            reason=str(data.get("reason") or "").strip(),
            confidence=confidence,
        )

    def _extract_json_object(self, content: str) -> str:
        text = str(content or "").strip()
        if not text:
            return ""
        if text.startswith("{") and text.endswith("}"):
            return text
        match = re.search(r"\{.*\}", text, re.DOTALL)
        return match.group(0).strip() if match else ""

    def _merge_reflection_into_metadata(
        self,
        *,
        metadata: Dict[str, Any],
        reflection: MemoryReflectionResult,
        conflict_candidates: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        merged = dict(metadata or {})
        merged["summary"] = reflection.summary or merged.get("summary", "")
        merged["patch_status"] = self._resolve_new_memory_patch_status(reflection)
        merged["patch_action"] = reflection.action
        merged["patch_conflict_type"] = reflection.conflict_type
        merged["patch_reason"] = reflection.reason
        merged["patch_confidence"] = reflection.confidence
        merged["patch_final_summary"] = reflection.summary
        merged["patch_target_memory_ids"] = [
            str(item.get("memory_id") or "")
            for item in conflict_candidates
            if str(item.get("memory_id") or "")
        ]
        merged["reflection"] = {
            "has_conflict": True,
            "conflict_type": reflection.conflict_type,
            "action": reflection.action,
            "summary": reflection.summary,
            "reason": reflection.reason,
            "confidence": reflection.confidence,
            "evidence": conflict_candidates,
            "reflected_at": datetime.now().isoformat(),
        }
        return merged

    def _is_suppressed_memory(self, metadata: Optional[Dict[str, Any]]) -> bool:
        prepared = dict(metadata or {})
        return str(prepared.get("patch_status") or "").strip().lower() in {"superseded", "contextualized"}

    def _resolve_new_memory_patch_status(self, reflection: MemoryReflectionResult) -> str:
        action = str(reflection.action or "keep_both").strip().lower()
        if action == "prefer_existing":
            return "superseded"
        if action in {"prefer_new", "keep_both_prefer_recent", "merge_context"}:
            return "active_patch"
        return "conflict_reflected"

    def _resolve_existing_memory_patch_status(self, reflection: MemoryReflectionResult) -> str:
        action = str(reflection.action or "keep_both").strip().lower()
        if action in {"prefer_new", "keep_both_prefer_recent"}:
            return "superseded"
        if action == "merge_context":
            return "contextualized"
        return "active"

    async def _apply_patch_merge(
        self,
        *,
        user_id: str,
        successor_memory_id: str,
        successor_memory_type: str,
        reflection: Optional[MemoryReflectionResult],
    ) -> None:
        if reflection is None or not reflection.has_conflict or not successor_memory_id:
            return

        target_status = self._resolve_existing_memory_patch_status(reflection)
        if target_status == "active":
            return

        ordinary_ids = [
            str(item.get("memory_id") or "")
            for item in reflection.targets
            if str(item.get("memory_type") or "ordinary").strip().lower() != "important"
            and str(item.get("memory_id") or "")
        ]
        important_ids = [
            str(item.get("memory_id") or "")
            for item in reflection.targets
            if str(item.get("memory_type") or "").strip().lower() == "important"
            and str(item.get("memory_id") or "")
        ]

        if ordinary_ids:
            await self._apply_patch_merge_to_ordinary_memories(
                user_id=user_id,
                memory_ids=ordinary_ids,
                target_status=target_status,
                successor_memory_id=successor_memory_id,
                successor_memory_type=successor_memory_type,
                reflection=reflection,
            )
        if important_ids:
            await self._apply_patch_merge_to_important_memories(
                user_id=user_id,
                memory_ids=important_ids,
                target_status=target_status,
                successor_memory_id=successor_memory_id,
                successor_memory_type=successor_memory_type,
                reflection=reflection,
            )

    async def _apply_patch_merge_to_ordinary_memories(
        self,
        *,
        user_id: str,
        memory_ids: List[str],
        target_status: str,
        successor_memory_id: str,
        successor_memory_type: str,
        reflection: MemoryReflectionResult,
    ) -> None:
        memories = await self.memory_store.get_user_memories(user_id)
        if not memories:
            return
        changed = False
        for memory in memories:
            if memory.id not in memory_ids:
                continue
            changed = self._update_existing_memory_patch_metadata(
                memory=memory,
                target_status=target_status,
                successor_memory_id=successor_memory_id,
                successor_memory_type=successor_memory_type,
                reflection=reflection,
            ) or changed
        if changed:
            await self.memory_store.replace_user_memories(user_id, memories)

    async def _apply_patch_merge_to_important_memories(
        self,
        *,
        user_id: str,
        memory_ids: List[str],
        target_status: str,
        successor_memory_id: str,
        successor_memory_type: str,
        reflection: MemoryReflectionResult,
    ) -> None:
        if not self.important_memory_store:
            return
        memories = await self.important_memory_store.get_memories(user_id, min_priority=1)
        if not memories:
            return
        changed = False
        for memory in memories:
            if str(memory.id or "") not in memory_ids:
                continue
            changed = self._update_existing_memory_patch_metadata(
                memory=memory,
                target_status=target_status,
                successor_memory_id=successor_memory_id,
                successor_memory_type=successor_memory_type,
                reflection=reflection,
            ) or changed
        if changed:
            await self.important_memory_store.replace_memories(user_id, memories)

    def _update_existing_memory_patch_metadata(
        self,
        *,
        memory: Any,
        target_status: str,
        successor_memory_id: str,
        successor_memory_type: str,
        reflection: MemoryReflectionResult,
    ) -> bool:
        metadata = dict(getattr(memory, "metadata", {}) or {})
        previous_status = str(metadata.get("patch_status") or "").strip().lower()
        if previous_status == "superseded":
            return False
        metadata["patch_status"] = target_status
        metadata["patch_relation"] = f"{target_status}_by_newer_memory"
        metadata["patch_successor_memory_id"] = successor_memory_id
        metadata["patch_successor_memory_type"] = successor_memory_type
        metadata["patch_conflict_type"] = reflection.conflict_type
        metadata["patch_action"] = reflection.action
        metadata["patch_reason"] = reflection.reason
        metadata["patch_confidence"] = reflection.confidence
        metadata["patch_final_summary"] = reflection.summary
        metadata["patch_updated_at"] = datetime.now().isoformat()
        setattr(memory, "metadata", metadata)
        if hasattr(memory, "updated_at"):
            memory.updated_at = datetime.now().isoformat()
        return True

    def _should_promote_to_important(self, mem: MemoryItem) -> bool:
        metadata = dict(mem.metadata or {})
        memory_type = str(metadata.get("memory_type", "")).lower()
        try:
            importance = float(metadata.get("importance", 0))
        except (TypeError, ValueError):
            importance = 0.0
        try:
            mention_count = int(float(metadata.get("mention_count", 1)))
        except (TypeError, ValueError):
            mention_count = 1
        return memory_type == "ordinary" and importance >= 5 and mention_count >= 2

    async def _sync_important_memory(
        self,
        *,
        user_id: str,
        content: str,
        source: str,
        priority: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Any]:
        if not self.important_memory_store:
            return None
        try:
            return await self.important_memory_store.add_memory(
                user_id=user_id,
                content=content,
                source=source,
                priority=priority,
                metadata=dict(metadata or {}),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("同步重要记忆失败：用户=%s，错误=%s", user_id, exc)
            return None

    def _is_rate_limit_error(self, exc: Exception) -> bool:
        message = str(exc).lower()
        return "429" in message or "rate limit" in message or "rate-limited" in message
