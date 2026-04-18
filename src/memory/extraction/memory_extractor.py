from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
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
        "如果用户只是说“对”“就是这个”“按你刚刚说的来”这种依赖上下文的话，你可以结合前面的助手发言来理解，"
        "但最后提取出来的记忆，仍然必须是关于用户的事实、状态、近况或需求，而不是助手的建议本身。\n"
        "但你仍然要控制提取质量：\n"
        "不是所有聊天内容都值得记忆。不要把寒暄、口头禅、一次性应答、没有后续价值的零碎句子、纯流水账内容提取成记忆。"
        "只有当一段内容能够概括成“以后继续聊天时可能有帮助的信息”时，才考虑提取。\n"
        "你会在对话里看到稳定的 turn 标记，例如 T12、T13。你输出的每一条记忆，都必须标注它来自哪个 turn，可以写成 Tn，或者 Tn-Tm。\n"
        "如果这段对话里没有值得保存的内容，你只需要输出“无”。\n"
        "输出时不要解释，不要分析，也不要加多余的话。每条记忆单独占一行，并严格使用下面两种格式之一：\n"
        "普通记忆：[NORMAL:1-5][Tn] 用户123: 记忆内容\n"
        "普通记忆：[NORMAL:1-5][Tn-Tm] 用户123: 记忆内容\n"
        "重要记忆：[IMPORTANT][Tn] 用户123: 记忆内容\n"
        "重要记忆：[IMPORTANT][Tn-Tm] 用户123: 记忆内容"
    )


@dataclass
class ExtractedMemory:
    content: str
    source_turn_start: int
    source_turn_end: int
    is_important: bool = False
    importance: int = 3


@dataclass
class LLMExtractionResponse:
    content: str
    provider: str = ""
    model: str = ""


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
        del force
        session_key = str(session_id or "").strip()
        pending_turns = self._get_pending_turns(session_key)
        if not pending_turns:
            return []

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
        if dialogue_text.strip() == "无":
            self._mark_session_extracted(session_key, latest_turn_id)
            logger.info("记忆提取跳过：会话=%s 当前没有可供提取的用户消息，checkpoint 已推进到 T%s", session_key, latest_turn_id)
            return []

        try:
            llm_response = await self._call_llm_for_extraction(user_id=user_id, dialogue_text=dialogue_text)
        except Exception as exc:
            if self._is_rate_limit_error(exc):
                logger.warning("记忆提取触发限流：用户=%s，会话=%s", user_id, session_key)
                return []
            logger.error("记忆提取失败：用户=%s，会话=%s，错误=%s", user_id, session_key, exc, exc_info=True)
            return []

        provider = llm_response.provider or "unknown"
        model = llm_response.model or ""
        logger.info("记忆提取本轮使用模型：provider=%s model=%s", provider, model)

        if self._is_explicit_no_memory_response(llm_response.content):
            self._mark_session_extracted(session_key, latest_turn_id)
            logger.info("记忆提取结果为空：用户=%s，会话=%s，checkpoint 已推进到 T%s", user_id, session_key, latest_turn_id)
            return []

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
        saved_memories: List[MemoryItem] = []
        important_count = 0
        ordinary_count = 0
        valid_anchor_count = 0

        for item in extracted:
            if not self._is_valid_anchor(item, allowed_turn_ids):
                logger.debug(
                    "丢弃来源锚点无效的记忆：会话=%s，锚点=%s-%s，内容=%s",
                    session_key,
                    item.source_turn_start,
                    item.source_turn_end,
                    item.content[:80],
                )
                continue

            anchor_turns = [
                turn
                for turn in visible_turns
                if item.source_turn_start <= int(turn["turn_id"]) <= item.source_turn_end
            ]
            if not anchor_turns:
                continue
            valid_anchor_count += 1

            metadata = self._build_memory_metadata(
                owner_user_id=str(user_id),
                session_id=session_key,
                dialogue_key=self._session_dialogue_key.get(session_key, ""),
                anchor_turns=anchor_turns,
                related_dialogue=related_dialogue,
            )
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
                if item.is_important:
                    important_count += 1
                else:
                    ordinary_count += 1

            if item.is_important:
                await self._sync_important_memory(
                    user_id=user_id,
                    content=item.content,
                    source="extraction",
                    priority=3,
                    metadata=metadata,
                )
            elif mem and self._should_promote_to_important(mem):
                await self._sync_important_memory(
                    user_id=user_id,
                    content=mem.content,
                    source="promoted_from_ordinary",
                    priority=4,
                    metadata=metadata,
                )

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
        for raw_line in lines:
            line = str(raw_line or "").strip()
            if not line or line.startswith("#"):
                continue
            if self._is_explicit_no_memory_response(line):
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
    ) -> None:
        if not self.important_memory_store:
            return
        try:
            await self.important_memory_store.add_memory(
                user_id=user_id,
                content=content,
                source=source,
                priority=priority,
                metadata=dict(metadata or {}),
            )
        except Exception as exc:
            logger.warning("同步重要记忆失败：用户=%s，错误=%s", user_id, exc)

    def _is_rate_limit_error(self, exc: Exception) -> bool:
        message = str(exc).lower()
        return "429" in message or "rate limit" in message or "rate-limited" in message
