"""
检索层 - BM25 索引实现。

基于中文分词的轻量级倒排检索。
"""
import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import jieba
from rank_bm25 import BM25Okapi

from ..storage.markdown_store import MemoryItem

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """搜索结果项。"""

    memory: MemoryItem
    bm25_score: float
    local_score: Optional[float] = None
    rerank_score: Optional[float] = None
    combined_score: Optional[float] = None
    ranking_stage: str = "bm25"


class ChineseTokenizer:
    """中文分词器。"""

    STOP_WORDS = {
        "的",
        "了",
        "在",
        "是",
        "我",
        "有",
        "和",
        "就",
        "不",
        "人",
        "都",
        "一",
        "一个",
        "上",
        "也",
        "很",
        "到",
        "说",
        "要",
        "去",
        "你",
        "会",
        "着",
        "没有",
        "看",
        "好",
        "自己",
        "这",
        "那",
    }

    @classmethod
    def tokenize(cls, text: str) -> List[str]:
        """对文本进行分词并做基础过滤。"""
        if not text:
            return []

        text = cls._clean_text(text)
        tokens = list(jieba.cut(text))

        result = []
        for token in tokens:
            token = token.strip().lower()
            if len(token) <= 1:
                continue
            if token in cls.STOP_WORDS:
                continue
            if token.isdigit() and len(token) > 10:
                continue
            result.append(token)

        return result

    @classmethod
    def _clean_text(cls, text: str) -> str:
        """清洗文本。"""
        text = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9\s]", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @classmethod
    def extract_keywords(cls, text: str, top_k: int = 10) -> List[str]:
        """提取关键词。"""
        tokens = cls.tokenize(text)
        freq: Dict[str, int] = {}
        for token in tokens:
            freq[token] = freq.get(token, 0) + 1
        sorted_keywords = sorted(freq.items(), key=lambda item: item[1], reverse=True)
        return [kw for kw, _ in sorted_keywords[:top_k]]


class BM25Index:
    """按用户维护 BM25 检索索引。"""

    def __init__(self):
        self._indices: Dict[str, "IndexData"] = {}
        self._tokenizer = ChineseTokenizer()

    def build_index(self, user_id: str, memories: List[MemoryItem]) -> bool:
        """为指定用户构建 BM25 索引。"""
        if not memories:
            logger.debug("[BM25] 跳过 BM25 构建，原因=无记忆")
            self._indices[user_id] = IndexData(
                memories=[],
                tokenized_docs=[],
                bm25=None,
            )
            return True

        try:
            tokenized_docs = [self._tokenizer.tokenize(mem.content) for mem in memories]
            bm25 = BM25Okapi(tokenized_docs) if any(tokenized_docs) else None

            self._indices[user_id] = IndexData(
                memories=memories,
                tokenized_docs=tokenized_docs,
                bm25=bm25,
                fallback_only=bm25 is None,
            )
            if bm25 is None:
                logger.debug("[BM25] BM25 索引已降级为回退检索")
                return True
            logger.debug("[BM25] BM25 索引已构建")
            return True
        except Exception as e:
            logger.error("[BM25] BM25 构建失败")
            return False

    def search(
        self,
        user_id: str,
        query: str,
        top_k: int = 20,
        min_score: float = 0.0,
    ) -> List[Tuple[MemoryItem, float]]:
        """执行 BM25 检索。"""
        index_data = self._indices.get(user_id)

        if not index_data:
            logger.debug("[BM25] BM25 索引不可用")
            return []

        if not index_data.memories:
            return []

        try:
            query_tokens = self._tokenizer.tokenize(query)
            if index_data.bm25 and query_tokens:
                scores = index_data.bm25.get_scores(query_tokens)
                results = []
                for idx, score in enumerate(scores):
                    if score >= min_score:
                        results.append((index_data.memories[idx], score))
                results.sort(key=lambda item: item[1], reverse=True)
                return results[:top_k]

            logger.debug("[BM25] BM25 回退检索")
            return self._fallback_search(
                memories=index_data.memories,
                tokenized_docs=index_data.tokenized_docs,
                query=query,
                query_tokens=query_tokens,
                top_k=top_k,
                min_score=min_score,
            )
        except Exception as e:
            logger.error("[BM25] BM25 检索失败")
            return []

    def _fallback_search(
        self,
        *,
        memories: List[MemoryItem],
        tokenized_docs: List[List[str]],
        query: str,
        query_tokens: List[str],
        top_k: int,
        min_score: float,
    ) -> List[Tuple[MemoryItem, float]]:
        results: List[Tuple[MemoryItem, float]] = []
        normalized_query = self._normalize_raw_text(query)
        query_chars = self._char_set(normalized_query)
        query_token_set = set(query_tokens)

        for memory, doc_tokens in zip(memories, tokenized_docs):
            token_score = self._overlap_coefficient(query_token_set, set(doc_tokens))
            raw_score = self._raw_text_score(normalized_query, query_chars, memory.content)
            score = max(token_score, raw_score)
            if score >= min_score:
                results.append((memory, float(score)))

        results.sort(key=lambda item: item[1], reverse=True)
        return results[:top_k]

    def _raw_text_score(self, normalized_query: str, query_chars: set[str], doc_content: str) -> float:
        normalized_doc = self._normalize_raw_text(doc_content)
        if not normalized_query or not normalized_doc:
            return 0.0
        if normalized_query == normalized_doc:
            return 1.0
        if normalized_query in normalized_doc or normalized_doc in normalized_query:
            return min(len(normalized_query), len(normalized_doc)) / max(len(normalized_query), len(normalized_doc))
        return self._overlap_coefficient(query_chars, self._char_set(normalized_doc))

    def _normalize_raw_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "").strip().lower())

    def _char_set(self, text: str) -> set[str]:
        return set(str(text or ""))

    def _overlap_coefficient(self, left: set[str], right: set[str]) -> float:
        if not left or not right:
            return 0.0
        return len(left & right) / min(len(left), len(right))

    def extract_keywords(self, query: str, top_k: int = 10) -> List[str]:
        """从查询中提取关键词。"""
        return self._tokenizer.extract_keywords(query, top_k)

    def get_stats(self, user_id: str) -> Dict:
        """获取索引统计信息。"""
        index_data = self._indices.get(user_id)
        if not index_data:
            return {"status": "not_found"}

        return {
            "status": "ready" if index_data.bm25 else ("fallback" if index_data.memories else "empty"),
            "memory_count": len(index_data.memories),
            "total_tokens": sum(len(tokens) for tokens in index_data.tokenized_docs),
        }

    def invalidate(self, user_id: str):
        """让指定用户的索引失效。"""
        if user_id in self._indices:
            del self._indices[user_id]
            logger.debug("[BM25] BM25 索引已失效")


@dataclass
class IndexData:
    """索引内部数据结构。"""

    memories: List[MemoryItem]
    tokenized_docs: List[List[str]]
    bm25: Optional[BM25Okapi] = None
    fallback_only: bool = False


