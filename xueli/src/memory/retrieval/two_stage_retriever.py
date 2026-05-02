from __future__ import annotations

import abc
import asyncio
import importlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.core.model_invocation_router import ModelInvocationRouter, ModelInvocationType
from src.services.ai_client import AIClient
from src.memory_limits import MIN_RERANK_CANDIDATE_MAX_CHARS, MIN_RERANK_TOTAL_PROMPT_BUDGET

from ..storage.markdown_store import MemoryItem
from .bm25_index import BM25Index, SearchResult
from .vector_index import VectorIndex

logger = logging.getLogger(__name__)


@dataclass
class RetrievalConfig:
    bm25_top_k: int = 100
    bm25_min_score: float = 0.0
    rerank_enabled: bool = False
    rerank_top_k: int = 20
    pre_rerank_top_k: int = 12
    dynamic_memory_limit: int = 8
    dynamic_dedup_enabled: bool = True
    dynamic_dedup_similarity_threshold: float = 0.72
    rerank_candidate_max_chars: int = 160
    rerank_total_prompt_budget: int = 2400
    reranker_type: str = "api"
    local_model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    api_endpoint: str = ""
    api_key: str = ""
    api_model: str = ""
    api_extra_params: Dict[str, Any] = field(default_factory=dict)
    api_extra_headers: Dict[str, str] = field(default_factory=dict)
    api_response_path: str = "choices.0.message.content"
    api_timeout: float = 5.0
    model_invocation_router: Optional[ModelInvocationRouter] = None
    local_bm25_weight: float = 1.0
    local_importance_weight: float = 0.35
    local_mention_weight: float = 0.2
    local_recency_weight: float = 0.15
    local_scene_weight: float = 0.3
    vector_weight: float = 0.4


@dataclass(frozen=True)
class RetrievalContext:
    requester_user_id: str = ""
    message_type: str = "private"
    group_id: str = ""
    read_scope: str = "user"


class BaseReranker(abc.ABC):
    @abc.abstractmethod
    async def rerank(
        self,
        query: str,
        candidates: List[Tuple[MemoryItem, float]],
        top_k: int = 20,
        retrieval_context: Optional[RetrievalContext] = None,
    ) -> List[Tuple[MemoryItem, float]]:
        raise NotImplementedError


class CrossEncoderReranker(BaseReranker):
    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
        self.model_name = model_name
        self._model: Optional[Any] = None
        self._lock = asyncio.Lock()

    async def _load_model(self) -> None:
        if self._model is not None:
            return
        async with self._lock:
            if self._model is not None:
                return
            try:
                cross_encoder_module = importlib.import_module("sentence_transformers")
                CrossEncoder = getattr(cross_encoder_module, "CrossEncoder")
            except ImportError:
                logger.warning("未安装 sentence-transformers，本地重排器不可用")
                return
            loop = asyncio.get_event_loop()
            self._model = await loop.run_in_executor(None, CrossEncoder, self.model_name)

    async def rerank(
        self,
        query: str,
        candidates: List[Tuple[MemoryItem, float]],
        top_k: int = 20,
        retrieval_context: Optional[RetrievalContext] = None,
    ) -> List[Tuple[MemoryItem, float]]:
        del retrieval_context
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
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("本地重排失败：%s", exc)
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
        candidate_max_chars: int = 160,
        total_prompt_budget: int = 2400,
        model_invocation_router: Optional[ModelInvocationRouter] = None,
    ):
        self.endpoint = str(endpoint or "").rstrip("/")
        self.candidate_max_chars = max(MIN_RERANK_CANDIDATE_MAX_CHARS, int(candidate_max_chars or 160))
        self.total_prompt_budget = max(MIN_RERANK_TOTAL_PROMPT_BUDGET, int(total_prompt_budget or 2400))
        self.timeout = max(0.001, float(timeout or 5.0))
        self._model_invocation_router = model_invocation_router
        self._client = AIClient(
            api_base=self.endpoint,
            api_key=api_key,
            model=model,
            timeout=max(1, int(self.timeout)),
            extra_params=extra_params or {},
            extra_headers=extra_headers or {},
            response_path=response_path or "choices.0.message.content",
            log_label="memory_rerank",
        )

    def _build_system_prompt(self) -> str:
        return (
            "You are a memory reranker. Rank candidate memories by semantic relevance and scene fitness. "
            "Prioritize memories that best match the query meaning, the current conversation scene, and long-term importance. "
            "Important long-term preferences, user boundaries, and stable facts should rank above weak lexical matches. "
            "Return JSON only in the form {\"results\": [{\"id\": \"memory-id\", \"score\": 0.95}]}. "
            "Sort from best to worst and assign lower scores to scene-mismatched or weakly related memories."
        )

    def _build_user_prompt(
        self,
        query: str,
        candidates: List[Tuple[MemoryItem, float]],
        top_k: int,
        retrieval_context: Optional[RetrievalContext],
    ) -> str:
        context = retrieval_context or RetrievalContext()
        lines = [
            f"query: {query}",
            f"top_k: {top_k}",
            "request_context:",
            f"- requester_user_id={context.requester_user_id or ''}",
            f"- message_type={context.message_type or 'private'}",
            f"- group_id={context.group_id or ''}",
            f"- read_scope={context.read_scope or 'user'}",
            "candidates:",
        ]
        used = sum(len(line) + 1 for line in lines)
        for mem, local_score in candidates:
            candidate_line = self._build_candidate_line(mem=mem, local_score=local_score)
            if len(lines) > 8 and used + len(candidate_line) + 1 > self.total_prompt_budget:
                break
            lines.append(candidate_line)
            used += len(candidate_line) + 1
        return "\n".join(lines)

    def _build_candidate_line(self, *, mem: MemoryItem, local_score: float) -> str:
        metadata = dict(getattr(mem, "metadata", {}) or {})
        content = self._truncate_candidate_content(str(mem.content or ""), self.candidate_max_chars)
        return (
            "- id={id}; local_score={local:.4f}; type={memory_type}; importance={importance}; mention_count={mention_count}; "
            "source_message_type={source_message_type}; source_group_id={source_group_id}; owner_user_id={owner_user_id}; updated_at={updated_at}; content={content}"
        ).format(
            id=mem.id,
            local=local_score,
            memory_type=str(metadata.get("memory_type", "legacy") or "legacy"),
            importance=metadata.get("importance", 3),
            mention_count=metadata.get("mention_count", 1),
            source_message_type=str(metadata.get("source_message_type", "") or ""),
            source_group_id=str(metadata.get("source_group_id", metadata.get("group_id", "")) or ""),
            owner_user_id=str(getattr(mem, "owner_user_id", "") or metadata.get("owner_user_id", "") or ""),
            updated_at=str(getattr(mem, "updated_at", "") or ""),
            content=content,
        )

    def _truncate_candidate_content(self, content: str, max_chars: int) -> str:
        normalized = re.sub(r"\s+", " ", str(content or "").strip())
        if len(normalized) <= max_chars:
            return normalized
        return normalized[: max(1, max_chars - 3)].rstrip() + "..."

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
        retrieval_context: Optional[RetrievalContext] = None,
    ) -> List[Tuple[MemoryItem, float]]:
        if not candidates:
            return []
        try:
            async def run_chat():
                return await self._client.chat_completion(
                    messages=[
                        self._client.build_text_message("system", self._build_system_prompt()),
                        self._client.build_text_message("user", self._build_user_prompt(query, candidates, top_k, retrieval_context)),
                    ],
                    temperature=0.1,
                    max_tokens=1200,
                )

            if self._model_invocation_router is not None:
                response = await self._model_invocation_router.submit(
                    purpose=ModelInvocationType.MEMORY_RERANK,
                    trace_id="",
                    session_key="",
                    message_id="",
                    label="记忆重排",
                    timeout_seconds=self.timeout,
                    runner=run_chat,
                )
            else:
                response = await run_chat()
            ranked = self._parse_response(getattr(response, "content", ""))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("API 重排失败：地址=%s，错误=%s", self.endpoint, exc)
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
        vector_index: Optional[VectorIndex] = None,
    ):
        self.bm25_index = bm25_index
        self.config = config or RetrievalConfig()
        self.vector_index = vector_index
        self._reranker: Optional[BaseReranker] = None
        self._vector_weight = self.config.vector_weight

    def initialize_reranker(self):
        if not self.config.rerank_enabled:
            return

        try:
            if self.config.reranker_type == "local":
                self._reranker = CrossEncoderReranker(model_name=self.config.local_model_name)
                logger.debug("本地重排器初始化完成：模型=%s", self.config.local_model_name)
            else:
                self._reranker = APIReranker(
                    endpoint=self.config.api_endpoint,
                    api_key=self.config.api_key,
                    model=self.config.api_model,
                    extra_params=self.config.api_extra_params,
                    extra_headers=self.config.api_extra_headers,
                    response_path=self.config.api_response_path,
                    timeout=self.config.api_timeout,
                    candidate_max_chars=self.config.rerank_candidate_max_chars,
                    total_prompt_budget=self.config.rerank_total_prompt_budget,
                    model_invocation_router=self.config.model_invocation_router,
                )
                logger.debug("API 重排器初始化完成：地址=%s，模型=%s", self.config.api_endpoint, self.config.api_model)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("初始化重排器失败：%s", exc)
            self._reranker = None

    async def retrieve(
        self,
        user_id: str,
        query: str,
        top_k: Optional[int] = None,
        retrieval_context: Optional[RetrievalContext] = None,
    ) -> List[SearchResult]:
        final_top_k = top_k or (self.config.rerank_top_k if self.config.rerank_enabled else self.config.bm25_top_k)
        recall_k = self.config.bm25_top_k if self.config.rerank_enabled else final_top_k

        candidates = self.bm25_index.search(
            user_id=user_id,
            query=query,
            top_k=recall_k,
            min_score=self.config.bm25_min_score,
        )

        vector_results: Dict[str, float] = {}
        if self.vector_index:
            try:
                vec_hits = self.vector_index.search(user_id=user_id, query=query, top_k=recall_k)
                for mem, score in vec_hits:
                    if mem.id not in vector_results:
                        vector_results[mem.id] = score
                    else:
                        vector_results[mem.id] = max(vector_results[mem.id], score)
            except Exception as exc:
                logger.debug("向量检索失败（非致命）：%s", exc)

        if vector_results:
            bm25_map: Dict[str, Tuple[MemoryItem, float]] = {mem.id: (mem, score) for mem, score in candidates}
            for mem_id, vec_score in vector_results.items():
                if mem_id not in bm25_map:
                    for mem, _ in candidates:
                        if mem.id == mem_id:
                            bm25_map[mem_id] = (mem, 0.0)
                            break
                if mem_id in bm25_map:
                    _, bm25_score = bm25_map[mem_id]
                    fused_score = bm25_score * (1.0 - self._vector_weight) + vec_score * self._vector_weight
                    bm25_map[mem_id] = (bm25_map[mem_id][0], fused_score)
            candidates = list(bm25_map.values())
            candidates.sort(key=lambda item: item[1], reverse=True)

        if not candidates:
            logger.debug("记忆召回为空：用户=%s", user_id)
            return []

        locally_ranked = self._apply_local_ranking(
            candidates=candidates,
            retrieval_context=retrieval_context,
        )

        if self.config.rerank_enabled and self._reranker and len(candidates) > 1:
            try:
                rerank_candidates = locally_ranked[: max(final_top_k, self.config.pre_rerank_top_k)]
                reranked = await self._reranker.rerank(
                    query=query,
                    candidates=rerank_candidates,
                    top_k=final_top_k,
                    retrieval_context=retrieval_context,
                )
                results = []
                for mem, rerank_score in reranked:
                    bm25_score = next((score for item, score in candidates if item.id == mem.id), 0.0)
                    local_score = next((score for item, score in locally_ranked if item.id == mem.id), bm25_score)
                    results.append(
                        SearchResult(
                            memory=mem,
                            bm25_score=bm25_score,
                            local_score=local_score,
                            rerank_score=rerank_score,
                            combined_score=rerank_score,
                            ranking_stage="model_rerank",
                        )
                    )
                return results
            except Exception as exc:
                logger.error("记忆重排失败，回退到本地预排序：用户=%s，错误=%s", user_id, exc)

        return [
            SearchResult(
                memory=mem,
                bm25_score=next((score for item, score in candidates if item.id == mem.id), local_score),
                local_score=local_score,
                combined_score=local_score,
                ranking_stage="local_prerank",
            )
            for mem, local_score in locally_ranked[:final_top_k]
        ]

    def _apply_local_ranking(
        self,
        *,
        candidates: List[Tuple[MemoryItem, float]],
        retrieval_context: Optional[RetrievalContext],
    ) -> List[Tuple[MemoryItem, float]]:
        scored: List[Tuple[MemoryItem, float]] = []
        for memory, bm25_score in candidates:
            scored.append(
                (
                    memory,
                    self._compute_local_score(
                        memory=memory,
                        bm25_score=bm25_score,
                        retrieval_context=retrieval_context,
                    ),
                )
            )
        scored.sort(
            key=lambda item: (item[1], getattr(item[0], "updated_at", ""), getattr(item[0], "content", "")),
            reverse=True,
        )
        return scored

    def _compute_local_score(
        self,
        *,
        memory: MemoryItem,
        bm25_score: float,
        retrieval_context: Optional[RetrievalContext],
    ) -> float:
        metadata = dict(getattr(memory, "metadata", {}) or {})
        importance = self._normalize_importance_score(self._safe_float(metadata.get("importance"), 3.0))
        mention_count = self._normalize_mention_score(self._safe_float(metadata.get("mention_count"), 1.0))
        normalized_bm25 = self._normalize_bm25_score(bm25_score)
        scene_score = self._compute_scene_score(metadata=metadata, retrieval_context=retrieval_context)
        recency_score = self._compute_recency_score(getattr(memory, "updated_at", "") or getattr(memory, "created_at", ""))
        score = (
            normalized_bm25 * self.config.local_bm25_weight
            + importance * self.config.local_importance_weight
            + mention_count * self.config.local_mention_weight
            + recency_score * self.config.local_recency_weight
            + scene_score * self.config.local_scene_weight
        )
        if metadata.get("_index_archived", False):
            score *= 0.5
        return score

    def _compute_scene_score(
        self,
        *,
        metadata: Dict[str, Any],
        retrieval_context: Optional[RetrievalContext],
    ) -> float:
        if retrieval_context is None:
            return 0.0
        score = 0.0
        source_message_type = str(metadata.get("source_message_type", "") or "").strip().lower()
        source_group_id = str(metadata.get("source_group_id", "") or metadata.get("group_id", "") or "").strip()
        owner_user_id = str(metadata.get("owner_user_id", "") or "").strip()

        if source_message_type and source_message_type == retrieval_context.message_type:
            score += 1.0
        if retrieval_context.message_type == "group" and retrieval_context.group_id and source_group_id == retrieval_context.group_id:
            score += 1.5
        if retrieval_context.requester_user_id and owner_user_id == retrieval_context.requester_user_id:
            score += 0.8
        return score

    def _compute_recency_score(self, updated_at: str) -> float:
        text = str(updated_at or "").strip()
        if not text:
            return 0.0
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return 0.0
        age_days = max((datetime.now() - dt).total_seconds() / 86400.0, 0.0)
        return max(0.0, 1.0 - min(age_days / 30.0, 1.0))

    def _normalize_bm25_score(self, bm25_score: float) -> float:
        score = max(self._safe_float(bm25_score, 0.0), 0.0)
        return score / (score + 3.0) if score > 0 else 0.0

    def _normalize_importance_score(self, importance: float) -> float:
        bounded = min(max(importance, 1.0), 5.0)
        return bounded / 5.0

    def _normalize_mention_score(self, mention_count: float) -> float:
        count = max(mention_count, 0.0)
        return count / (count + 2.0) if count > 0 else 0.0

    def _safe_float(self, value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

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
