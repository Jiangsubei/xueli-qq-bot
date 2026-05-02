from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.core.models import MoodState

logger = logging.getLogger(__name__)


@dataclass
class MoodTickCauses:
    """Causal breakdown of a mood tick — supports 因果是可追溯的 principle."""

    user_emotion_valence: float = 0.0
    recent_negative_density: float = 0.0
    retrieval_failure_rate: float = 0.0
    conversation_gap_hours: float = 0.0
    energy_drain: float = 0.0

    def summary(self) -> str:
        parts: List[str] = []
        if self.recent_negative_density > 0.7:
            parts.append(f"负面对话密度={self.recent_negative_density:.1%}")
        if self.retrieval_failure_rate > 0.3:
            parts.append(f"检索失败率={self.retrieval_failure_rate:.1%}")
        if self.conversation_gap_hours > 48:
            parts.append(f"长间隔={self.conversation_gap_hours:.0f}h")
        if self.user_emotion_valence < -0.3:
            parts.append(f"用户情绪偏负面")
        elif self.user_emotion_valence > 0.3:
            parts.append(f"用户情绪偏正面")
        return "; ".join(parts) if parts else "无明显压力源"


@dataclass
class MoodTickResult:
    """Tick result with full causal trace."""

    state: MoodState = field(default_factory=MoodState)
    causes: MoodTickCauses = field(default_factory=MoodTickCauses)
    warmth_modifier: float = 0.0
    verbosity_modifier: float = 0.0
    initiative_modifier: float = 0.0
    visible_hint: str = ""


@dataclass
class MoodEngine:
    """Multi-dimensional mood engine driven by internal state variables.

    No sine waves — emotional state emerges from real metrics:
    - user_emotion_valence: 用户当前情感倾向
    - recent_negative_density: 近期对话负面情感密度
    - retrieval_failure_rate: 记忆调取失败率
    - conversation_gap_hours: 对话间隔时长

    When `enabled=False`, the engine is bypassed entirely and the bot's
    existing mirror-emotion behavior is preserved.
    """

    enabled: bool = False
    volatility: float = 0.3
    independence_ratio: float = 0.7
    energy_decay_per_turn: float = 0.05
    energy_recovery_night: float = 0.2
    cycle_length_days: int = 7  # deprecated — kept for backward compat, no longer drives sin()
    show_in_reply: bool = False
    _state: Optional[MoodState] = None

    def load(self, state: Optional[MoodState]) -> None:
        self._state = state or MoodState()

    def dump(self) -> MoodState:
        return self._state or MoodState()

    def tick(
        self,
        *,
        user_emotion_valence: float = 0.0,
        recent_negative_density: float = 0.0,
        retrieval_failure_rate: float = 0.0,
        conversation_gap_hours: float = 0.0,
    ) -> MoodTickResult:
        """Advance mood state by one conversation turn, driven by real metrics."""
        state = self._state or MoodState()
        causes = MoodTickCauses(
            user_emotion_valence=user_emotion_valence,
            recent_negative_density=recent_negative_density,
            retrieval_failure_rate=retrieval_failure_rate,
            conversation_gap_hours=conversation_gap_hours,
            energy_drain=self.energy_decay_per_turn,
        )

        if not self.enabled:
            return MoodTickResult(state=state, causes=causes)

        # ---- Valence: emerges from user emotion + internal stressors ----
        # user_emotion contributes via independence_ratio (e.g. 0.7 → 70% mirror)
        user_component = user_emotion_valence * (1.0 - self.independence_ratio)

        # negative density drags valence negative
        stress_component = -max(0.0, recent_negative_density - 0.3) * 0.5

        # retrieval failures cause frustration
        retrieval_component = -retrieval_failure_rate * 0.3

        # long gaps cause slight disconnection
        gap_component = -min(max(0.0, conversation_gap_hours - 24) / 168.0, 0.3)

        # random noise for natural variability
        noise = random.uniform(-0.05, 0.05) * self.volatility

        target_valence = user_component + stress_component + retrieval_component + gap_component + noise
        # smooth toward target (avoid sudden jumps)
        state.valence = state.valence * 0.7 + target_valence * 0.3
        state.valence = max(-1.0, min(1.0, state.valence))

        # ---- Arousal: driven by intensity, decays with long gaps ----
        base_arousal = 0.5
        if conversation_gap_hours > 24:
            arousal_suppression = min(conversation_gap_hours / 168.0, 0.4)
            base_arousal -= arousal_suppression
        arousal_target = base_arousal + abs(user_emotion_valence) * 0.2 + random.uniform(-0.03, 0.03)
        state.arousal = state.arousal * 0.6 + arousal_target * 0.4
        state.arousal = max(0.0, min(1.0, state.arousal))

        # ---- Energy: decays per turn, drains faster under stress ----
        stress_multiplier = 1.0 + max(0.0, recent_negative_density - 0.4) * 0.5
        energy_drain = self.energy_decay_per_turn * stress_multiplier
        state.energy = min(1.0, max(0.0, state.energy - energy_drain))
        causes.energy_drain = energy_drain

        state.updated_at = datetime.now().isoformat()
        self._state = state

        warmth_mod, verb_mod, init_mod = self.mood_modifier()
        visible_hint = self.mood_visible_hint()

        cause_text = causes.summary()
        if cause_text:
            logger.info("[情绪] valence=%.2f energy=%.2f 原因: %s", state.valence, state.energy, cause_text)

        return MoodTickResult(
            state=state,
            causes=causes,
            warmth_modifier=warmth_mod,
            verbosity_modifier=verb_mod,
            initiative_modifier=init_mod,
            visible_hint=visible_hint,
        )

    def night_recovery(self) -> None:
        if not self.enabled or self._state is None:
            return
        self._state.energy = min(1.0, self._state.energy + self.energy_recovery_night)

    def mood_modifier(self) -> Tuple[float, float, float]:
        """Returns (warmth_modifier, verbosity_modifier, initiative_modifier)."""
        if not self.enabled or self._state is None or self._state.valence is None:
            return (0.0, 0.0, 0.0)
        state = self._state
        warmth = state.valence * 0.3
        verbosity = (state.arousal - 0.5) * 0.4
        initiative = (state.energy - 0.5) * 0.4
        return (warmth, verbosity, initiative)

    def mood_visible_hint(self) -> str:
        if not self.show_in_reply or not self.enabled or self._state is None:
            return ""
        state = self._state
        if state.energy < 0.3 and state.valence < -0.2:
            return "今天有点累，但我还是想跟你聊聊"
        if state.valence > 0.5 and state.arousal > 0.6:
            return "今天心情不错"
        return ""
