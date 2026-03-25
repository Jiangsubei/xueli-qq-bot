"""
记忆提取与更新。

异步调用大模型，从对话中提取长期事实，并区分普通记忆与重要记忆。
"""
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
    """记忆提取配置。"""

    extract_every_n_turns: int = 3
    max_dialogue_length: int = 10
    min_memory_quality: float = 0.7
    system_prompt: str = """你是一个对话记忆提取助手。请分析对话，并提取值得长期保存的用户事实。
核心规则：
- 只提取用户明确表达过的信息，不要提取助手回复内容。
- 只保留关于用户自身的稳定事实、偏好、背景、关系和计划。
- 宁可少提取，也不要把普通聊天内容误判成重要记忆。

重要记忆只允许以下情况：
- 用户明确要求你长期记住的事。
- 如果用户是在要求你“记住”“记得”“别忘了”某件事，或明确表达“以后都按这个来”“之后一直这样”，通常应直接判为重要记忆。
- 明确的硬约束、禁忌、过敏、雷区、称呼要求。
- 稳定且高优先级的身份背景、关键关系、长期承诺。

以下内容不要判成重要记忆：
- 普通兴趣爱好。
- 一般口味偏好。
- 临时计划、阶段性想法。
- 没有明确长期价值的聊天细节。

普通记忆需要给出重要程度：
- 1 分：很弱的参考信息，容易被遗忘。
- 2 分：较弱偏好或偶发事实。
- 3 分：一般稳定信息。
- 4 分：较强偏好或较有价值的背景。
- 5 分：接近重要记忆，但还不到硬约束级别。

输出要求：
- 每行一条。
- 严格使用以下格式之一：
  [IMPORTANT] 用户[用户ID]: 记忆内容
  [NORMAL:1-5] 用户[用户ID]: 记忆内容
- 如果没有可提取内容，只输出“无”。

示例：
[IMPORTANT] 用户123456: 对花生严重过敏
[IMPORTANT] 用户123456: 要求你长期记住不要叫他全名
[NORMAL:4] 用户123456: 很喜欢黑咖啡且长期如此
[NORMAL:2] 用户123456: 最近在补番"""


@dataclass
class ExtractedMemory:
    """提取后的记忆项。"""

    content: str
    is_important: bool = False
    importance: int = 3


class MemoryExtractor:
    """从对话中提取并保存长期记忆。"""

    def __init__(
        self,
        memory_store: MarkdownMemoryStore,
        llm_callback: Callable[[str, List[Dict[str, str]]], Any],
        config: Optional[ExtractionConfig] = None,
        important_memory_store: Any = None,
    ):
        self.memory_store = memory_store
        self.llm_callback = llm_callback
        self.config = config or ExtractionConfig()
        self.important_memory_store = important_memory_store

        self._dialogue_buffer: Dict[str, List[Dict[str, str]]] = {}
        self._turn_counter: Dict[str, int] = {}

    def add_dialogue_turn(self, user_id: str, user_message: str, assistant_message: str):
        """向提取缓冲区添加一轮对话。"""
        if user_id not in self._dialogue_buffer:
            self._dialogue_buffer[user_id] = []
            self._turn_counter[user_id] = 0
            logger.debug("初始化记忆提取缓冲区: user=%s", user_id)

        self._dialogue_buffer[user_id].append(
            {
                "turn": self._turn_counter[user_id] + 1,
                "user": user_message,
                "assistant": assistant_message,
                "timestamp": datetime.now().isoformat(),
            }
        )

        max_len = self.config.max_dialogue_length * 2
        if len(self._dialogue_buffer[user_id]) > max_len:
            self._dialogue_buffer[user_id] = self._dialogue_buffer[user_id][-max_len:]

        self._turn_counter[user_id] += 1
        logger.debug("记录对话轮次: user=%s, 当前轮数=%s", user_id, self._turn_counter[user_id])

    def should_extract(self, user_id: str) -> bool:
        """判断当前是否应该执行提取。"""
        count = self._turn_counter.get(user_id, 0)
        should = count > 0 and count % self.config.extract_every_n_turns == 0
        logger.debug(
            "检查提取条件: user=%s, 轮数=%s, 阈值=%s, 触发=%s",
            user_id,
            count,
            self.config.extract_every_n_turns,
            should,
        )
        return should

    async def extract_memories(self, user_id: str) -> List[MemoryItem]:
        """执行提取并持久化结果。"""
        buffer = self._dialogue_buffer.get(user_id, [])
        if not buffer:
            return []

        dialogue_text = self._format_dialogue(
            buffer[-self.config.max_dialogue_length :],
            user_id,
        )

        try:
            extracted = await self._call_llm_for_extraction(user_id, dialogue_text)
            related_dialogue = self._build_related_dialogue_snapshot(user_id)

            saved_memories: List[MemoryItem] = []
            important_count = 0
            ordinary_count = 0

            for item in extracted:
                content = item.content.strip()
                if not content or content == "无":
                    continue

                memory_type = "important" if item.is_important else "ordinary"
                ordinary_importance = 5 if item.is_important else max(1, min(item.importance, 5))

                mem = await self.memory_store.add_memory(
                    content=content,
                    user_id=user_id,
                    source=f"extraction_{user_id}",
                    tags=["auto_extracted", memory_type],
                    metadata={
                        "memory_type": memory_type,
                        "importance": ordinary_importance,
                        "decay_exempt": item.is_important,
                        "related_dialogue": related_dialogue,
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
                        content=content,
                        source="extraction",
                        priority=3,
                    )
                elif mem and self._should_promote_to_important(mem):
                    await self._sync_important_memory(
                        user_id=user_id,
                        content=mem.content,
                        source="promoted_from_ordinary",
                        priority=4,
                    )

            logger.info(
                "记忆提取完成: user=%s, 新增=%s, 重要=%s, 普通=%s",
                user_id,
                len(saved_memories),
                important_count,
                ordinary_count,
            )
            return saved_memories
        except Exception as exc:
            if self._is_rate_limit_error(exc):
                logger.warning("记忆提取跳过: user=%s, 原因=上游限流: %s", user_id, exc)
                return []
            logger.error("记忆提取失败: user=%s, 错误=%s", user_id, exc, exc_info=True)
            return []

    def _should_promote_to_important(self, mem: MemoryItem) -> bool:
        """Promote ordinary memories once a max-importance fact is mentioned again."""
        metadata = mem.metadata or {}
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
        user_id: str,
        content: str,
        source: str,
        priority: int,
    ) -> None:
        """Best-effort sync to important memory store."""
        if not self.important_memory_store:
            return

        try:
            await self.important_memory_store.add_memory(
                user_id=user_id,
                content=content,
                source=source,
                priority=priority,
            )
            logger.debug("重要记忆已同步: user=%s, 内容=%s", user_id, content[:40])
        except Exception as exc:
            logger.warning("重要记忆同步失败: user=%s, 错误=%s", user_id, exc)

    def _format_dialogue(self, dialogue: List[Dict[str, str]], user_id: str) -> str:
        """将对话历史格式化为仅包含用户发言的分析文本。"""
        lines = [
            f"=== 用户 {user_id} 的发言记录 ===",
            "以下内容仅包含用户发送的消息。",
            "",
        ]
        has_user_content = False

        for i, turn in enumerate(dialogue, 1):
            user_content = (turn.get("user") or "").strip()
            if not user_content:
                continue

            has_user_content = True
            turn_number = turn.get("turn", i)
            lines.append(f"第{turn_number}轮用户: {user_content}")

        if not has_user_content:
            lines.append("无")

        return "\n".join(lines)

    def _build_related_dialogue_snapshot(self, user_id: str) -> List[Dict[str, str]]:
        """为提取出的记忆保留最近若干轮关联对话。"""
        buffer = self._dialogue_buffer.get(user_id, [])
        snapshot = []

        for turn in buffer[-self.config.max_dialogue_length :]:
            snapshot.append(
                {
                    "turn": turn.get("turn"),
                    "user": turn.get("user", ""),
                    "assistant": turn.get("assistant", ""),
                    "timestamp": turn.get("timestamp", ""),
                }
            )

        return snapshot

    async def _call_llm_for_extraction(
        self,
        user_id: str,
        dialogue_text: str,
    ) -> List[ExtractedMemory]:
        """调用大模型执行记忆提取。"""
        messages = [
            {
                "role": "user",
                "content": (
                    "请分析以下对话，提取值得长期保存的记忆事实。\n"
                    "注意：下方内容已经过滤为仅包含用户发送的消息。\n"
                    "重要记忆的判定要非常严格，只有硬约束、长期明确要求、关键身份关系才算重要。\n"
                    "如果用户是在要求你记住某件事，比如让你记住、记得、别忘了，或明确要求以后一直按某个规则执行，通常应直接标为 IMPORTANT。\n"
                    "普通记忆请给出 1-5 的重要程度。\n"
                    "输出格式必须严格为：\n"
                    f"[IMPORTANT] 用户{user_id}: 记忆内容\n"
                    f"[NORMAL:1-5] 用户{user_id}: 记忆内容\n"
                    "如果没有可提取内容，只输出“无”。\n\n"
                    f"{dialogue_text}"
                ),
            }
        ]

        system_prompt = self.config.system_prompt.replace("123456", user_id)

        logger.info("调用记忆提取模型: user=%s, 对话长度=%s", user_id, len(dialogue_text))
        response = None
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            try:
                response = await self.llm_callback(system_prompt, messages)
                break
            except Exception as exc:
                if self._is_rate_limit_error(exc) and attempt < max_attempts:
                    delay_seconds = attempt
                    logger.warning(
                        "记忆提取请求遇到限流: user=%s, attempt=%s/%s, %ss 后重试",
                        user_id,
                        attempt,
                        max_attempts,
                        delay_seconds,
                    )
                    await asyncio.sleep(delay_seconds)
                    continue
                raise

        content = ""
        if isinstance(response, str):
            content = response
        elif hasattr(response, "content"):
            content = response.content
        elif isinstance(response, dict):
            content = response.get("content", "") or response.get("text", "")

        preview = content[:300].replace("\n", " | ")
        logger.debug("记忆提取响应: user=%s, 预览=%s", user_id, preview)

        memories: List[ExtractedMemory] = []
        parts = content.split("|") if "|" in content else content.split("\n")

        for part in parts:
            line = part.strip()
            if not line or line.startswith("#"):
                continue

            line = re.sub(r"^[\-\*\u2022\s]+", "", line)

            is_important = False
            importance = 3

            important_match = re.match(r"^\[(?:IMPORTANT|重要)\]\s*", line, re.IGNORECASE)
            ordinary_match = re.match(r"^\[(?:NORMAL|普通):(\d)\]\s*", line, re.IGNORECASE)

            if important_match:
                is_important = True
                importance = 5
                line = re.sub(r"^\[(?:IMPORTANT|重要)\]\s*", "", line, flags=re.IGNORECASE)
            elif ordinary_match:
                importance = int(ordinary_match.group(1))
                line = re.sub(r"^\[(?:NORMAL|普通):(\d)\]\s*", "", line, flags=re.IGNORECASE)

            line = re.sub(r"^用户\w+:\s*", "", line)

            if (
                line
                and line != "无"
                and len(line) > 5
                and not line.startswith("以下是")
                and not line.startswith("根据")
                and not line.startswith("提取")
                and not line.startswith("分析")
            ):
                memories.append(
                    ExtractedMemory(
                        content=line,
                        is_important=is_important,
                        importance=importance,
                    )
                )
                logger.debug(
                    "识别记忆: 类型=%s, 重要度=%s, 内容=%s",
                    "重要" if is_important else "普通",
                    importance,
                    line[:40],
                )

        return memories

    def _is_rate_limit_error(self, exc: Exception) -> bool:
        """Best-effort detection for upstream rate limit failures."""
        message = str(exc).lower()
        return "429" in message or "rate limit" in message or "rate-limited" in message

    async def trigger_extraction(self, user_id: str, force: bool = False) -> List[MemoryItem]:
        """供外部主动触发记忆提取。"""
        if not force and not self.should_extract(user_id):
            return []
        return await self.extract_memories(user_id)

    def clear_buffer(self, user_id: Optional[str] = None):
        """清空对话缓冲区。"""
        if user_id:
            self._dialogue_buffer.pop(user_id, None)
            self._turn_counter.pop(user_id, None)
            logger.debug("已清空提取缓冲区: user=%s", user_id)
        else:
            self._dialogue_buffer.clear()
            self._turn_counter.clear()
            logger.debug("已清空全部提取缓冲区")
