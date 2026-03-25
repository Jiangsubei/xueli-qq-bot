"""
检索层 - 两阶段检索。

第一阶段使用 BM25 召回，第二阶段可选使用本地或远程模型精排。
"""
import abc
import asyncio
import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

import aiohttp

from ..storage.markdown_store import MemoryItem
from .bm25_index import BM25Index, SearchResult

logger = logging.getLogger(__name__)


@dataclass
class RetrievalConfig:
    """检索配置。"""

    bm25_top_k: int = 100
    bm25_min_score: float = 0.0
    rerank_enabled: bool = False
    rerank_top_k: int = 20
    reranker_type: str = "local"
    local_model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    api_endpoint: str = ""
    api_key: str = ""
    api_timeout: float = 5.0


class BaseReranker(abc.ABC):
    """精排器基类。"""

    @abc.abstractmethod
    async def rerank(
        self,
        query: str,
        candidates: List[Tuple[MemoryItem, float]],
        top_k: int = 20,
    ) -> List[Tuple[MemoryItem, float]]:
        """对候选记忆进行精排。"""


class CrossEncoderReranker(BaseReranker):
    """本地 CrossEncoder 精排器。"""

    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
        self.model_name = model_name
        self._model = None
        self._tokenizer = None
        self._lock = asyncio.Lock()

    async def _load_model(self):
        """延迟加载模型。"""
        if self._model is not None:
            return

        async with self._lock:
            if self._model is not None:
                return

            try:
                loop = asyncio.get_event_loop()
                self._model, self._tokenizer = await loop.run_in_executor(
                    None,
                    self._load_model_sync,
                )
                logger.info("本地精排模型已加载: model=%s", self.model_name)
            except Exception as e:
                logger.error("本地精排模型加载失败: model=%s, 错误=%s", self.model_name, e)
                raise

    def _load_model_sync(self):
        """在线程池中同步加载模型。"""
        try:
            from sentence_transformers import CrossEncoder

            model = CrossEncoder(self.model_name)
            return model, None
        except ImportError:
            logger.error("缺少 sentence-transformers，无法启用本地精排")
            raise

    async def rerank(
        self,
        query: str,
        candidates: List[Tuple[MemoryItem, float]],
        top_k: int = 20,
    ) -> List[Tuple[MemoryItem, float]]:
        """使用 CrossEncoder 对候选结果精排。"""
        if not candidates:
            return []

        await self._load_model()

        if self._model is None:
            logger.warning("本地精排不可用，回退原始排序")
            return candidates[:top_k]

        try:
            pairs = [(query, mem.content) for mem, _ in candidates]
            loop = asyncio.get_event_loop()
            scores = await loop.run_in_executor(None, self._model.predict, pairs)

            results = []
            for i, (mem, bm25_score) in enumerate(candidates):
                rerank_score = float(scores[i])
                combined_score = rerank_score * 0.7 + (bm25_score / 100) * 0.3
                results.append((mem, rerank_score, combined_score))

            results.sort(key=lambda item: item[2], reverse=True)
            return [(mem, score) for mem, score, _ in results[:top_k]]
        except Exception as e:
            logger.error("本地精排失败，回退原始排序: 错误=%s", e)
            return candidates[:top_k]


class APIReranker(BaseReranker):
    """远程 API 精排器。"""

    def __init__(
        self,
        endpoint: str,
        api_key: str = "",
        timeout: float = 5.0,
    ):
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建 HTTP 会话。"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def rerank(
        self,
        query: str,
        candidates: List[Tuple[MemoryItem, float]],
        top_k: int = 20,
    ) -> List[Tuple[MemoryItem, float]]:
        """调用远程接口进行精排。"""
        if not candidates:
            return []

        try:
            session = await self._get_session()
            documents = [
                {
                    "id": mem.id,
                    "content": mem.content,
                    "metadata": {
                        "bm25_score": bm25_score,
                        "source": mem.source,
                    },
                }
                for mem, bm25_score in candidates
            ]

            payload = {
                "query": query,
                "documents": documents,
                "top_k": top_k,
                "return_scores": True,
            }

            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            async with session.post(
                self.endpoint,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as response:
                if response.status != 200:
                    logger.error(
                        "API 精排失败: status=%s, endpoint=%s",
                        response.status,
                        self.endpoint,
                    )
                    return candidates[:top_k]

                data = await response.json()
                results = []
                id_to_memory = {mem.id: mem for mem, _ in candidates}

                for item in data.get("results", []):
                    mem_id = item.get("id")
                    score = item.get("score", 0.0)
                    if mem_id in id_to_memory:
                        results.append((id_to_memory[mem_id], score))

                return results if results else candidates[:top_k]
        except asyncio.TimeoutError:
            logger.warning("API 精排超时，回退原始排序: endpoint=%s", self.endpoint)
            return candidates[:top_k]
        except Exception as e:
            logger.error("API 精排失败: endpoint=%s, 错误=%s", self.endpoint, e)
            return candidates[:top_k]

    async def close(self):
        """关闭 HTTP 会话。"""
        if self._session and not self._session.closed:
            await self._session.close()


class TwoStageRetriever:
    """两阶段记忆检索器。"""

    def __init__(
        self,
        bm25_index: BM25Index,
        config: Optional[RetrievalConfig] = None,
    ):
        self.bm25_index = bm25_index
        self.config = config or RetrievalConfig()
        self._reranker: Optional[BaseReranker] = None

    def initialize_reranker(self):
        """根据配置初始化精排器。"""
        if not self.config.rerank_enabled:
            return

        try:
            if self.config.reranker_type == "local":
                self._reranker = CrossEncoderReranker(
                    model_name=self.config.local_model_name
                )
                logger.info("已启用本地精排: model=%s", self.config.local_model_name)
            elif self.config.reranker_type == "api":
                self._reranker = APIReranker(
                    endpoint=self.config.api_endpoint,
                    api_key=self.config.api_key,
                    timeout=self.config.api_timeout,
                )
                logger.info("已启用 API 精排: endpoint=%s", self.config.api_endpoint)
            else:
                logger.warning("未知精排类型: type=%s", self.config.reranker_type)
        except Exception as e:
            logger.error("初始化精排器失败: 错误=%s", e)
            self._reranker = None

    async def retrieve(
        self,
        user_id: str,
        query: str,
        top_k: Optional[int] = None,
    ) -> List[SearchResult]:
        """执行两阶段检索。"""
        if self.config.rerank_enabled:
            final_top_k = top_k or self.config.rerank_top_k
        else:
            final_top_k = top_k or self.config.bm25_top_k

        recall_k = self.config.bm25_top_k if self.config.rerank_enabled else final_top_k

        candidates = self.bm25_index.search(
            user_id=user_id,
            query=query,
            top_k=recall_k,
            min_score=self.config.bm25_min_score,
        )

        if not candidates:
            logger.debug("记忆召回为空: user=%s", user_id)
            return []

        logger.debug("BM25 召回完成: user=%s, 候选=%s", user_id, len(candidates))

        if self.config.rerank_enabled and self._reranker and len(candidates) > 1:
            try:
                reranked = await self._reranker.rerank(
                    query=query,
                    candidates=candidates,
                    top_k=final_top_k,
                )

                results = []
                for mem, rerank_score in reranked:
                    bm25_score = next((score for item, score in candidates if item.id == mem.id), 0.0)
                    results.append(
                        SearchResult(
                            memory=mem,
                            bm25_score=bm25_score,
                            rerank_score=rerank_score,
                        )
                    )

                logger.debug("精排完成: user=%s, 返回=%s", user_id, len(results))
                return results
            except Exception as e:
                logger.error("精排失败，回退 BM25: user=%s, 错误=%s", user_id, e)

        return [
            SearchResult(
                memory=mem,
                bm25_score=bm25_score,
            )
            for mem, bm25_score in candidates[:final_top_k]
        ]

    async def quick_check(
        self,
        user_id: str,
        query: str,
        threshold: float = 0.5,
    ) -> Optional[MemoryItem]:
        """快速检查是否存在相关记忆。"""
        results = self.bm25_index.search(
            user_id=user_id,
            query=query,
            top_k=1,
            min_score=threshold,
        )
        if results:
            return results[0][0]
        return None

    async def close(self):
        """关闭底层资源。"""
        if isinstance(self._reranker, APIReranker):
            await self._reranker.close()
