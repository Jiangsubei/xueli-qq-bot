from __future__ import annotations

import asyncio
import json
import os
import re
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from src.core.config import CharacterGrowthConfig
from src.core.models import CharacterCardSnapshot


class CharacterCardService:
    """Maintain a lightweight layered character preference snapshot per user."""

    def __init__(self, base_path: str, config: CharacterGrowthConfig) -> None:
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        self.config = config

    def record_explicit_feedback(self, user_id: str, text: str) -> None:
        if not self.config.enabled:
            return
        normalized = str(text or "").strip()
        if not normalized:
            return
        category = self._classify_feedback(normalized)
        if not category:
            return
        payload = self._load_payload(user_id)
        payload["explicit_feedback"].append(
            {
                "text": normalized,
                "category": category,
                "created_at": datetime.now().isoformat(),
            }
        )
        self._save_payload(user_id, payload)

    def record_interaction_signal(self, user_id: str, signal: str, weight: int = 1) -> None:
        if not self.config.enabled:
            return
        normalized = str(signal or "").strip()
        if not normalized:
            return
        payload = self._load_payload(user_id)
        payload["stable_signals"].append(
            {
                "signal": normalized,
                "weight": max(1, int(weight or 1)),
                "created_at": datetime.now().isoformat(),
            }
        )
        self._save_payload(user_id, payload)

    def refresh_snapshot(self, user_id: str) -> CharacterCardSnapshot:
        payload = self._load_payload(user_id)
        explicit_counter = Counter(item.get("category") for item in payload.get("explicit_feedback", []))
        signal_counter = Counter()
        for item in payload.get("stable_signals", []):
            signal_counter[str(item.get("signal") or "")] += max(1, int(item.get("weight", 1) or 1))

        snapshot = CharacterCardSnapshot(
            user_id=str(user_id),
            core_traits=self._build_core_traits(explicit_counter),
            tone_preferences=self._build_tone_preferences(explicit_counter, signal_counter),
            behavior_habits=self._build_behavior_habits(explicit_counter, signal_counter),
            explicit_feedback_count=sum(explicit_counter.values()),
            stable_signal_count=sum(signal_counter.values()),
            updated_at=datetime.now().isoformat(),
            metadata={
                "explicit_feedback": dict(explicit_counter),
                "stable_signals": dict(signal_counter),
            },
        )
        payload["snapshot"] = asdict(snapshot)
        self._save_payload(user_id, payload)
        return snapshot

    def get_snapshot(self, user_id: str) -> CharacterCardSnapshot:
        payload = self._load_payload(user_id)
        snapshot_data = payload.get("snapshot") or {}
        if snapshot_data:
            return CharacterCardSnapshot(**snapshot_data)
        return self.refresh_snapshot(user_id)

    def _build_core_traits(self, explicit_counter: Counter) -> List[str]:
        traits: List[str] = []
        if explicit_counter.get("more_gentle", 0) >= self.config.core_trait_threshold:
            traits.append("更注重温和承接")
        if explicit_counter.get("more_direct", 0) >= self.config.core_trait_threshold:
            traits.append("更偏向直接清楚")
        return traits

    def _build_tone_preferences(self, explicit_counter: Counter, signal_counter: Counter) -> List[str]:
        hints: List[str] = []
        if explicit_counter.get("more_brief", 0) >= self.config.tone_preference_threshold:
            hints.append("偏好更短一点")
        if explicit_counter.get("more_warm", 0) >= self.config.tone_preference_threshold:
            hints.append("偏好更柔和一点")
        if signal_counter.get("private_continue", 0) >= self.config.stable_signal_threshold:
            hints.append("私聊里可以更自然续接")
        return hints

    def _build_behavior_habits(self, explicit_counter: Counter, signal_counter: Counter) -> List[str]:
        hints: List[str] = []
        if explicit_counter.get("less_followup", 0) >= self.config.behavior_habit_threshold:
            hints.append("少一点主动追问")
        if signal_counter.get("group_light_presence", 0) >= self.config.stable_signal_threshold:
            hints.append("群聊里保持轻接话")
        if signal_counter.get("comfort_acceptance", 0) >= self.config.stable_signal_threshold:
            hints.append("情绪承接可以更明显")
        return hints

    def _classify_feedback(self, text: str) -> str:
        normalized = re.sub(r"\s+", "", text)
        if any(token in normalized for token in ("简短一点", "短一点", "别太长", "精简")):
            return "more_brief"
        if any(token in normalized for token in ("接住我", "先安慰", "温柔一些", "温柔一点")):
            return "more_gentle"
        if any(token in normalized for token in ("温柔一点", "柔和一点", "别那么冲", "语气好一点")):
            return "more_warm"
        if any(token in normalized for token in ("直接一点", "说清楚点", "别绕")):
            return "more_direct"
        if any(token in normalized for token in ("别追问", "少问点", "别一直问")):
            return "less_followup"
        return ""

    def _file_path(self, user_id: str) -> Path:
        return self.base_path / f"{str(user_id or 'unknown').strip() or 'unknown'}.json"

    def _load_payload(self, user_id: str) -> Dict[str, object]:
        file_path = self._file_path(user_id)
        try:
            return json.loads(file_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"explicit_feedback": [], "stable_signals": [], "snapshot": {}}

    def _save_payload(self, user_id: str, payload: Dict[str, object]) -> None:
        file_path = self._file_path(user_id)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, file_path)
