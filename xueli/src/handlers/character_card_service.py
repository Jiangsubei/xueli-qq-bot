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
from src.core.models import CharacterCardSnapshot, RelationshipProfile


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

        bot_hints = self._build_bot_persona_hints(explicit_counter, signal_counter)
        snapshot = CharacterCardSnapshot(
            user_id=str(user_id),
            core_traits=self._build_core_traits(explicit_counter),
            tone_preferences=self._build_tone_preferences(explicit_counter, signal_counter),
            behavior_habits=self._build_behavior_habits(explicit_counter, signal_counter),
            bot_persona_hints=bot_hints,
            explicit_feedback_count=sum(explicit_counter.values()),
            stable_signal_count=sum(signal_counter.values()),
            updated_at=datetime.now().isoformat(),
            metadata={
                "explicit_feedback": dict(explicit_counter),
                "stable_signals": dict(signal_counter),
            },
            relationship_tone_hint=self.get_relationship_tone_hint(str(user_id)),
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

    def _build_bot_persona_hints(self, explicit_counter: Counter, signal_counter: Counter) -> List[str]:
        """Build hints about how the bot should respond to this specific user."""
        hints: List[str] = []
        if explicit_counter.get("more_brief", 0) >= self.config.tone_preference_threshold:
            hints.append("这个用户喜欢简短精炼的回复")
        if explicit_counter.get("more_warm", 0) >= self.config.tone_preference_threshold:
            hints.append("这个用户偏好更柔和温暖的语气")
        if explicit_counter.get("more_direct", 0) >= self.config.core_trait_threshold:
            hints.append("这个用户喜欢直接清楚的表达")
        if explicit_counter.get("more_gentle", 0) >= self.config.core_trait_threshold:
            hints.append("这个用户需要更温和的承接方式")
        if explicit_counter.get("less_followup", 0) >= self.config.behavior_habit_threshold:
            hints.append("这个用户不太喜欢被追问")
        if signal_counter.get("private_continue", 0) >= self.config.stable_signal_threshold:
            hints.append("私聊里可以自然续接，不用刻意开启新话题")
        if signal_counter.get("group_light_presence", 0) >= self.config.stable_signal_threshold:
            hints.append("群聊里保持轻量参与，不过度回复")
        if signal_counter.get("comfort_acceptance", 0) >= self.config.stable_signal_threshold:
            hints.append("用户接受了情绪承接，可以更自然地表达关心")
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

    def record_emotion(self, user_id: str, tone: str) -> None:
        """Record an emotional tone for this user (sliding window of recent emotions)."""
        if not self.config.enabled or not str(tone or "").strip():
            return
        payload = self._load_payload(user_id)
        history = list(payload.get("emotional_history") or [])
        history.append({"tone": tone, "created_at": datetime.now().isoformat()})
        max_len = 10
        if len(history) > max_len:
            history = history[-max_len:]
        payload["emotional_history"] = history
        self._save_payload(user_id, payload)

    def get_emotional_trend(self, user_id: str) -> str:
        """Return emotional trend description for prompt injection."""
        payload = self._load_payload(user_id)
        history = list(payload.get("emotional_history") or [])
        if not history:
            return ""
        recent = list(item.get("tone", "") for item in history[-5:])
        if not recent:
            return ""
        dominant = max(set(recent), key=recent.count)
        negative_tones = {"伤心", "生气", "无语", "委屈", "害怕", "困惑"}
        positive_tones = {"开心", "喜欢", "惊讶"}
        neg_count = sum(1 for t in recent[-3:] if t in negative_tones)
        pos_count = sum(1 for t in recent[-3:] if t in positive_tones)
        if neg_count > pos_count and len(recent) >= 3:
            trend = "worsening"
        elif pos_count > neg_count and len(recent) >= 3:
            trend = "improving"
        else:
            trend = "stable"
        trend_cn = {"worsening": "情绪逐渐变差", "improving": "情绪逐渐变好", "stable": "情绪平稳"}
        dominant_cn = f"以{dominant}为主" if dominant else ""
        return f"用户情绪趋势：最近几轮对话中{trend_cn.get(trend, '情绪波动')}，{dominant_cn}"

    def get_relationship_profile(self, user_id: str) -> RelationshipProfile:
        payload = self._load_payload(user_id)
        data = payload.get("relationship_profile", {})
        if isinstance(data, dict):
            return RelationshipProfile.from_dict(data)
        return RelationshipProfile(user_id=user_id)

    def save_relationship_profile(self, user_id: str, profile: RelationshipProfile) -> None:
        if not self.config.relationship_tracking_enabled:
            return
        payload = self._load_payload(user_id)
        profile.relationship_stage = profile.resolve_stage(
            acquaintance_threshold=self.config.intimacy_acquaintance_threshold,
            friend_threshold=self.config.intimacy_friend_threshold,
            close_friend_threshold=self.config.intimacy_close_friend_threshold,
        )
        profile.last_intimacy_change = datetime.now().isoformat()
        payload["relationship_profile"] = profile.to_dict()
        self._save_payload(user_id, payload)

    def update_intimacy(self, user_id: str, delta: float, *, is_friction: bool = False) -> RelationshipProfile:
        if not self.config.relationship_tracking_enabled:
            return RelationshipProfile(user_id=user_id)
        profile = self.get_relationship_profile(user_id)
        profile.user_id = user_id
        profile.intimacy_level = max(0.0, min(1.0, profile.intimacy_level + delta))
        profile.total_interactions += 1
        if is_friction:
            profile.friction_signals += 1
        else:
            profile.friction_signals = max(0, profile.friction_signals - 1)
        self.save_relationship_profile(user_id, profile)
        return profile

    def get_relationship_tone_hint(self, user_id: str) -> str:
        if not self.config.relationship_tracking_enabled:
            return ""
        profile = self.get_relationship_profile(user_id)
        if profile.friction_signals >= self.config.friction_signals_caution_threshold:
            return "注意到你们近期有些摩擦，语气要柔和，别让事情变得更僵"
        return profile.tone_hint()

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
