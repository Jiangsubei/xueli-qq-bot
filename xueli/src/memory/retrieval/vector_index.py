from __future__ import annotations

import math
from collections import Counter
from typing import Dict, List, Tuple

from ..storage.markdown_store import MemoryItem


def _character_ngrams(text: str, n: int = 2) -> Counter:
    chars = list(text)
    if len(chars) < n:
        return Counter()
    return Counter("".join(chars[i : i + n]) for i in range(len(chars) - n + 1))


class VectorIndex:
    """Lightweight vector index using character n-gram embeddings and cosine similarity.

    No external dependencies — uses pure Python. Designed as a soft-semantic
    supplement to BM25 for improved recall of semantically related memories.
    The n-gram approach captures surface-form similarity (shared substrings
    between query and memory content) which works well for CJK text.

    Per-user sharding: each user_id gets its own vectors/items/vocabulary
    to prevent concurrent build_index() calls from wiping each other.
    """

    def __init__(self, ngram_size: int = 2):
        self.ngram_size = max(2, int(ngram_size))
        self._indices: Dict[str, Dict[str, List[float]]] = {}
        self._items_dict: Dict[str, Dict[str, MemoryItem]] = {}
        self._vocabularies: Dict[str, List[str]] = {}

    def build_index(self, user_id: str, memories: List[MemoryItem]) -> None:
        """Build n-gram vectors for all memories of a specific user."""
        vectors: Dict[str, List[float]] = {}
        items: Dict[str, MemoryItem] = {}
        for mem in memories:
            if not mem.content.strip():
                continue
            items[mem.id] = mem

        vocabulary = self._build_vocabulary(memories)
        if not vocabulary:
            self._indices[user_id] = vectors
            self._items_dict[user_id] = items
            self._vocabularies[user_id] = vocabulary
            return

        for mem_id, mem in items.items():
            ngrams = _character_ngrams(mem.content, self.ngram_size)
            total = sum(ngrams.values()) or 1
            vectors[mem_id] = [
                math.log1p(ngrams.get(token, 0)) / math.log1p(total)
                for token in vocabulary
            ]

        self._indices[user_id] = vectors
        self._items_dict[user_id] = items
        self._vocabularies[user_id] = vocabulary

    def _build_vocabulary(self, memories: List[MemoryItem]) -> List[str]:
        counter: Counter = Counter()
        for mem in memories:
            if mem.content.strip():
                counter.update(_character_ngrams(mem.content, self.ngram_size))
        min_freq = max(1, int(len(memories) * 0.05))
        return [token for token, count in counter.items() if count >= min_freq][:1024]

    def search(
        self,
        user_id: str,
        query: str,
        top_k: int = 20,
        min_score: float = 0.0,
    ) -> List[Tuple[MemoryItem, float]]:
        """Search by cosine similarity between query and memory vectors."""
        vectors = self._indices.get(user_id, {})
        items = self._items_dict.get(user_id, {})
        vocabulary = self._vocabularies.get(user_id, [])

        if not vectors or not vocabulary:
            return []

        query_ngrams = _character_ngrams(query, self.ngram_size)
        total = sum(query_ngrams.values()) or 1

        query_vec = [query_ngrams.get(token, 0) / total for token in vocabulary]
        query_norm = math.sqrt(sum(v * v for v in query_vec)) or 1.0

        scored: List[Tuple[MemoryItem, float]] = []
        for mem_id, vec in vectors.items():
            dot = sum(a * b for a, b in zip(query_vec, vec))
            vec_norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            denom = query_norm * vec_norm
            score = (dot / denom) if denom > 0 else 0.0
            if score >= min_score:
                mem = items.get(mem_id)
                if mem:
                    scored.append((mem, score))

        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_k]

    def clear(self) -> None:
        self._indices.clear()
        self._items_dict.clear()
        self._vocabularies.clear()
