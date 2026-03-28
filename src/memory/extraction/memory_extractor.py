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
        "你是一个对话记忆提取助手。请只根据用户消息提取值得长期保存的稳定事实。\n"
        "规则：\n"
        "- 只提取用户自己说过的信息，不要把助手回复当成记忆。\n"
        "- 优先提取稳定偏好、背景、边界、长期计划、称呼要求。\n"
        "- 重要记忆只用于明确要求长期记住、硬性约束、长期身份关系等高优先级信息。\n"
        "- 群聊内容默认不要提取为重要记忆，除非用户明确要求长期记住。\n"
        "- 输入中的历史记录只包含用户消息，每条前面都有稳定 turn 标签，例如 T12。\n"
        "- 你必须为每条记忆标注来源 turn，格式为 Tn 或 Tn-Tm。\n"
        "输出要求：\n"
        "- 每行一条。\n"
        "- 普通记忆格式：[NORMAL:1-5][Tn] 用户123: 记忆内容\n"
        "- 重要记忆格式：[IMPORTANT][Tn-Tm] 用户123: 记忆内容\n"
        "- 如果没有可提取内容，只输出“无”。"
    )


@dataclass
class ExtractedMemory:
    content: str
    source_turn_start: int
    source_turn_end: int
    is_important: bool = False
    importance: int = 3


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
        session_key = str(session_id or "").strip()
        pending_turns = self._get_pending_turns(session_key)
        if not pending_turns:
            return []

        visible_turns = pending_turns[-self.config.max_dialogue_length :]
        latest_turn_id = max(int(turn.get("turn_id", 0) or 0) for turn in pending_turns)
        dialogue_text = self._format_dialogue(visible_turns, user_id)
        if dialogue_text.strip() == "无":
            self._mark_session_extracted(session_key, latest_turn_id)
            return []

        try:
            extracted = await self._call_llm_for_extraction(user_id=user_id, dialogue_text=dialogue_text)
        except Exception as exc:
            if self._is_rate_limit_error(exc):
                logger.warning("记忆提取触发限流：用户=%s，会话=%s", user_id, session_key)
                return []
            logger.error("记忆提取失败：用户=%s，会话=%s，错误=%s", user_id, session_key, exc, exc_info=True)
            return []

        self._mark_session_extracted(session_key, latest_turn_id)
        allowed_turn_ids = {int(turn["turn_id"]) for turn in visible_turns}
        related_dialogue = self._build_related_dialogue_snapshot(visible_turns)
        saved_memories: List[MemoryItem] = []
        important_count = 0
        ordinary_count = 0

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

        logger.info(
            "记忆提取完成：用户=%s，会话=%s，写入=%s，重要=%s，普通=%s",
            user_id,
            session_key,
            len(saved_memories),
            important_count,
            ordinary_count,
        )
        return saved_memories

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
        return [
            turn
            for turn in turns
            if int(turn.get("turn_id", 0) or 0) > extracted_upto
        ]

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
        lines = [
            f"=== 用户 {user_id} 的消息记录 ===",
            "以下内容只包含用户消息。每条前缀里的 Tn 是稳定 turn 标签，输出时必须引用它。",
            "",
        ]
        has_user_content = False

        for turn in dialogue:
            user_content = str(turn.get("user", "") or "").strip()
            if not user_content:
                continue
            has_user_content = True
            lines.append(f"T{int(turn.get('turn_id', 0) or 0)}: {user_content}")

        if not has_user_content:
            return "无"
        return "\n".join(lines)

    async def _call_llm_for_extraction(self, *, user_id: str, dialogue_text: str) -> List[ExtractedMemory]:
        messages = [
            {
                "role": "user",
                "content": (
                    "请分析下面这些用户消息，提取值得长期保存的记忆。\n"
                    "注意：这些历史记录只包含用户消息，不包含助手回复。\n"
                    "你必须给每条记忆标注来源 turn 标签，格式只能是 Tn 或 Tn-Tm。\n"
                    "如果没有可提取内容，只输出“无”。\n\n"
                    f"{dialogue_text}"
                ),
            }
        ]

        system_prompt = self.config.system_prompt.replace("用户123", f"用户{user_id}")
        response = None
        for attempt in range(1, 3):
            try:
                response = await self.llm_callback(system_prompt, messages)
                break
            except Exception as exc:
                if self._is_rate_limit_error(exc) and attempt < 2:
                    await asyncio.sleep(attempt)
                    continue
                raise

        content = ""
        if isinstance(response, str):
            content = response
        elif hasattr(response, "content"):
            content = str(response.content or "")
        elif isinstance(response, dict):
            content = str(response.get("content", "") or response.get("text", "") or "")

        return self._parse_extraction_response(content)

    def _parse_extraction_response(self, content: str) -> List[ExtractedMemory]:
        memories: List[ExtractedMemory] = []
        for raw_line in (content.split("|") if "|" in content else content.splitlines()):
            line = str(raw_line or "").strip()
            if not line or line.startswith("#"):
                continue
            if line == "无":
                continue
            line = re.sub(r"^[\-\*\u2022\s]+", "", line)

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
