from __future__ import annotations

import abc
import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from src.services.ai_client import AIClient

from ..storage.markdown_store import MemoryItem
from .bm25_index import BM25Index, SearchResult

logger = logging.getLogger(__name__)


@dataclass
class RetrievalConfig:
    bm25_top_k: int = 100
    bm25_min_score: float = 0.0
    rerank_enabled: bool = False
    rerank_top_k: int = 20
    reranker_type: str = "api"
    local_model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    api_endpoint: str = ""
    api_key: str = ""
    api_model: str = ""
    api_extra_params: Dict[str, Any] = field(default_factory=dict)
    api_extra_headers: Dict[str, str] = field(default_factory=dict)
    api_response_path: str = "choices.0.message.content"
    api_timeout: float = 5.0


class BaseReranker(abc.ABC):
    @abc.abstractmethod
    async def rerank(
        self,
        query: str,
        candidates: List[Tuple[MemoryItem, float]],
        top_k: int = 20,
    ) -> List[Tuple[MemoryItem, float]]:
        raise NotImplementedError


class CrossEncoderReranker(BaseReranker):
    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
        self.model_name = model_name
        self._model = None
        self._lock = asyncio.Lock()

    async def _load_model(self) -> None:
        if self._model is not None:
            return
        async with self._lock:
            if self._model is not None:
                return
            from sentence_transformers import CrossEncoder
            loop = asyncio.get_event_loop()
            self._model = await loop.run_in_executor(None, CrossEncoder, self.model_name)

    async def rerank(
        self,
        query: str,
        candidates: List[Tuple[MemoryItem, float]],
        top_k: int = 20,
    ) -> List[Tuple[MemoryItem, float]]:
        if not candidates:
            return []
        await self._load_model()
        if self._model is None:
            return candidates[:top_k]
        try:
            pairs = [(query, mem.content) for mem, _ in candidates]
            loop = asyncio.get_event_loop()
            scores = await loop.run_in_executor(None, self._model.predict, pairs)
            results = []
            for i, (mem, bm25_score) in enumerate(candidates):
                rerank_score = float(scores[i])
                combined_score = rerank_score * 0.7 + (bm25_score / 100.0) * 0.3
                results.append((mem, rerank_score, combined_score))
            results.sort(key=lambda item: item[2], reverse=True)
            return [(mem, score) for mem, score, _ in results[:top_k]]
        except Exception as exc:
            logger.error("local rerank failed: %s", exc)
            return candidates[:top_k]


class APIReranker(BaseReranker):
    def __init__(
        self,
        endpoint: str,
        api_key: str = "",
        model: str = "",
        extra_params: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        response_path: str = "choices.0.message.content",
        timeout: float = 5.0,
    ):
        self.endpoint = str(endpoint or "").rstrip("/")
        self._client = AIClient(
            api_base=self.endpoint,
            api_key=api_key,
            model=model,
            timeout=max(1, int(timeout)),
            extra_params=extra_params or {},
            extra_headers=extra_headers or {},
            response_path=response_path or "choices.0.message.content",
            log_label="memory_rerank",
        )

    def _build_system_prompt(self) -> str:
        return (
            "You are a memory reranker. Rank candidate memories by relevance to the query. "
            "Return JSON only in the form {\"results\": [{\"id\": \"memory-id\", \"score\": 0.95}]}. "
            "Keep only the most relevant items and sort from best to worst."
        )

    def _build_user_prompt(self, query: str, candidates: List[Tuple[MemoryItem, float]], top_k: int) -> str:
        lines = [f"query: {query}", f"top_k: {top_k}", "candidates:"]
        for mem, bm25_score in candidates:
            content = str(mem.content or "").strip().replace("\n", " ")
            lines.append(f"- id={mem.id}; bm25={bm25_score:.4f}; content={content}")
        return "\n".join(lines)

    def _parse_response(self, content: str) -> List[Tuple[str, float]]:
        text = str(content or "").strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.MULTILINE).strip()
        payload = json.loads(text)
        items = payload if isinstance(payload, list) else payload.get("results", [])
        results: List[Tuple[str, float]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            mem_id = str(item.get("id") or "").strip()
            if not mem_id:
                continue
            try:
                score = float(item.get("score", 0.0))
            except (TypeError, ValueError):
                score = 0.0
            results.append((mem_id, score))
        return results

    async def rerank(
        self,
        query: str,
        candidates: List[Tuple[MemoryItem, float]],
        top_k: int = 20,
    ) -> List[Tuple[MemoryItem, float]]:
        if not candidates:
            return []
        try:
            response = await self._client.chat_completion(
                messages=[
                    self._client.build_text_message("system", self._build_system_prompt()),
                    self._client.build_text_message("user", self._build_user_prompt(query, candidates, top_k)),
                ],
                temperature=0.1,
                max_tokens=1200,
            )
            ranked = self._parse_response(getattr(response, "content", ""))
        except Exception as exc:
            logger.error("API rerank failed: endpoint=%s error=%s", self.endpoint, exc)
            return candidates[:top_k]

        if not ranked:
            return candidates[:top_k]

        id_to_memory = {mem.id: mem for mem, _ in candidates}
        ordered: List[Tuple[MemoryItem, float]] = []
        for mem_id, score in ranked:
            memory = id_to_memory.get(mem_id)
            if memory is not None:
                ordered.append((memory, score))
        return ordered[:top_k] if ordered else candidates[:top_k]

    async def close(self):
        await self._client.close()


class TwoStageRetriever:
    def __init__(
        self,
        bm25_index: BM25Index,
        config: Optional[RetrievalConfig] = None,
    ):
        self.bm25_index = bm25_index
        self.config = config or RetrievalConfig()
        self._reranker: Optional[BaseReranker] = None

    def initialize_reranker(self):
        if not self.config.rerank_enabled:
            return

        try:
            if self.config.reranker_type == "local":
                self._reranker = CrossEncoderReranker(model_name=self.config.local_model_name)
                logger.info("initialized local reranker: model=%s", self.config.local_model_name)
            else:
                self._reranker = APIReranker(
                    endpoint=self.config.api_endpoint,
                    api_key=self.config.api_key,
                    model=self.config.api_model,
                    extra_params=self.config.api_extra_params,
                    extra_headers=self.config.api_extra_headers,
                    response_path=self.config.api_response_path,
                    timeout=self.config.api_timeout,
                )
                logger.info("initialized API reranker: endpoint=%s model=%s", self.config.api_endpoint, self.config.api_model)
        except Exception as exc:
            logger.error("initialize reranker failed: %s", exc)
            self._reranker = None

    async def retrieve(
        self,
        user_id: str,
        query: str,
        top_k: Optional[int] = None,
    ) -> List[SearchResult]:
        final_top_k = top_k or (self.config.rerank_top_k if self.config.rerank_enabled else self.config.bm25_top_k)
        recall_k = self.config.bm25_top_k if self.config.rerank_enabled else final_top_k

        candidates = self.bm25_index.search(
            user_id=user_id,
            query=query,
            top_k=recall_k,
            min_score=self.config.bm25_min_score,
        )

        if not candidates:
            logger.debug("memory recall empty: user=%s", user_id)
            return []

        if self.config.rerank_enabled and self._reranker and len(candidates) > 1:
            try:
                reranked = await self._reranker.rerank(query=query, candidates=candidates, top_k=final_top_k)
                results = []
                for mem, rerank_score in reranked:
                    bm25_score = next((score for item, score in candidates if item.id == mem.id), 0.0)
                    results.append(SearchResult(memory=mem, bm25_score=bm25_score, rerank_score=rerank_score))
                return results
            except Exception as exc:
                logger.error("rerank failed, fallback to bm25: user=%s error=%s", user_id, exc)

        return [SearchResult(memory=mem, bm25_score=bm25_score) for mem, bm25_score in candidates[:final_top_k]]

    async def quick_check(
        self,
        user_id: str,
        query: str,
        threshold: float = 0.5,
    ) -> Optional[MemoryItem]:
        results = self.bm25_index.search(user_id=user_id, query=query, top_k=1, min_score=threshold)
        if results:
            return results[0][0]
        return None

    async def close(self):
        if isinstance(self._reranker, APIReranker):
            await self._reranker.close()
