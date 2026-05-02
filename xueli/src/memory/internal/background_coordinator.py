from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Any

from src.core.prompt_templates import PromptTemplateLoader
from src.memory.extraction.memory_extractor import MemoryExtractor
from src.memory.chat_summary_service import ChatSummaryService
from src.memory.person_fact_service import PersonFactService
from src.memory.storage.conversation_store import ConversationStore
from src.memory.storage.markdown_store import MarkdownMemoryStore, MemoryItem
from src.memory.storage.important_memory_store import ImportantMemoryItem, ImportantMemoryStore

from .task_manager import MemoryTaskManager

logger = logging.getLogger(__name__)

_template_loader = PromptTemplateLoader()


def _insight_system_prompt() -> str:
    return _template_loader.load("insight_digestion.prompt")


class MemoryBackgroundCoordinator:
    """Schedule conversation saves and memory extraction around session files."""

    def __init__(
        self,
        *,
        conversation_store: ConversationStore,
        extractor: Optional[MemoryExtractor],
        task_manager: MemoryTaskManager,
        auto_extract_memory: bool,
        on_memory_changed: Callable[[str], None],
        summary_service: Optional[ChatSummaryService] = None,
        person_fact_service: Optional[PersonFactService] = None,
        storage: Optional[MarkdownMemoryStore] = None,
        important_memory_store: Optional[ImportantMemoryStore] = None,
        llm_callback: Optional[Callable[[str, List[Dict[str, str]]], Any]] = None,
    ) -> None:
        self.conversation_store = conversation_store
        self.extractor = extractor
        self.summary_service = summary_service
        self.person_fact_service = person_fact_service
        self.task_manager = task_manager
        self.auto_extract_memory = auto_extract_memory
        self.on_memory_changed = on_memory_changed
        self.storage = storage
        self.important_memory_store = important_memory_store
        self.llm_callback = llm_callback
        self._digestion_task: Optional[asyncio.Task] = None
        self._digestion_stop: Optional[asyncio.Event] = None

    async def _sync_person_facts(self, user_id: str) -> None:
        if not self.person_fact_service:
            return
        try:
            await self.person_fact_service.sync_user_facts(str(user_id))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[后台协调] 同步人物事实失败")

    async def _save_conversation_and_summary(
        self,
        *,
        user_id: str,
        session_id: Optional[str] = None,
        dialogue_key: Optional[str] = None,
        force: bool = False,
    ):
        result = await self.conversation_store.save_conversation(
            user_id=user_id,
            session_id=session_id,
            dialogue_key=dialogue_key,
            force=force,
        )
        if result and self.summary_service and str(result.closed_at or "").strip():
            updated = await self.summary_service.refresh_session_summary(user_id=user_id, record=result)
            if updated is not None:
                result = updated
        return result

    def register_dialogue_turn(
        self,
        user_id: str,
        user_message: str,
        assistant_message: str,
        *,
        dialogue_key: Optional[str] = None,
        message_type: str = "private",
        group_id: Optional[str] = None,
        message_id: Optional[str] = None,
        image_description: str = "",
    ) -> None:
        registration = self.conversation_store.add_turn(
            user_id=user_id,
            user_message=user_message,
            assistant_message=assistant_message,
            dialogue_key=dialogue_key,
            message_type=message_type,
            group_id=group_id,
            message_id=message_id,
            image_description=image_description,
        )
        logger.debug(
            "已登记对话轮次：用户=%s，会话=%s，轮次=%s",
            user_id,
            registration.session_id,
            registration.turn_id,
        )

        if self.extractor:
            self.extractor.add_dialogue_turn(
                user_id=user_id,
                user_message=user_message,
                assistant_message=assistant_message,
                session_id=registration.session_id,
                turn_id=registration.turn_id,
                dialogue_key=registration.dialogue_key,
                message_type=message_type,
                group_id=group_id,
                message_id=message_id,
            )

        if registration.closed_session_id:
            self.schedule_conversation_save(
                registration.closed_session_user_id or user_id,
                session_id=registration.closed_session_id,
                force=True,
            )

    def schedule_conversation_save(
        self,
        user_id: str,
        *,
        session_id: Optional[str] = None,
        dialogue_key: Optional[str] = None,
        force: bool = False,
    ) -> asyncio.Task:
        async def save_conversation() -> None:
            try:
                result = await self._save_conversation_and_summary(
                    user_id=user_id,
                    session_id=session_id,
                    dialogue_key=dialogue_key,
                    force=force,
                )
                if result:
                    logger.debug("[后台协调] 对话会话已保存")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[后台协调] 保存对话会话失败")

        task_name = f"memory-save-{session_id or dialogue_key or user_id}"
        return self.task_manager.create_task(save_conversation(), name=task_name)

    async def _finalize_session(
        self,
        *,
        user_id: str,
        session_id: str,
        extract_pending: bool,
    ) -> List[MemoryItem]:
        saved_memories: List[MemoryItem] = []
        try:
            await self._save_conversation_and_summary(
                user_id=user_id,
                session_id=session_id,
                force=True,
            )
            if extract_pending and self.auto_extract_memory and self.extractor:
                saved_memories = await self.extractor.extract_memories(user_id, session_id=session_id, force=True)
                if saved_memories:
                    await self._sync_person_facts(user_id)
                    self.on_memory_changed(user_id)
            return saved_memories
        finally:
            if self.extractor:
                self.extractor.clear_buffer(session_id=session_id)

    async def maybe_extract_memories(
        self,
        user_id: str,
        *,
        dialogue_key: Optional[str] = None,
        message_type: str = "private",
        group_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> List[MemoryItem]:
        if not self.auto_extract_memory:
            logger.debug("[后台协调] 自动记忆提取未启用")
            return []
        if not self.extractor:
            logger.debug("[后台协调] 记忆提取器不可用")
            return []

        resolved_session_id = self._resolve_session_id(
            user_id=user_id,
            dialogue_key=dialogue_key,
            message_type=message_type,
            group_id=group_id,
            session_id=session_id,
        )
        if not resolved_session_id:
            return []

        if not self.extractor.should_extract(resolved_session_id):
            pending_turns = self.extractor.get_pending_turn_count(resolved_session_id)
            interval = max(1, int(self.extractor.config.extract_every_n_turns))
            turns_until_next = max(interval - pending_turns, 0)
            logger.info(
                "自动记忆提取暂不触发：用户=%s，会话=%s，当前待提取轮次=%s/%s，还差=%s 轮",
                user_id,
                resolved_session_id,
                pending_turns,
                interval,
                turns_until_next,
            )
            return []

        memories = await self.extractor.extract_memories(user_id, session_id=resolved_session_id)
        if memories:
            await self._sync_person_facts(user_id)
            self.on_memory_changed(user_id)
        return memories

    def schedule_memory_extraction(
        self,
        user_id: str,
        *,
        dialogue_key: Optional[str] = None,
        message_type: str = "private",
        group_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> asyncio.Task:
        async def extract() -> None:
            try:
                memories = await self.maybe_extract_memories(
                    user_id,
                    dialogue_key=dialogue_key,
                    message_type=message_type,
                    group_id=group_id,
                    session_id=session_id,
                )
                if memories:
                    logger.debug("[后台协调] 后台记忆提取完成")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[后台协调] 后台记忆提取任务失败")

        task_name = f"memory-extract-{session_id or dialogue_key or user_id}"
        return self.task_manager.create_task(extract(), name=task_name)

    def force_extraction(
        self,
        user_id: str,
        *,
        dialogue_key: Optional[str] = None,
        message_type: str = "private",
        group_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> asyncio.Task:
        if not self.extractor:
            return self.task_manager.create_task(asyncio.sleep(0), name=f"memory-extract-empty-{user_id}")

        async def extract() -> List[MemoryItem]:
            resolved_session_id = self._resolve_session_id(
                user_id=user_id,
                dialogue_key=dialogue_key,
                message_type=message_type,
                group_id=group_id,
                session_id=session_id,
            )
            if not resolved_session_id:
                return []
            memories = await self.extractor.extract_memories(user_id, session_id=resolved_session_id, force=True)
            if memories:
                await self._sync_person_facts(user_id)
                self.on_memory_changed(user_id)
            return memories

        task_name = f"memory-force-extract-{session_id or dialogue_key or user_id}"
        return self.task_manager.create_task(extract(), name=task_name)

    def flush_conversation_session(
        self,
        *,
        user_id: str,
        message_type: str = "private",
        group_id: Optional[str] = None,
        dialogue_key: Optional[str] = None,
    ) -> Optional[asyncio.Task]:
        session_id = self.conversation_store.close_session(
            user_id=user_id,
            dialogue_key=dialogue_key,
            message_type=message_type,
            group_id=group_id,
        )
        if not session_id:
            return None

        async def finalize_session() -> List[MemoryItem]:
            try:
                return await self._finalize_session(
                    user_id=user_id,
                    session_id=session_id,
                    extract_pending=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "会话收尾失败：用户=%s，会话=%s，错误=%s",
                    user_id,
                    session_id,
                    exc,
                    exc_info=True,
                )
                return []

        task_name = f"memory-flush-{session_id}"
        return self.task_manager.create_task(finalize_session(), name=task_name)

    async def flush_conversation_buffers(self) -> None:
        for session_id in list(self.conversation_store.active_session_ids()):
            owner_user_id = self.conversation_store.get_session_owner(session_id)
            if not owner_user_id:
                continue
            await self._save_conversation_and_summary(
                user_id=owner_user_id,
                session_id=session_id,
                force=True,
            )

    async def flush(self) -> None:
        await self.flush_conversation_buffers()
        await self.task_manager.flush()

    async def close(self) -> None:
        self.stop_digestion()
        closed_session_ids = self.conversation_store.close_all_sessions()
        for session_id in closed_session_ids:
            owner_user_id = self.conversation_store.get_session_owner(session_id)
            if not owner_user_id:
                continue
            try:
                await self._finalize_session(
                    user_id=owner_user_id,
                    session_id=session_id,
                    extract_pending=False,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "关闭会话收尾失败：用户=%s，会话=%s，错误=%s",
                    owner_user_id,
                    session_id,
                    exc,
                    exc_info=True,
                )
        await self.flush_conversation_buffers()
        await self.task_manager.cancel_all()

    def start_digestion(self, interval_hours: float = 6.0) -> None:
        """启动周期性记忆消化任务。"""
        if self._digestion_task is not None and not self._digestion_task.done():
            return
        if not self.storage or not self.important_memory_store or not self.llm_callback:
            logger.debug("记忆消化缺少必要组件，未启动")
            return

        self._digestion_stop = asyncio.Event()

        async def run_loop():
            logger.info("记忆消化任务已启动：间隔=%.1f小时", interval_hours)
            while not self._digestion_stop.is_set():
                try:
                    await asyncio.wait_for(self._digestion_stop.wait(), timeout=interval_hours * 3600)
                except asyncio.TimeoutError:
                    pass
                if self._digestion_stop.is_set():
                    break
                try:
                    await self._run_digestion_cycle()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("[后台协调] 记忆消化循环异常")

        self._digestion_task = asyncio.create_task(run_loop())

    def stop_digestion(self) -> None:
        """停止周期性记忆消化任务。"""
        if self._digestion_stop:
            self._digestion_stop.set()
        if self._digestion_task and not self._digestion_task.done():
            self._digestion_task.cancel()
        self._digestion_task = None
        self._digestion_stop = None

    async def _run_digestion_cycle(self) -> None:
        """执行一轮记忆消化：扫描所有用户，尝试发现模式并存入重要记忆。"""
        user_ids = self.storage.get_user_ids()
        if not user_ids:
            return

        logger.debug("[后台协调] 记忆消化开始扫描")
        insight_count = 0
        for user_id in user_ids:
            try:
                insight = await self._generate_insight(user_id)
                if insight is not None:
                    await self.important_memory_store.add_memory(
                        user_id=user_id,
                        content=insight,
                        source="periodic_digestion",
                        priority=2,
                        metadata={"insight_type": "digested", "insight_source": "periodic_digestion"},
                    )
                    insight_count += 1
                    logger.info("[后台协调] 记忆消化发现 insight")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("[后台协调] 记忆消化处理用户失败")

        if insight_count > 0:
            logger.info("[后台协调] 记忆消化本轮完成")

    async def _generate_insight(self, user_id: str) -> Optional[str]:
        """使用 LLM 从近期记忆中生成 insight。返回 insight 文本或 None。"""
        memories = await self.storage.get_user_memories(user_id)
        if len(memories) < 3:
            return None

        now = datetime.now()
        recent = [
            m for m in memories
            if self._days_ago(m.updated_at, now) <= 7
        ]
        if len(recent) < 3:
            return None

        memory_lines = [f"- [{m.updated_at[:10]}] {m.content}" for m in recent[-20:]]
        user_prompt = "以下是用户最近的记忆列表。请分析其中是否存在值得记录的模式、趋势或变化：\n" + "\n".join(memory_lines)

        try:
            insight_prompt = _insight_system_prompt()
            messages = [
                {"role": "system", "content": insight_prompt},
                {"role": "user", "content": user_prompt},
            ]
            response = await asyncio.wait_for(
                self.llm_callback(insight_prompt, [messages[-1]]),
                timeout=30.0,
            )
            content = str(getattr(response, "content", "") or "")
            return self._parse_insight_response(content)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.debug("[后台协调] 记忆消化 LLM 超时")
            return None
        except Exception as exc:
            logger.debug("[后台协调] 记忆消化 LLM 失败")
            return None

    def _parse_insight_response(self, content: str) -> Optional[str]:
        import json
        import re as _re
        text = str(content or "").strip()
        text = _re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=_re.IGNORECASE | _re.MULTILINE).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        if not data.get("has_insight", False):
            return None
        insight_text = str(data.get("content", "")).strip()
        confidence = float(data.get("confidence", 0.0) or 0.0)
        if not insight_text or confidence < 0.7:
            return None
        return insight_text

    @staticmethod
    def _days_ago(timestamp: str, now: datetime) -> float:
        try:
            dt = datetime.fromisoformat(timestamp)
            return (now - dt).total_seconds() / 86400.0
        except (TypeError, ValueError):
            return 999.0

    def _resolve_session_id(
        self,
        *,
        user_id: str,
        dialogue_key: Optional[str],
        message_type: str,
        group_id: Optional[str],
        session_id: Optional[str],
    ) -> str:
        if str(session_id or "").strip():
            return str(session_id).strip()
        return self.conversation_store.get_active_session_id(
            user_id=user_id,
            dialogue_key=dialogue_key,
            message_type=message_type,
            group_id=group_id,
        )
