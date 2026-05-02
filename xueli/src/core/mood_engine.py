from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from src.core.models import MoodState


@dataclass
class MoodEngine:
    """Autonomous mood fluctuation engine - pure math, no LLM calls.

    When `enabled=False`, the engine is bypassed entirely and the bot's
    existing mirror-emotion behavior is preserved.
    """

    enabled: bool = False
    volatility: float = 0.3
    independence_ratio: float = 0.7
    energy_decay_per_turn: float = 0.05
    energy_recovery_night: float = 0.2
    cycle_length_days: int = 7
    show_in_reply: bool = False
    _state: Optional[MoodState] = None

    def load(self, state: Optional[MoodState]) -> None:
        self._state = state or MoodState()
        self._sync_cycle_day()

    def dump(self) -> MoodState:
        return self._state or MoodState()

    def tick(self, *, user_emotion_valence: float = 0.0) -> MoodState:
        """Advance mood state by one conversation turn."""
        state = self._state or MoodState()
        if not self.enabled:
            return state

        cycle_phase = (2 * math.pi * state.mood_cycle_day) / max(1, self.cycle_length_days)
        autonomous_valence = math.sin(cycle_phase) * self.volatility
        autonomous_valence += random.uniform(-0.1, 0.1) * self.volatility

        effective_valence = autonomous_valence * self.independence_ratio + user_emotion_valence * (
            1.0 - self.independence_ratio
        )

        energy = max(0.0, state.energy - self.energy_decay_per_turn)
        arousal = max(0.0, min(1.0, (state.arousal + random.uniform(-0.05, 0.05))))

        state.valence = max(-1.0, min(1.0, effective_valence))
        state.arousal = arousal
        state.energy = energy
        state.mood_cycle_day = (state.mood_cycle_day + 1) % max(1, self.cycle_length_days)
        state.updated_at = datetime.now().isoformat()

        self._state = state
        return state

    def night_recovery(self) -> None:
        if not self.enabled or self._state is None:
            return
        self._state.energy = min(1.0, self._state.energy + self.energy_recovery_night)
        self._sync_cycle_day()

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

    def _sync_cycle_day(self) -> None:
        if self._state is None:
            return
        now = datetime.now()
        self._state.mood_cycle_day = now.day % max(1, self.cycle_length_days)
