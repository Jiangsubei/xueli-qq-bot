from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional


@dataclass
class RecallRenderer:
    """Fuzzy recall rendering: wraps precise recall context with uncertain expressions."""

    enabled: bool = False
    fuzzy_probability: float = 0.3
    confidence_threshold: float = 0.7
    confidence_decay_per_day: float = 0.01
    confidence_minimum: float = 0.3

    def compute_confidence(self, created_at: str, updated_at: Optional[str] = None) -> float:
        if not created_at:
            return 1.0
        try:
            if created_at.endswith("Z"):
                created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            else:
                created_dt = datetime.fromisoformat(created_at)
        except (ValueError, TypeError):
            return 1.0
        now = datetime.now(timezone.utc)
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)
        days = (now - created_dt).total_seconds() / 86400.0
        confidence = 1.0 - days * self.confidence_decay_per_day
        return max(self.confidence_minimum, confidence)

    def should_fuzzify(self, confidence: float) -> bool:
        if not self.enabled:
            return False
        if confidence >= self.confidence_threshold:
            return False
        return random.random() < self.fuzzy_probability

    def render_fuzzy_instruction(self) -> str:
        """返回通用指导，让模型自行决定如何表达模糊感。"""
        if not self.enabled:
            return ""
        return (
            "【回忆提示】当你在回复中引用记忆内容时，请使用自然、模糊的口吻，"
            "让回忆听起来像是你脑海中自然浮现的片段，而非精确检索出的内容。"
            "不要逐字复述细节，适当加入你自己的想法和感受，"
            "用你自己的方式表达不确定感，不需要使用任何预设的开场白。"
        )

    def wrap_recall_context(self, raw_text: str) -> str:
        if not self.enabled:
            return raw_text
        instruction = self.render_fuzzy_instruction()
        if not instruction:
            return raw_text
        return f"{instruction}\n\n{raw_text}"

    def apply(self, recall_context: str) -> str:
        text = str(recall_context or "").strip()
        if not text:
            return ""
        if not self.enabled:
            return text
        return self.wrap_recall_context(text)
