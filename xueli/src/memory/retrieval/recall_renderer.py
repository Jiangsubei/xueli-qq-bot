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
    fuzzy_expressions: List[str] = field(default_factory=list)
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
        if not self.enabled or not self.fuzzy_expressions:
            return ""
        prefix_list = "、".join(f"“{expr}”" for expr in self.fuzzy_expressions[:4])
        return (
            "【回忆提示】当你在回复中引用以下回忆时，请使用模糊的表达方式，"
            "不要逐字复述细节。偶尔加入不确定的口吻，例如："
            f"{prefix_list}。"
            "让回忆听起来像是你脑海中自然浮现的，而非精确检索出的。"
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
