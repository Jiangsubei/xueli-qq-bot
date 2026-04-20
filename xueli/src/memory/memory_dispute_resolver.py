from __future__ import annotations

from typing import Any

from src.core.config import MemoryDisputeConfig
from src.core.models import MemoryDisputeDecision


class MemoryDisputeResolver:
    """Normalize reflection metadata into a stable dispute decision."""

    def __init__(self, config: MemoryDisputeConfig) -> None:
        self.config = config

    def resolve(self, reflection: Any) -> MemoryDisputeDecision:
        has_conflict = bool(getattr(reflection, "has_conflict", False))
        action = str(getattr(reflection, "action", "") or "").strip().lower()
        confidence = self._normalize_confidence(getattr(reflection, "confidence", 0.0))
        if not has_conflict or not action:
            return MemoryDisputeDecision(level="ignore", confidence=confidence)
        if confidence >= float(self.config.high_confidence_threshold or 0.75):
            level = "high_confidence"
        elif confidence >= float(self.config.normal_confidence_threshold or 0.45):
            level = "normal"
        else:
            level = "ignore"
        return MemoryDisputeDecision(
            level=level,
            confidence=confidence,
            action=action,
            conflict_type=str(getattr(reflection, "conflict_type", "none") or "none").strip() or "none",
            summary=str(getattr(reflection, "summary", "") or "").strip(),
            reason=str(getattr(reflection, "reason", "") or "").strip(),
            targets=list(getattr(reflection, "targets", []) or []),
            evidence=list(getattr(reflection, "evidence", []) or []),
        )

    def resolve_from_memory_metadata(self, metadata: dict | None) -> MemoryDisputeDecision:
        payload = dict(metadata or {}).get("reflection") or {}
        if not isinstance(payload, dict):
            return MemoryDisputeDecision(level="ignore")
        reflection = type("ReflectionPayload", (), payload)()
        setattr(reflection, "evidence", payload.get("evidence", []))
        setattr(reflection, "targets", payload.get("targets", []))
        return self.resolve(reflection)

    def _normalize_confidence(self, value: Any) -> float:
        try:
            return max(0.0, min(float(value), 1.0))
        except (TypeError, ValueError):
            return 0.0
