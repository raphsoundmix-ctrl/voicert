"""What a turn consumed, and what that costs.

This module is pure: no clock, no lock, no state, no I/O. Given the same
usage and the same rate cards it returns the same integers, every time, which
is what makes its output citable in an argument rather than merely indicative.
Everything else in :mod:`voicert.economics` is bookkeeping around these
functions.

The one contract worth reading twice is on :class:`TurnUsage`:
``input_tokens`` and ``cached_input_tokens`` are **disjoint**. Vendors
disagree about this — OpenAI reports ``prompt_tokens`` *inclusive* of cached
tokens, Anthropic reports ``input_tokens`` *exclusive* of them. On the
measured NPC profile the cached prefix is ~700 of ~980 tokens, so getting the
convention backwards misprices the turn by about 70%. Rather than guess, the
fields are defined as disjoint and each vendor gets a named constructor that
does the subtraction (or does not) explicitly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Literal

from voicert.economics.money import (
    PER_MILLION,
    BudgetMisuseError,
    NanoUSD,
    ceil_div,
    usd,
)
from voicert.economics.prices import CostTier, PriceBook

SECONDS_PER_HOUR = 3600
MS_PER_HOUR = SECONDS_PER_HOUR * 1000


@dataclass(frozen=True, slots=True)
class TierPlan:
    """Per-modality tier assignment for one turn.

    A scalar tier cannot express the configuration that actually matters —
    cloud LLM with on-device TTS — and that mix is where most of the money is
    saved. So the plan is per modality.
    """

    llm: CostTier
    stt: CostTier
    tts: CostTier

    @property
    def headline(self) -> CostTier:
        """The most expensive modality. What the game reasons about."""
        return max(self.llm, self.stt, self.tts)

    @property
    def is_free(self) -> bool:
        return self.headline <= CostTier.LOCAL

    @classmethod
    def uniform(cls, tier: CostTier) -> "TierPlan":
        return cls(llm=tier, stt=tier, tts=tier)

    @classmethod
    def local_tts(cls, cloud: CostTier = CostTier.CLOUD_CHEAP) -> "TierPlan":
        """Cloud brain, on-device voice. The default worth reaching for."""
        return cls(llm=cloud, stt=cloud, tts=CostTier.LOCAL)

    def downgraded_to(self, ceiling: CostTier) -> "TierPlan":
        """Clamp every modality at ``ceiling``.

        Uses ``min`` per field so a downgrade never accidentally *upgrades* a
        modality that was already cheaper — the classic bug where a global
        "fall back to CLOUD_CHEAP" promotes an on-device TTS into a paid one.
        """
        return TierPlan(
            llm=min(self.llm, ceiling),
            stt=min(self.stt, ceiling),
            tts=min(self.tts, ceiling),
        )


@dataclass(frozen=True, slots=True)
class TurnUsage:
    """Measured consumption of one turn. A dumb value with no prices attached.

    Deliberately price-free so the same usage can be costed against several
    tiers — which is what :func:`shadow_cost` needs in order to answer "what
    would this local turn have cost in the cloud?".
    """

    #: Prompt tokens billed at the full input rate. DISJOINT from cached.
    input_tokens: int = 0
    #: Prompt tokens served from a cache. DISJOINT from ``input_tokens``.
    cached_input_tokens: int = 0
    #: Tokens written into a cache. Anthropic bills these at a premium.
    cache_write_tokens: int = 0
    output_tokens: int = 0
    #: Duration of the user's utterance sent to STT.
    audio_ms_in: int = 0
    #: Characters handed to TTS. The dominant term on a cloud voice turn.
    characters_out: int = 0

    def __post_init__(self) -> None:
        for name in (
            "input_tokens",
            "cached_input_tokens",
            "cache_write_tokens",
            "output_tokens",
            "audio_ms_in",
            "characters_out",
        ):
            if getattr(self, name) < 0:
                raise BudgetMisuseError(f"TurnUsage.{name} cannot be negative")

    @property
    def total_prompt_tokens(self) -> int:
        """Every prompt token, cached or not. For reporting, not for pricing."""
        return self.input_tokens + self.cached_input_tokens

    def __add__(self, other: "TurnUsage") -> "TurnUsage":
        return TurnUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            audio_ms_in=self.audio_ms_in + other.audio_ms_in,
            characters_out=self.characters_out + other.characters_out,
        )

    def with_output(self, *, output_tokens: int, characters_out: int) -> "TurnUsage":
        """Same input side, different output side. Used when a barge-in cuts a
        turn short: the prompt was already sent and is already owed."""
        return replace(
            self, output_tokens=output_tokens, characters_out=characters_out
        )

    @classmethod
    def from_openai_usage(
        cls,
        payload: Mapping[str, Any],
        *,
        audio_ms_in: int = 0,
        characters_out: int = 0,
    ) -> "TurnUsage":
        """Read an OpenAI-shaped ``usage`` block.

        OpenAI's ``prompt_tokens`` **includes** ``prompt_tokens_details.
        cached_tokens``, so the cached count is subtracted out here to satisfy
        this module's disjointness contract.
        """
        prompt = int(payload.get("prompt_tokens", 0))
        details = payload.get("prompt_tokens_details") or {}
        cached = int(details.get("cached_tokens", 0))
        if cached > prompt:
            raise BudgetMisuseError(
                f"cached_tokens ({cached}) exceeds prompt_tokens ({prompt}); "
                "this payload is not OpenAI-shaped"
            )
        return cls(
            input_tokens=prompt - cached,
            cached_input_tokens=cached,
            output_tokens=int(payload.get("completion_tokens", 0)),
            audio_ms_in=audio_ms_in,
            characters_out=characters_out,
        )

    @classmethod
    def from_anthropic_usage(
        cls,
        payload: Mapping[str, Any],
        *,
        audio_ms_in: int = 0,
        characters_out: int = 0,
    ) -> "TurnUsage":
        """Read an Anthropic-shaped ``usage`` block.

        Anthropic's ``input_tokens`` **excludes** ``cache_read_input_tokens``,
        so no subtraction is needed — which is precisely why this cannot share
        a code path with the OpenAI reader.
        """
        return cls(
            input_tokens=int(payload.get("input_tokens", 0)),
            cached_input_tokens=int(payload.get("cache_read_input_tokens", 0)),
            cache_write_tokens=int(payload.get("cache_creation_input_tokens", 0)),
            output_tokens=int(payload.get("output_tokens", 0)),
            audio_ms_in=audio_ms_in,
            characters_out=characters_out,
        )

    @classmethod
    def from_ollama_usage(
        cls,
        payload: Mapping[str, Any],
        *,
        audio_ms_in: int = 0,
        characters_out: int = 0,
    ) -> "TurnUsage":
        """Read Ollama's final streamed object.

        Ollama reports ``prompt_eval_count`` for the *whole* prompt even when
        most of it was served from a reused KV prefix, and gives no way to
        separate the two. Everything is therefore counted as uncached input,
        which makes any cloud shadow price computed from it an **upper
        bound** — the honest direction for a cost argument to err in.
        """
        return cls(
            input_tokens=int(payload.get("prompt_eval_count", 0)),
            output_tokens=int(payload.get("eval_count", 0)),
            audio_ms_in=audio_ms_in,
            characters_out=characters_out,
        )


@dataclass(frozen=True, slots=True)
class TurnCost:
    """A priced turn, split by modality.

    The split is not decoration. On the bundled premium table the TTS
    component of the measured NPC turn is roughly two orders of magnitude
    larger than the LLM component, and a single scalar hides that entirely.
    """

    llm_nusd: NanoUSD
    stt_nusd: NanoUSD
    tts_nusd: NanoUSD
    plan: TierPlan
    usage: TurnUsage

    def __post_init__(self) -> None:
        for name in ("llm_nusd", "stt_nusd", "tts_nusd"):
            if getattr(self, name) < 0:
                raise BudgetMisuseError(f"TurnCost.{name} cannot be negative")

    @property
    def total_nusd(self) -> NanoUSD:
        return self.llm_nusd + self.stt_nusd + self.tts_nusd

    @property
    def is_free(self) -> bool:
        return self.total_nusd == 0

    @property
    def dominant_component(self) -> Literal["llm", "stt", "tts", "none"]:
        """Which modality to attack first. Usually not the one people expect."""
        if self.total_nusd == 0:
            return "none"
        parts: list[tuple[NanoUSD, Literal["llm", "stt", "tts"]]] = [
            (self.llm_nusd, "llm"),
            (self.stt_nusd, "stt"),
            (self.tts_nusd, "tts"),
        ]
        return max(parts)[1]

    def share(self, component: Literal["llm", "stt", "tts"]) -> float:
        """Fraction of this turn's cost in one modality, 0.0 when free."""
        if self.total_nusd == 0:
            return 0.0
        value = {"llm": self.llm_nusd, "stt": self.stt_nusd, "tts": self.tts_nusd}[
            component
        ]
        return value / self.total_nusd

    def as_usd(self) -> Decimal:
        """Exact dollars, for display. Never fed back into arithmetic."""
        return usd(self.total_nusd)

    def as_log_record(self) -> dict[str, object]:
        return {
            "event": "turn_cost",
            "total_nusd": self.total_nusd,
            "llm_nusd": self.llm_nusd,
            "stt_nusd": self.stt_nusd,
            "tts_nusd": self.tts_nusd,
            "dominant": self.dominant_component,
            "tier": self.plan.headline.name,
            "output_tokens": self.usage.output_tokens,
            "characters_out": self.usage.characters_out,
        }


def price_turn(usage: TurnUsage, book: PriceBook, plan: TierPlan) -> TurnCost:
    """Cost one turn. Pure, total, and the root of everything else here.

    Each modality is priced from **its own** tier's table, because the
    dominant real-world mix is a cloud brain with an on-device voice and a
    scalar tier cannot express that.

    Rounding is ceiling *per component*, then summed. Rounding once at the end
    would let three sub-nano components each collapse to zero and price a real
    turn at nothing.
    """
    llm_price = book[plan.llm].llm
    stt_price = book[plan.stt].stt
    tts_price = book[plan.tts].tts

    llm_nusd = (
        ceil_div(usage.input_tokens * llm_price.input_nusd_per_mtok, PER_MILLION)
        + ceil_div(
            usage.cached_input_tokens * llm_price.cached_input_nusd_per_mtok,
            PER_MILLION,
        )
        + ceil_div(
            usage.cache_write_tokens * llm_price.cache_write_nusd_per_mtok, PER_MILLION
        )
        + ceil_div(usage.output_tokens * llm_price.output_nusd_per_mtok, PER_MILLION)
    )

    billed_ms = max(usage.audio_ms_in, stt_price.minimum_billed_ms)
    stt_nusd = ceil_div(billed_ms * stt_price.nusd_per_audio_hour, MS_PER_HOUR)

    billed_chars = max(usage.characters_out, tts_price.minimum_billed_chars)
    tts_nusd = ceil_div(billed_chars * tts_price.nusd_per_million_chars, PER_MILLION)

    return TurnCost(
        llm_nusd=llm_nusd,
        stt_nusd=stt_nusd,
        tts_nusd=tts_nusd,
        plan=plan,
        usage=usage,
    )


def shadow_cost(
    usage: TurnUsage,
    book: PriceBook,
    *,
    tier: CostTier = CostTier.CLOUD_CHEAP,
) -> TurnCost:
    """What this turn *would* have cost in the cloud.

    Free to compute and it is the number that turns "local inference is
    cheaper" from a claim into a per-session figure a studio can read off its
    own telemetry. Reserves nothing, denies nothing, and never touches a
    ledger.
    """
    return price_turn(usage, book, TierPlan.uniform(tier))
