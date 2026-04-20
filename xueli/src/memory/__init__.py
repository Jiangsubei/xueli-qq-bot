"""
记忆模块
提供透明可维护的长期记忆管理

主要组件:
- storage: Markdown 明文存储
- retrieval: BM25 索引 + 两阶段检索
- extraction: LLM 自动提取记忆
- memory_manager: 统一管理层
"""
from .storage.markdown_store import MemoryItem, MarkdownMemoryStore
from .storage.conversation_store import ConversationRecord, ConversationStore
from .storage.important_memory_store import ImportantMemoryItem, ImportantMemoryStore
from .storage.person_fact_store import PersonFactItem, PersonFactStore
from .retrieval.bm25_index import BM25Index, ChineseTokenizer, SearchResult
from .retrieval.two_stage_retriever import (
    TwoStageRetriever,
    RetrievalConfig,
    BaseReranker,
    CrossEncoderReranker,
    APIReranker,
)
from .extraction.memory_extractor import MemoryExtractor, ExtractionConfig
from .memory_manager import MemoryManager, MemoryManagerConfig
from .conversation_recall_service import ConversationRecallService
from .person_fact_service import PersonFactService

__version__ = "1.0.0"

__all__ = [
    # 存储层
    "MemoryItem",
    "MarkdownMemoryStore",
    "ConversationRecord",
    "ConversationStore",
    "ImportantMemoryItem",
    "ImportantMemoryStore",
    "PersonFactItem",
    "PersonFactStore",

    # 检索层
    "BM25Index",
    "ChineseTokenizer",
    "SearchResult",
    "TwoStageRetriever",
    "RetrievalConfig",
    "BaseReranker",
    "CrossEncoderReranker",
    "APIReranker",

    # 提取层
    "MemoryExtractor",
    "ExtractionConfig",

    # 管理器
    "MemoryManager",
    "MemoryManagerConfig",
    "ConversationRecallService",
    "PersonFactService",
]


def create_memory_manager(
    llm_callback=None,
    storage_path: str = "memories",
    enable_rerank: bool = False,
    enable_extraction: bool = True
) -> MemoryManager:
    """
    快速创建记忆管理器的工厂函数

    Args:
        llm_callback: LLM 调用回调函数
        storage_path: 记忆存储路径
        enable_rerank: 是否启用精排
        enable_extraction: 是否启用自动记忆提取

    Returns:
        MemoryManager 实例
    """
    config = MemoryManagerConfig(
        storage_base_path=storage_path,
        retrieval_config=RetrievalConfig(
            rerank_enabled=enable_rerank
        ),
        auto_extract_memory=enable_extraction
    )

    manager = MemoryManager(
        llm_callback=llm_callback,
        config=config
    )

    return manager
