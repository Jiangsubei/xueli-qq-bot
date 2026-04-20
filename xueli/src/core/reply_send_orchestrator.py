from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterable, List, Sequence


@dataclass(frozen=True)
class ReplyPartPlan:
    text: str
    delay_before_seconds: float = 0.0


class ReplySendOrchestrator:
    """Normalize reply segments and compute per-part delays."""

    def __init__(self, *, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()

    def build_part_plan(
        self,
        *,
        segments: Sequence[str] | None,
        fallback_text: str,
        max_segments: int,
        first_segment_delay_min_ms: int,
        first_segment_delay_max_ms: int,
        followup_delay_min_seconds: float,
        followup_delay_max_seconds: float,
    ) -> List[ReplyPartPlan]:
        normalized = self.normalize_segments(
            segments=segments,
            fallback_text=fallback_text,
            max_segments=max_segments,
        )
        plans: List[ReplyPartPlan] = []
        for index, text in enumerate(normalized):
            if index == 0:
                if len(normalized) <= 1:
                    delay = 0.0
                else:
                    delay = self._uniform_seconds(
                        float(first_segment_delay_min_ms or 0) / 1000.0,
                        float(first_segment_delay_max_ms or 0) / 1000.0,
                    )
            else:
                delay = self._uniform_seconds(
                    float(followup_delay_min_seconds or 0.0),
                    float(followup_delay_max_seconds or 0.0),
                )
            plans.append(ReplyPartPlan(text=text, delay_before_seconds=delay))
        return plans

    def normalize_segments(
        self,
        *,
        segments: Sequence[str] | None,
        fallback_text: str,
        max_segments: int,
    ) -> List[str]:
        cleaned = self._clean_texts(segments or [])
        if not cleaned:
            fallback = str(fallback_text or "").strip()
            return [fallback] if fallback else []
        if max_segments > 0:
            cleaned = cleaned[: max(1, int(max_segments))]
        return cleaned

    def _clean_texts(self, items: Iterable[str]) -> List[str]:
        result: List[str] = []
        previous = ""
        for raw in list(items or []):
            text = str(raw or "").strip()
            if not text:
                continue
            if text == previous:
                continue
            previous = text
            result.append(text)
        return result

    def _uniform_seconds(self, minimum: float, maximum: float) -> float:
        lower = max(0.0, float(minimum or 0.0))
        upper = max(0.0, float(maximum or 0.0))
        if upper < lower:
            lower, upper = upper, lower
        if upper == lower:
            return lower
        return self._rng.uniform(lower, upper)
