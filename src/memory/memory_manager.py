"""
记忆管理器
整合存储、检索、提取三大模块，提供统一的记忆管理接口
"""
import logging
import os
from typing import List, Dict, Optional, Any, Callable, Tuple
from dataclasses import dataclass, field
import asyncio
from datetime import datetime

from src.memory.storage.markdown_store import MemoryItem, MarkdownMemoryStore
from src.memory.storage.conversation_store import ConversationStore, ConversationRecord
from src.memory.storage.important_memory_store import ImportantMemoryStore, ImportantMemoryItem
from src.memory.retrieval.bm25_index import BM25Index, SearchResult
from src.memory.retrieval.two_stage_retriever import TwoStageRetriever, RetrievalConfig
from src.memory.extraction.memory_extractor import MemoryExtractor, ExtractionConfig

logger = logging.getLogger(__name__)


@dataclass
class MemoryManagerConfig:
    """记忆管理器配置"""
    # 存储配置
    storage_base_path: str = "memories"

    # 检索配置
    retrieval_config: RetrievalConfig = field(default_factory=RetrievalConfig)

    # 提取配置
    extraction_config: ExtractionConfig = field(default_factory=ExtractionConfig)

    # 普通记忆衰减配置
    ordinary_decay_enabled: bool = True
    ordinary_half_life_days: float = 30.0
    ordinary_forget_threshold: float = 0.5

    # 对话记录保存配置
    conversation_save_interval: int = 10

    # 全局配置
    auto_build_index: bool = True  # 启动时自动构建索引
    auto_extract_memory: bool = True  # 自动提取记忆


class MemoryManager:
    """
    记忆管理器

    整合存储层、检索层、提取层，提供：
    1. 记忆的增删改查
    2. 基于 BM25 + 可选精排的检索
    3. 自动从对话中提取记忆
    4. 索引的自动维护
    """

    def __init__(
        self,
        llm_callback: Optional[Callable[[str, List[Dict[str, str]]], Any]] = None,
        config: Optional[MemoryManagerConfig] = None
    ):
        self.config = config or MemoryManagerConfig()
        self.llm_callback = llm_callback

        # 初始化存储层
        self.storage = MarkdownMemoryStore(
            base_path=self.config.storage_base_path,
            ordinary_decay_enabled=self.config.ordinary_decay_enabled,
            ordinary_half_life_days=self.config.ordinary_half_life_days,
            ordinary_forget_threshold=self.config.ordinary_forget_threshold,
        )

        # 初始化检索层
        self.bm25_index = BM25Index()
        self.retriever = TwoStageRetriever(
            bm25_index=self.bm25_index,
            config=self.config.retrieval_config
        )

        # 初始化重要记忆存储（必须在提取层之前初始化）
        self.important_memory_store = ImportantMemoryStore(
            base_path=os.path.join(self.config.storage_base_path, "important")
        )
        logger.info(f"重要记忆存储已就绪: {self.important_memory_store.base_path}")

        # 初始化提取层
        self.extractor: Optional[MemoryExtractor] = None
        if llm_callback:
            self.extractor = MemoryExtractor(
                memory_store=self.storage,
                llm_callback=llm_callback,
                config=self.config.extraction_config,
                important_memory_store=self.important_memory_store  # 传入重要记忆存储
            )
            logger.info(f"记忆提取器已就绪: 每 {self.config.extraction_config.extract_every_n_turns} 轮提取一次")
        else:
            logger.warning("未提供 llm_callback，已关闭自动记忆提取")

        # 初始化对话存储（使用可配置的保存间隔）
        save_interval = self.config.conversation_save_interval
        self.conversation_store = ConversationStore(
            base_path=os.path.join(self.config.storage_base_path, "conversations"),
            save_interval=save_interval
        )
        logger.info(f"对话存储已就绪: 间隔={save_interval} 轮")

        # 索引状态跟踪
        self._index_built: Dict[str, bool] = {}
        self._index_dirty: Dict[str, bool] = {}

    # ===== 初始化与维护 =====

    async def initialize(self):
        """初始化记忆管理器"""
        logger.info("初始化记忆管理器")

        # 如果配置了自动构建索引，为所有用户构建索引
        if self.config.auto_build_index:
            await self.rebuild_all_indices()

        # 初始化精排器
        if self.config.retrieval_config.rerank_enabled:
            self.retriever.initialize_reranker()

        logger.info("记忆管理器初始化完成")

    async def rebuild_index(self, user_id: str):
        """为指定用户重建 BM25 索引"""
        try:
            # 读取用户记忆
            memories = await self.storage.get_user_memories(user_id)

            # 构建索引
            success = self.bm25_index.build_index(user_id, memories)

            if success:
                self._index_built[user_id] = True
                self._index_dirty[user_id] = False
                logger.debug(f"索引重建完成: 用户={user_id}, 记忆={len(memories)}")

            return success

        except Exception as e:
            logger.error(f"索引重建失败: 用户={user_id}, 错误={e}")
            return False

    async def rebuild_all_indices(self):
        """重建所有用户的索引"""
        # 获取所有用户文件
        users_path = self.storage.users_path
        if not users_path.exists():
            return

        user_files = list(users_path.glob("*.md"))

        logger.info(f"开始重建索引: 用户数={len(user_files)}")

        tasks = []
        for file_path in user_files:
            user_id = file_path.stem
            tasks.append(self.rebuild_index(user_id))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            success_count = sum(1 for r in results if r is True)
            logger.info(f"索引重建完成: 成功={success_count}/{len(tasks)}")

    def mark_index_dirty(self, user_id: str):
        """标记用户索引需要重建"""
        self._index_dirty[user_id] = True
        logger.debug(f"索引已标记待刷新: 用户={user_id}")

    async def ensure_index_fresh(self, user_id: str):
        """确保索引是最新的（延迟重建）"""
        if self._index_dirty.get(user_id, False):
            await self.rebuild_index(user_id)

    # ===== 核心 API =====

    async def add_memory(
        self,
        content: str,
        user_id: Optional[str] = None,
        source: str = "",
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict] = None
    ) -> Optional[MemoryItem]:
        """
        添加记忆

        Args:
            content: 记忆内容
            user_id: 用户ID（None表示全局记忆）
            source: 来源
            tags: 标签

        Returns:
            创建的记忆项
        """
        result = await self.storage.add_memory(
            content=content,
            user_id=user_id,
            source=source,
            tags=tags,
            metadata=metadata
        )

        if result:
            # 标记索引需要更新
            if user_id:
                self.mark_index_dirty(user_id)
            else:
                # 全局记忆变化，标记所有索引 dirty
                # 实际可以优化为只更新全局部分
                pass

        return result

    async def search_memories(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        use_rerank: Optional[bool] = None
    ) -> List[SearchResult]:
        """
        搜索记忆（两阶段检索）

        Args:
            user_id: 用户ID
            query: 查询文本
            top_k: 返回结果数量
            use_rerank: 是否使用精排（None表示使用配置）

        Returns:
            搜索结果列表
        """
        # 确保索引是最新的
        await self.ensure_index_fresh(user_id)

        # 检查索引是否存在
        if not self._index_built.get(user_id, False):
            logger.debug(f"索引不存在，尝试重建: 用户={user_id}")
            await self.rebuild_index(user_id)

        # 临时修改精排配置（如果需要）
        original_rerank = self.config.retrieval_config.rerank_enabled
        if use_rerank is not None:
            self.config.retrieval_config.rerank_enabled = use_rerank

        try:
            # 执行检索
            results = await self.retriever.retrieve(
                user_id=user_id,
                query=query,
                top_k=top_k
            )
            return results

        finally:
            # 恢复配置
            self.config.retrieval_config.rerank_enabled = original_rerank

    async def quick_check_relevance(
        self,
        user_id: str,
        query: str,
        threshold: float = 0.5
    ) -> Optional[MemoryItem]:
        """
        快速检查是否有相关记忆
        返回最相关的一条，如果没有超过阈值则返回 None
        """
        await self.ensure_index_fresh(user_id)
        return await self.retriever.quick_check(user_id, query, threshold)

    async def search_memories_with_context(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        include_conversations: bool = True
    ) -> Dict[str, Any]:
        """
        搜索记忆并包含相关的对话上下文

        Args:
            user_id: 用户ID
            query: 查询文本
            top_k: 返回记忆数量
            include_conversations: 是否包含相关对话

        Returns:
            {
                "memories": [记忆列表],
                "conversations": [相关对话记录],
                "context_text": "格式化的上下文文本"
            }
        """
        # 1. 搜索记忆
        important_memories = await self.search_important_memories(user_id, query, limit=top_k)
        if important_memories:
            context_text = self._format_context_for_prompt(important_memories, [])
            return {
                "memories": important_memories,
                "conversations": [],
                "context_text": context_text,
                "history_messages": [],
            }

        memory_results = await self.search_memories(user_id, query, top_k=top_k)

        memories = []
        related_turns = self._collect_related_turns_from_memories(memory_results)
        for result in memory_results:
            if result.memory:
                memories.append({
                    "content": result.memory.content,
                    "source": result.memory.source,
                    "tags": result.memory.tags,
                    "score": result.bm25_score
                })

        # 2. 搜索相关对话
        conversations = []
        if include_conversations and self.conversation_store:
            try:
                if not related_turns:
                    related_turns = await self._load_related_turns_from_conversation_store(
                        user_id=user_id,
                        memories=memory_results,
                        query=query,
                        limit=10,
                    )

                if related_turns:
                    conversations.append({
                        "turns": related_turns,
                        "created_at": related_turns[-1].get("timestamp", ""),
                        "turn_count": len(related_turns)
                    })
            except Exception as e:
                logger.warning(f"搜索对话记录失败: {e}")

        # 3. 格式化上下文文本
        context_text = self._format_context_for_prompt(memories, conversations)
        history_messages = self._build_history_messages_from_turns(related_turns)

        return {
            "memories": memories,
            "conversations": conversations,
            "context_text": context_text,
            "history_messages": history_messages,
        }

    async def search_important_memories(
        self,
        user_id: str,
        query: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Search important memories before ordinary retrieval."""
        matched = await self.important_memory_store.search_memories(
            user_id=user_id,
            query=query,
            top_k=limit,
        )
        return [
            {
                "content": memory.content,
                "source": memory.source,
                "priority": memory.priority,
                "score": memory.score,
                "memory_type": "important",
            }
            for memory in matched
        ]

    def _format_context_for_prompt(
        self,
        memories: List[Dict],
        conversations: List[Dict]
    ) -> str:
        """格式化记忆和对话为提示词上下文"""
        parts = []

        if memories:
            parts.append("=== 关于用户的历史记忆 ===")
            for i, mem in enumerate(memories, 1):
                parts.append(f"{i}. {mem['content']}")
            parts.append("")

        if conversations:
            parts.append("=== 相关对话记录 ===")
            for conv in conversations:
                parts.append(f"对话时间: {conv.get('created_at', '未知')}")
                for turn in conv.get('turns', []):
                    parts.append(f"用户: {turn.get('user', '未知')}")
                    parts.append(f"助手: {turn.get('assistant', '未知')}")
                parts.append("")

        return "\n".join(parts) if parts else ""

    def _collect_related_turns_from_memories(
        self,
        memory_results: List[SearchResult],
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """从命中的记忆元数据中收集关联对话。"""
        merged: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

        for result in memory_results:
            memory = getattr(result, "memory", None)
            if not memory:
                continue

            related_dialogue = memory.metadata.get("related_dialogue", [])
            if not isinstance(related_dialogue, list):
                continue

            for turn in related_dialogue:
                if not isinstance(turn, dict):
                    continue
                key = (
                    str(turn.get("timestamp", "")),
                    str(turn.get("user", "")),
                    str(turn.get("assistant", "")),
                )
                merged[key] = {
                    "user": turn.get("user", ""),
                    "assistant": turn.get("assistant", ""),
                    "timestamp": turn.get("timestamp", ""),
                }

        turns = list(merged.values())
        turns.sort(key=lambda item: item.get("timestamp", ""))
        return turns[-limit:]

    async def _load_related_turns_from_conversation_store(
        self,
        user_id: str,
        memories: List[SearchResult],
        query: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """为旧记忆补充关联对话。"""
        keywords: List[str] = []

        for result in memories:
            memory = getattr(result, "memory", None)
            if memory and memory.content:
                keywords.append(memory.content)

        if query:
            keywords.append(query)

        for keyword in keywords:
            conv_records = await self.conversation_store.search_conversations(
                user_id=user_id,
                keyword=keyword,
                limit=1,
            )
            if not conv_records:
                continue

            record = conv_records[0]
            return record.turns[-limit:]

        return []

    def _build_history_messages_from_turns(
        self,
        turns: List[Dict[str, Any]],
        limit_turns: int = 10,
    ) -> List[Dict[str, str]]:
        """把关联对话轮次转换成可发给模型的历史消息。"""
        history_messages: List[Dict[str, str]] = []

        for turn in turns[-limit_turns:]:
            user_text = str(turn.get("user", "")).strip()
            assistant_text = str(turn.get("assistant", "")).strip()

            if user_text:
                history_messages.append({"role": "user", "content": user_text})
            if assistant_text:
                history_messages.append({"role": "assistant", "content": assistant_text})

        return history_messages

    async def get_user_memories(self, user_id: str) -> List[MemoryItem]:
        """获取用户的所有记忆"""
        return await self.storage.get_user_memories(user_id)

    async def delete_memory(self, mem_id: str, user_id: Optional[str] = None) -> bool:
        """删除记忆"""
        result = await self.storage.delete_memory(mem_id, user_id)
        if result and user_id:
            self.mark_index_dirty(user_id)
        return result

    async def update_memory(
        self,
        mem_id: str,
        content: str,
        user_id: Optional[str] = None
    ) -> bool:
        """更新记忆"""
        result = await self.storage.update_memory(mem_id, content, user_id)
        if result and user_id:
            self.mark_index_dirty(user_id)
        return result

    # ===== 记忆提取集成 =====

    def register_dialogue_turn(
        self,
        user_id: str,
        user_message: str,
        assistant_message: str
    ):
        """
        注册一轮对话（用于自动记忆提取和对话存储）

        Args:
            user_id: 用户ID
            user_message: 用户消息
            assistant_message: 助手回复
        """
        # 添加到记忆提取器的对话缓冲区
        if self.extractor:
            self.extractor.add_dialogue_turn(user_id, user_message, assistant_message)

        # 添加到对话存储（每10轮会自动保存）
        turn_count = self.conversation_store.add_turn(user_id, user_message, assistant_message)
        logger.debug(f"对话缓冲: 用户={user_id}, 轮数={turn_count}")

        # 如果达到10轮，触发保存
        if turn_count >= 10:
            logger.info(f"对话达到保存阈值: 用户={user_id}, 轮数={turn_count}")
            # 异步保存（不等待），但添加错误处理
            async def _save_with_error_handling():
                try:
                    result = await self.conversation_store.save_conversation(user_id)
                    if result:
                        logger.info(f"对话已保存: 用户={user_id}, 记录={result.record_id}")
                    else:
                        logger.warning(f"对话保存未返回结果: 用户={user_id}")
                except Exception as e:
                    logger.error(f"对话保存失败: 用户={user_id}, 错误={e}", exc_info=True)

            asyncio.create_task(_save_with_error_handling())

    async def maybe_extract_memories(self, user_id: str) -> List[MemoryItem]:
        """
        检查是否需要提取记忆，如果是则执行提取

        Args:
            user_id: 用户ID

        Returns:
            提取到的记忆列表（如果没有触发提取则为空）
        """
        if not self.config.auto_extract_memory:
            logger.info(f"自动记忆提取已禁用: 用户={user_id}")
            return []

        if not self.extractor:
            logger.warning(f"无法提取记忆: 用户={user_id}, 提取器未初始化")
            return []

        should_extract = self.extractor.should_extract(user_id)
        logger.info(f"检查记忆提取: 用户={user_id}, 触发={should_extract}")

        if not should_extract:
            return []

        logger.info(f"触发自动记忆提取: 用户={user_id}")

        # 异步执行提取
        memories = await self.extractor.extract_memories(user_id)

        # 如果提取到记忆，需要重建索引
        if memories:
            self.mark_index_dirty(user_id)

        return memories

    def force_extraction(self, user_id: str) -> asyncio.Task:
        """
        强制触发记忆提取（返回 Task，不等待）

        Args:
            user_id: 用户ID

        Returns:
            asyncio.Task
        """
        if not self.extractor:
            # 返回一个已完成的空任务
            task = asyncio.create_task(asyncio.sleep(0))
            return task

        async def _extract():
            memories = await self.extractor.extract_memories(user_id)
            if memories:
                self.mark_index_dirty(user_id)
            return memories

        return asyncio.create_task(_extract())

    # ===== 重要记忆管理 =====

    async def add_important_memory(
        self,
        user_id: str,
        content: str,
        source: str = "manual",
        priority: int = 1
    ) -> Optional[ImportantMemoryItem]:
        """
        添加重要记忆

        Args:
            user_id: 用户ID
            content: 记忆内容
            source: 来源标识
            priority: 优先级（1-5，越大越重要）

        Returns:
            创建的记忆项
        """
        return await self.important_memory_store.add_memory(
            user_id=user_id,
            content=content,
            source=source,
            priority=priority
        )

    async def get_important_memories(
        self,
        user_id: str,
        min_priority: int = 1,
        limit: int = 10
    ) -> List[ImportantMemoryItem]:
        """
        获取用户的重要记忆

        Args:
            user_id: 用户ID
            min_priority: 最小优先级
            limit: 最大返回数量

        Returns:
            记忆项列表（按优先级降序）
        """
        memories = await self.important_memory_store.get_memories(
            user_id=user_id,
            min_priority=min_priority
        )
        return memories[:limit]

    async def format_important_memories_for_prompt(
        self,
        user_id: str,
        limit: int = 5
    ) -> str:
        """
        格式化重要记忆为提示词文本

        Args:
            user_id: 用户ID
            limit: 最大返回数量

        Returns:
            格式化的提示词文本，如果没有则返回空字符串
        """
        memories = await self.get_important_memories(user_id, limit=limit)

        if not memories:
            return ""

        lines = ["=== 重要事实（请务必记住）==="]
        for i, mem in enumerate(memories, 1):
            lines.append(f"{i}. {mem.content}")
        lines.append("")

        return "\n".join(lines)

    async def delete_important_memory(self, user_id: str, content_substring: str) -> bool:
        """
        删除包含指定内容的重要记忆

        Args:
            user_id: 用户ID
            content_substring: 内容子串

        Returns:
            是否成功删除
        """
        return await self.important_memory_store.delete_memory(user_id, content_substring)

    async def clear_important_memories(self, user_id: str) -> bool:
        """
        清空用户的所有重要记忆

        Args:
            user_id: 用户ID

        Returns:
            是否成功清空
        """
        return await self.important_memory_store.clear_memories(user_id)

    # ===== 资源管理 =====

    async def close(self):
        """关闭资源"""
        if self.retriever:
            await self.retriever.close()
        logger.info("记忆管理器已关闭")

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            "storage_path": str(self.storage.base_path),
            "indices_built": len(self._index_built),
            "indices_dirty": len(self._index_dirty),
            "extractor_enabled": self.extractor is not None
        }
