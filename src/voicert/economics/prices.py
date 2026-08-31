"""Rate cards — the three modalities of a voice turn, priced separately.

Keeping LLM, STT and TTS as separate prices is not tidiness. On every rate
card surveyed for this module, **speech synthesis dominates a voice turn and
the language model is a rounding error**: for a short NPC reply on a cheap
cloud mix, TTS is ~88% of the bill and the LLM is ~4%. A design that collapses
these into one "AI cost" number optimizes the wrong term.

Every price carries ``source`` and ``valid_until`` as *fields*, not comments,
because two of the rates below are explicitly promotional and one vendor's
public pricing page was withdrawn during research. A stale rate card silently
understating cost is the failure mode this provenance exists to catch —
:meth:`PriceTable.is_stale` warns, but never alters a cost and never raises. A
shipped game whose promo expired must keep talking, not crash.

The bundled tables are **starting points with citations, not promises**.
Re-check them against the vendor before shipping.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import IntEnum
from types import MappingProxyType
from typing import ClassVar, Final

from voicert.economics.money import BudgetMisuseError, NanoUSD, parse_usd


class CostTier(IntEnum):
    """*Who* computes the turn, ordered by marginal cost to the studio.

    Deliberately orthogonal to ``game.lod.DialogueTier``, which describes what
    the player can *hear*. An NPC can be audibly LIVE while being computed at
    CANNED cost — that combination is the entire point of a fallback.

    ``CANNED`` is the floor: pre-authored lines, zero marginal cost, therefore
    *always* affordable. Because a zero-cost tier always exists, tier routing
    is a total function and never has to answer "nothing is affordable".
    """

    CANNED = 0
    LOCAL = 1
    CLOUD_CHEAP = 2
    CLOUD_PREMIUM = 3


@dataclass(frozen=True, slots=True)
class LLMPrice:
    """Text-generation rate. Input, output and cached input are separate.

    They must be, because the surveyed cached-input discounts range from 50%
    (``gpt-4o-mini``) to ~97% (DeepSeek off-peak). A single blended "input"
    number would misprice a persona-prefixed NPC turn — which is nearly all of
    them — by most of the bill.

    ``cache_write_nusd_per_mtok`` exists because Anthropic charges a *premium*
    to write the cache (1.25x base for a 5-minute TTL, 2x for an hour). For
    bursty NPC traffic with gaps longer than the TTL, caching can cost more
    than not caching. Modelling the write is what surfaces that.
    """

    input_nusd_per_mtok: NanoUSD
    output_nusd_per_mtok: NanoUSD
    cached_input_nusd_per_mtok: NanoUSD = 0
    cache_write_nusd_per_mtok: NanoUSD = 0

    def __post_init__(self) -> None:
        for name in (
            "input_nusd_per_mtok",
            "output_nusd_per_mtok",
            "cached_input_nusd_per_mtok",
            "cache_write_nusd_per_mtok",
        ):
            if getattr(self, name) < 0:
                raise BudgetMisuseError(f"LLMPrice.{name} cannot be negative")

    @classmethod
    def from_usd(
        cls,
        *,
        input_per_mtok: str | int | float | Decimal,
        output_per_mtok: str | int | float | Decimal,
        cached_input_per_mtok: str | int | float | Decimal = 0,
        cache_write_per_mtok: str | int | float | Decimal = 0,
    ) -> "LLMPrice":
        """Read a vendor rate card in its published units ($/million tokens)."""
        return cls(
            input_nusd_per_mtok=parse_usd(input_per_mtok),
            output_nusd_per_mtok=parse_usd(output_per_mtok),
            cached_input_nusd_per_mtok=parse_usd(cached_input_per_mtok),
            cache_write_nusd_per_mtok=parse_usd(cache_write_per_mtok),
        )

    FREE: ClassVar["LLMPrice"]


LLMPrice.FREE = LLMPrice(0, 0, 0, 0)


@dataclass(frozen=True, slots=True)
class STTPrice:
    """Speech-in rate, stored per audio hour.

    Per hour because that is how the vendors publish it. A per-second integer
    field would have to round AssemblyAI's $0.15/hr and drift over a session.
    """

    nusd_per_audio_hour: NanoUSD
    minimum_billed_ms: int = 0

    def __post_init__(self) -> None:
        if self.nusd_per_audio_hour < 0:
            raise BudgetMisuseError("STTPrice.nusd_per_audio_hour cannot be negative")
        if self.minimum_billed_ms < 0:
            raise BudgetMisuseError("STTPrice.minimum_billed_ms cannot be negative")

    @classmethod
    def from_usd_per_minute(
        cls,
        usd_per_minute: str | int | float | Decimal,
        *,
        minimum_billed_ms: int = 0,
    ) -> "STTPrice":
        return cls(
            nusd_per_audio_hour=parse_usd(usd_per_minute) * 60,
            minimum_billed_ms=minimum_billed_ms,
        )

    FREE: ClassVar["STTPrice"]


STTPrice.FREE = STTPrice(0)


@dataclass(frozen=True, slots=True)
class TTSPrice:
    """Speech-out rate, per character.

    Per character only. There is deliberately no ``from_usd_per_minute``
    constructor: ElevenLabs' own page implies ~1000 chars/minute and
    Cartesia's plan maths implies ~750, so a minutes-based conversion would
    bake a 33% error into the dominant cost term. If a vendor quotes minutes,
    convert it yourself and own the assumption.
    """

    nusd_per_million_chars: NanoUSD
    minimum_billed_chars: int = 0

    def __post_init__(self) -> None:
        if self.nusd_per_million_chars < 0:
            raise BudgetMisuseError("TTSPrice.nusd_per_million_chars cannot be negative")
        if self.minimum_billed_chars < 0:
            raise BudgetMisuseError("TTSPrice.minimum_billed_chars cannot be negative")

    @classmethod
    def from_usd_per_1k_chars(
        cls,
        usd_per_1k: str | int | float | Decimal,
        *,
        minimum_billed_chars: int = 0,
    ) -> "TTSPrice":
        return cls(
            nusd_per_million_chars=parse_usd(usd_per_1k) * 1000,
            minimum_billed_chars=minimum_billed_chars,
        )

    FREE: ClassVar["TTSPrice"]


TTSPrice.FREE = TTSPrice(0)


@dataclass(frozen=True, slots=True)
class PriceTable:
    """One coherent vendor mix, with provenance attached."""

    label: str
    llm: LLMPrice
    stt: STTPrice
    tts: TTSPrice
    source: str = ""
    valid_until: date | None = None

    def is_stale(self, today: date | None = None) -> bool:
        """True once a promotional rate has expired. Advisory: never alters cost."""
        if self.valid_until is None:
            return False
        return (today or date.today()) > self.valid_until


#: Everything on-device. The LOCAL and CANNED tiers resolve here.
FREE_PRICES: Final[PriceTable] = PriceTable(
    label="local (on-device)",
    llm=LLMPrice.FREE,
    stt=STTPrice.FREE,
    tts=TTSPrice.FREE,
    source="the player's own silicon; marginal cost to the studio is zero by construction",
)


# -- Bundled rate cards ------------------------------------------------
# All figures read from the vendors' own pricing pages on 2026-08-31.
# Re-verify before shipping: the premium table carries a promotional STT rate
# and is dated accordingly.

CLOUD_CHEAP_PRICES: Final[PriceTable] = PriceTable(
    label="cheap cloud (gpt-5-nano + AssemblyAI + Deepgram Aura-1)",
    llm=LLMPrice.from_usd(
        input_per_mtok="0.05", output_per_mtok="0.40", cached_input_per_mtok="0.005"
    ),
    stt=STTPrice.from_usd_per_minute("0.0025"),
    tts=TTSPrice.from_usd_per_1k_chars("0.015"),
    source=(
        "developers.openai.com/api/docs/pricing; assemblyai.com/pricing "
        "(Universal-Streaming, $0.15/hr); deepgram.com/pricing (Aura-1) — read 2026-08-31"
    ),
)

CLOUD_PREMIUM_PRICES: Final[PriceTable] = PriceTable(
    label="premium cloud (Claude Haiku 4.5 + Deepgram Nova-3 + ElevenLabs Flash)",
    llm=LLMPrice.from_usd(
        input_per_mtok="1.00",
        output_per_mtok="5.00",
        cached_input_per_mtok="0.10",
        cache_write_per_mtok="1.25",
    ),
    # Nova-3 streaming was on a limited-time promo at $0.0048/min; the regular
    # rate on the same page was $0.0077/min. The promo rate is used here and
    # dated, so an expiry surfaces as staleness rather than as a silent lie.
    stt=STTPrice.from_usd_per_minute("0.0048"),
    tts=TTSPrice.from_usd_per_1k_chars("0.05"),
    source=(
        "platform.claude.com/docs/en/about-claude/pricing; deepgram.com/pricing "
        "(Nova-3 streaming, promotional); elevenlabs.io/pricing/api (Flash) — read 2026-08-31"
    ),
    valid_until=date(2026, 12, 31),
)


@dataclass(frozen=True, slots=True)
class PriceBook:
    """The ``CostTier -> PriceTable`` mapping the router and ledger share.

    Frozen so a price cannot change under an in-flight authorization: a turn
    is quoted, reserved and settled against one rate card.
    """

    tables: Mapping[CostTier, PriceTable] = field(default_factory=dict)

    def __post_init__(self) -> None:
        merged = dict(self.tables)
        # CANNED and LOCAL are free by definition. Requiring the caller to say
        # so invites the one typo that would bill on-device inference.
        for free_tier in (CostTier.CANNED, CostTier.LOCAL):
            merged.setdefault(free_tier, FREE_PRICES)
        object.__setattr__(self, "tables", MappingProxyType(merged))

    def __getitem__(self, tier: CostTier) -> PriceTable:
        try:
            return self.tables[tier]
        except KeyError:
            raise BudgetMisuseError(
                f"no price table for {tier.name}; this PriceBook offers "
                f"{sorted(t.name for t in self.tables)}"
            ) from None

    def has(self, tier: CostTier) -> bool:
        return tier in self.tables

    def tiers(self) -> frozenset[CostTier]:
        return frozenset(self.tables)

    def stale_tables(self, today: date | None = None) -> tuple[PriceTable, ...]:
        """Tables whose promotional window has closed. For a startup warning."""
        return tuple(t for t in self.tables.values() if t.is_stale(today))

    @classmethod
    def local_only(cls) -> "PriceBook":
        """Fully on-device. Every turn costs exactly zero, forever."""
        return cls({})

    @classmethod
    def standard(cls) -> "PriceBook":
        """Local plus both bundled cloud tiers, at the rates cited above."""
        return cls(
            {
                CostTier.CLOUD_CHEAP: CLOUD_CHEAP_PRICES,
                CostTier.CLOUD_PREMIUM: CLOUD_PREMIUM_PRICES,
            }
        )

    @classmethod
    def hybrid_local_tts(cls) -> "PriceBook":
        """Cloud LLM and STT, **on-device TTS** — the configuration that matters.

        Speech synthesis is ~88% of a cheap-cloud voice turn, and a Piper-class
        voice is ~60 MB of CPU-only inference with no GPU and no vendor lock.
        Moving just that one modality on-device is the single largest cost
        lever available, and unlike a local LLM it runs on the low-VRAM,
        non-NVIDIA majority of the installed base.
        """
        return cls(
            {
                CostTier.CLOUD_CHEAP: PriceTable(
                    label="cheap cloud LLM+STT, local TTS",
                    llm=CLOUD_CHEAP_PRICES.llm,
                    stt=CLOUD_CHEAP_PRICES.stt,
                    tts=TTSPrice.FREE,
                    source=CLOUD_CHEAP_PRICES.source + " + on-device TTS (sherpa-onnx)",
                ),
                CostTier.CLOUD_PREMIUM: PriceTable(
                    label="premium cloud LLM+STT, local TTS",
                    llm=CLOUD_PREMIUM_PRICES.llm,
                    stt=CLOUD_PREMIUM_PRICES.stt,
                    tts=TTSPrice.FREE,
                    source=CLOUD_PREMIUM_PRICES.source + " + on-device TTS (sherpa-onnx)",
                ),
            }
        )
