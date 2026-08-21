"""Budget maths — the question every game dev asks first.

Two budgets decide whether AI dialogue ships or gets cut:

* **The compute budget.** A game has 16.6 ms per frame at 60 fps, and voice
  synthesis may take exactly none of it. Everything here runs off the render
  thread, so the real constraint is *how much of one CPU core* the pool costs.
  That is governed by the real-time factor (RTF) of the local TTS: RTF 0.1
  means one second of speech costs 0.1 s of CPU, so one continuously-speaking
  NPC occupies 10% of a core.

* **The money budget.** Recording dialogue with actors is a fixed cost per
  line that recurs on every rewrite. Baked synthesis is a one-time cost per
  line with no re-record fee. On-device synthesis has a **marginal cost of
  zero** — the player's silicon does the work — which is what makes an
  open world with thousands of spoken lines viable for a two-person studio.

Both calculators take their numbers as parameters. Measure your own models and
your own quotes; the defaults are conservative placeholders, not promises.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ComputeBudget:
    """Derives a safe voice-pool size from a CPU allowance.

    ``rtf`` — real-time factor of the local TTS on the *target minimum spec*,
    not on the developer's workstation. Measure it, do not assume it.

    ``cpu_share`` — fraction of ONE core you are willing to spend on speech,
    expressed 0..1. Games are usually GPU-bound with spare CPU threads, but
    the minimum spec is what matters.

    ``speaking_duty`` — the fraction of time a live NPC is actually mid-
    utterance. Conversation is turn-taking, so an NPC is rarely synthesizing
    continuously; 0.5 is a reasonable starting assumption for dialogue and
    lower for ambient chatter.
    """

    rtf: float = 0.10
    cpu_share: float = 0.35
    speaking_duty: float = 0.5

    def __post_init__(self) -> None:
        if self.rtf <= 0:
            raise ValueError("rtf must be positive")
        if not 0 < self.cpu_share <= 1:
            raise ValueError("cpu_share must be within (0, 1]")
        if not 0 < self.speaking_duty <= 1:
            raise ValueError("speaking_duty must be within (0, 1]")

    @property
    def cost_per_live_npc(self) -> float:
        """Core-fraction consumed by one live NPC, averaged over time."""
        return self.rtf * self.speaking_duty

    def max_pool_size(self) -> int:
        """How many live agents fit in the allowance. Never returns 0 — if the
        budget cannot afford even one voice, that is a configuration error the
        caller should see rather than a silently muted game."""
        # Plain floor division loses a whole voice to float representation
        # (0.35 // 0.05 == 6.0, because the quotient is 6.999...). The epsilon
        # snaps quotients that are within a rounding error of an integer.
        ratio = self.cpu_share / self.cost_per_live_npc
        fits = int(math.floor(ratio + 1e-9))
        if fits < 1:
            raise ValueError(
                f"cpu_share={self.cpu_share} cannot afford a single voice at "
                f"rtf={self.rtf}, duty={self.speaking_duty} "
                f"(needs {self.cost_per_live_npc:.3f} of a core). "
                "Use a faster/smaller TTS model or raise the allowance."
            )
        return fits

    def report(self) -> dict[str, float | int]:
        return {
            "rtf": self.rtf,
            "cpu_share_of_one_core": self.cpu_share,
            "speaking_duty": self.speaking_duty,
            "cost_per_live_npc": round(self.cost_per_live_npc, 4),
            "max_pool_size": self.max_pool_size(),
        }


@dataclass(frozen=True, slots=True)
class VoiceCostModel:
    """Compares the three ways to voice a script.

    All money figures are *per line* and supplied by the caller — quotes vary
    by region, union status, and vendor tier, and any hardcoded number here
    would be stale within a quarter.

    ``rewrites`` is the multiplier that decides most projects: recorded
    dialogue is re-quoted every time the script changes, synthesized dialogue
    is regenerated for free.
    """

    line_count: int
    #: Studio VO: talent + direction + engineering + editing, amortized per line.
    vo_cost_per_line: float = 12.0
    #: Cloud TTS at build time, per line, one-off.
    baked_tts_cost_per_line: float = 0.02
    #: How many times the script gets revised after first recording.
    rewrites: int = 2
    #: Share of lines that must be dynamic (cannot be pre-baked).
    dynamic_share: float = 0.15

    def __post_init__(self) -> None:
        if self.line_count < 0:
            raise ValueError("line_count cannot be negative")
        if not 0 <= self.dynamic_share <= 1:
            raise ValueError("dynamic_share must be within [0, 1]")

    @property
    def _revisions(self) -> int:
        """First pass plus each rewrite."""
        return 1 + self.rewrites

    def traditional_vo(self) -> float:
        """Actors re-record the changed script every revision."""
        return self.line_count * self.vo_cost_per_line * self._revisions

    def baked_only(self) -> float:
        """Every line synthesized at build time. Regeneration is ~free, but
        cost still scales with revisions because you re-run the whole bank."""
        return self.line_count * self.baked_tts_cost_per_line * self._revisions

    def hybrid(self) -> float:
        """Static lines baked at build time; dynamic lines synthesized on the
        player's device at runtime, which costs the studio nothing per line.

        This is the model this framework is built for.
        """
        static_lines = self.line_count * (1 - self.dynamic_share)
        return static_lines * self.baked_tts_cost_per_line * self._revisions

    @classmethod
    def from_session_rate(
        cls,
        line_count: int,
        session_cost: float = 1_134.95,
        lines_per_session: int = 250,
        overhead_multiplier: float = 1.6,
        **kwargs: object,
    ) -> "VoiceCostModel":
        """Derive the per-line VO cost from a booked session rate.

        The default ``session_cost`` is the SAG-AFTRA Interactive Media
        Agreement off-camera Day Performer minimum for a 4-hour day covering
        up to three voices, contract year 2025-11-01 to 2026-10-31
        (a 6-hour day covering 6-10 voices is $2,270.78). It is a *minimum*:
        non-union work can be cheaper, name talent is far more expensive.

        ``lines_per_session`` is studio throughput and varies enormously with
        material — combat barks run fast, emotional narrative does not. Use
        your own casting director's number.

        ``overhead_multiplier`` covers what the talent fee excludes: studio
        time, direction, engineering, editing, retakes, and localization
        management.
        """
        if lines_per_session < 1:
            raise ValueError("lines_per_session must be at least 1")
        per_line = (session_cost / lines_per_session) * overhead_multiplier
        return cls(line_count=line_count, vo_cost_per_line=per_line, **kwargs)  # type: ignore[arg-type]

    def report(self) -> dict[str, float]:
        traditional = self.traditional_vo()
        hybrid = self.hybrid()
        return {
            "lines": float(self.line_count),
            "revisions": float(self._revisions),
            "traditional_vo": round(traditional, 2),
            "baked_only": round(self.baked_only(), 2),
            "hybrid_baked_plus_ondevice": round(hybrid, 2),
            "saved_vs_vo": round(traditional - hybrid, 2),
            "runtime_cost_per_player": 0.0,
        }
