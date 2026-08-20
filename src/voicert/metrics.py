"""Latency metrics — TTFB per pipeline stage, Rapida-style structured logs.

Stages tracked per turn (all relative to the user's final utterance):
  * ``stt_final``       — STT emitted the final transcript
  * ``llm_first_token`` — first LLM token (true conversational TTFB)
  * ``tts_first_audio`` — first synthesized audio chunk (what the user hears)

Budgets are per-profile (NPC has the tightest). ``over_budget`` in the
report makes regressions greppable in production logs.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger("voicert.metrics")

STAGES = ("stt_final", "llm_first_token", "tts_first_audio")


@dataclass(frozen=True, slots=True)
class LatencyBudget:
    """Per-profile targets, milliseconds from end of user speech."""

    total_ms: int
    llm_first_token_ms: int
    tts_first_audio_ms: int


@dataclass
class TurnMetrics:
    turn_id: int
    started: float
    marks: dict[str, float] = field(default_factory=dict)
    interrupted: bool = False

    def elapsed_ms(self, stage: str) -> float | None:
        ts = self.marks.get(stage)
        return None if ts is None else (ts - self.started) * 1000.0


class TTFBTracker:
    def __init__(self, profile: str, budget: LatencyBudget) -> None:
        self.profile = profile
        self.budget = budget
        self._turns: dict[int, TurnMetrics] = {}
        self.interruptions = 0

    def turn_started(self, turn_id: int) -> None:
        self._turns[turn_id] = TurnMetrics(turn_id=turn_id, started=time.monotonic())

    def mark(self, turn_id: int, stage: str) -> None:
        tm = self._turns.get(turn_id)
        if tm is None or stage in tm.marks:
            return  # first occurrence only — TTFB is about the *first* byte
        tm.marks[stage] = time.monotonic()

    def turn_interrupted(self, turn_id: int) -> None:
        self.interruptions += 1
        tm = self._turns.get(turn_id)
        if tm is not None:
            tm.interrupted = True

    def report(self, turn_id: int) -> dict[str, object]:
        tm = self._turns.get(turn_id)
        if tm is None:
            return {}
        ttfa = tm.elapsed_ms("tts_first_audio")
        payload: dict[str, object] = {
            "event": "turn_latency",
            "profile": self.profile,
            "turn_id": turn_id,
            "interrupted": tm.interrupted,
            "budget_total_ms": self.budget.total_ms,
            "over_budget": ttfa is not None and ttfa > self.budget.total_ms,
            **{s: tm.elapsed_ms(s) for s in STAGES},
        }
        logger.info(json.dumps(payload, ensure_ascii=False))
        return payload
