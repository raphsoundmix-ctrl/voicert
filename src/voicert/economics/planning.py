"""The arithmetic that turns the argument into a number.

"A player burned more in tokens than the game cost" is an anecdote. It becomes
checkable the moment you write down four things: what a turn actually
consumes, what the vendors actually charge, what a player is actually worth,
and what fraction of turns run on the player's own hardware.

Everything here is pure, so the same inputs always give the same integers.
That is the property that lets a studio put these numbers in a green-light
memo instead of a slide.

The bundled :meth:`TurnProfile.measured_npc` is a real measurement, not a
guess — see its docstring for the provenance and for the one field that is an
assumption rather than an observation.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from voicert.economics.money import BudgetMisuseError, NanoUSD, parse_usd, usd
from voicert.economics.prices import CostTier, PriceBook
from voicert.economics.usage import TierPlan, TurnUsage, price_turn


@dataclass(frozen=True, slots=True)
class TurnProfile:
    """The shape of a typical turn, as an input to a forecast.

    Named separately from :class:`~voicert.economics.usage.TurnUsage` because
    this one is a *prediction* about turns not yet taken, and conflating a
    forecast with a measurement is how cost models start lying.
    """

    usage: TurnUsage
    label: str = ""

    @classmethod
    def measured_npc(cls) -> "TurnProfile":
        """A real NPC turn, measured rather than assumed.

        Provenance: 12-turn conversation with a tavern-keeper persona
        (696-token system prompt: personality, 8 world facts, 3 few-shot
        exchanges, a 1-2 sentence constraint) against ``qwen3:1.7b`` on
        Ollama, RTX 4080, 2026-08-31. Prompt tokens grew 727 -> 982 over
        seven turns and then plateaued as the rolling 6-pair window
        saturated; the plateau value is used here. Output was 23 tokens at
        the median across the 12 turns.

        The ~700/282 split between cached and fresh prompt tokens reflects the
        stable persona prefix a real deployment would cache.

        ``audio_ms_in`` is the one **assumption**: 3 seconds for a spoken
        player line. It was not measured, and it moves the STT term only,
        which is under 10% of a cloud turn.
        """
        return cls(
            usage=TurnUsage(
                input_tokens=282,
                cached_input_tokens=700,
                output_tokens=23,
                audio_ms_in=3000,
                characters_out=92,  # 23 tokens at ~4 chars/token
            ),
            label="measured: qwen3:1.7b tavern keeper, 12 turns, 2026-08-31",
        )


@dataclass(frozen=True, slots=True)
class AffordabilityReport:
    """The dispute, as a value you can assert on."""

    revenue_per_player_nusd: NanoUSD
    margin_target: float
    cogs_allowance_nusd: NanoUSD
    cloud_tier: CostTier
    cost_per_cloud_turn_nusd: NanoUSD
    cost_per_local_turn_nusd: NanoUSD
    local_share: float
    blended_cost_per_turn_nusd: NanoUSD
    #: ``None`` means unbounded (the blended cost is exactly zero), which is
    #: emphatically not the same as zero affordable turns.
    affordable_turns: int | None
    breakdown_per_cloud_turn_nusd: dict[str, NanoUSD]

    @property
    def dominant_cloud_component(self) -> str:
        if not any(self.breakdown_per_cloud_turn_nusd.values()):
            return "none"
        return max(
            self.breakdown_per_cloud_turn_nusd,
            key=lambda k: self.breakdown_per_cloud_turn_nusd[k],
        )

    def cloud_component_share(self, component: str) -> float:
        total = sum(self.breakdown_per_cloud_turn_nusd.values())
        if not total:
            return 0.0
        return self.breakdown_per_cloud_turn_nusd[component] / total

    def as_usd(self) -> dict[str, Decimal | float | int | str | None]:
        return {
            "revenue_per_player": usd(self.revenue_per_player_nusd),
            "margin_target": self.margin_target,
            "cogs_allowance": usd(self.cogs_allowance_nusd),
            "cloud_tier": self.cloud_tier.name,
            "cost_per_cloud_turn": usd(self.cost_per_cloud_turn_nusd),
            "cost_per_local_turn": usd(self.cost_per_local_turn_nusd),
            "local_share": self.local_share,
            "blended_cost_per_turn": usd(self.blended_cost_per_turn_nusd),
            "affordable_turns": self.affordable_turns,
            "dominant_cloud_component": self.dominant_cloud_component,
        }


def ceiling_from_revenue(
    revenue_per_player_usd: str | int | float | Decimal,
    margin_target: float = 0.90,
) -> NanoUSD:
    """Revenue and margin in; the number for ``lifetime_ceiling_nusd`` out.

    Floors rather than ceilings — the only rounding in this package that goes
    *against* the house, because rounding a spending cap upward would let real
    spend exceed the margin target.
    """
    if not 0.0 <= margin_target < 1.0:
        raise BudgetMisuseError("margin_target must be within [0, 1)")
    # Decimal, not float: `10_000_000_000 * (1.0 - 0.90)` is 999_999_999.98 in
    # binary floating point, so the naive version quietly shaves a nano-dollar
    # off every ceiling it computes. This module argues against floats
    # elsewhere; it does not get an exemption here.
    share = Decimal(1) - Decimal(str(margin_target))
    return int(parse_usd(revenue_per_player_usd) * share)


def affordable_turns(
    *,
    book: PriceBook,
    profile: TurnProfile | None = None,
    revenue_per_player_usd: str | int | float | Decimal,
    margin_target: float = 0.90,
    local_share: float = 0.0,
    cloud_tier: CostTier = CostTier.CLOUD_CHEAP,
    cloud_plan: TierPlan | None = None,
) -> AffordabilityReport:
    """How many turns a player can afford, and what dominates the bill.

    ``local_share`` is the fraction of turns computed on the player's own
    hardware, which cost the studio nothing. ``cloud_plan`` lets the caller
    price a *mixed* turn — cloud brain, on-device voice — which is the
    configuration that changes the answer most.
    """
    if not 0.0 <= local_share <= 1.0:
        raise BudgetMisuseError("local_share must be within [0, 1]")
    profile = profile or TurnProfile.measured_npc()
    plan = cloud_plan or TierPlan.uniform(cloud_tier)

    cloud = price_turn(profile.usage, book, plan)
    local = price_turn(profile.usage, book, TierPlan.uniform(CostTier.LOCAL))

    allowance = ceiling_from_revenue(revenue_per_player_usd, margin_target)
    # Blend in Decimal and round up, so a mostly-local mix never reports a
    # cheaper blended turn than it can actually deliver. A float here would
    # make `local_share=1.0` land on a nonzero cost and turn "unbounded" into
    # a large finite number.
    cloud_share = Decimal(1) - Decimal(str(local_share))
    # The local term is included rather than assumed zero: a PriceBook *can*
    # map CostTier.LOCAL to a paid table (a metered on-prem GPU), and silently
    # dropping it would understate a mostly-local mix.
    blended = int(
        (
            cloud.total_nusd * cloud_share
            + local.total_nusd * Decimal(str(local_share))
        ).to_integral_value(rounding="ROUND_CEILING")
    )
    turns = None if blended == 0 else allowance // blended

    return AffordabilityReport(
        revenue_per_player_nusd=parse_usd(revenue_per_player_usd),
        margin_target=margin_target,
        cogs_allowance_nusd=allowance,
        cloud_tier=cloud_tier,
        cost_per_cloud_turn_nusd=cloud.total_nusd,
        cost_per_local_turn_nusd=local.total_nusd,
        local_share=local_share,
        blended_cost_per_turn_nusd=blended,
        affordable_turns=turns,
        breakdown_per_cloud_turn_nusd={
            "llm": cloud.llm_nusd,
            "stt": cloud.stt_nusd,
            "tts": cloud.tts_nusd,
        },
    )


def required_local_share(
    *,
    book: PriceBook,
    revenue_per_player_usd: str | int | float | Decimal,
    expected_turns: int,
    profile: TurnProfile | None = None,
    margin_target: float = 0.90,
    cloud_tier: CostTier = CostTier.CLOUD_CHEAP,
    cloud_plan: TierPlan | None = None,
) -> float:
    """The inverse, and the question a studio actually has to answer.

    To serve ``expected_turns`` per player at the target margin, what fraction
    must run on the player's hardware? Returns ``0.0`` when the cloud alone
    already clears margin and ``1.0`` when nothing short of fully local will.
    """
    if expected_turns < 0:
        raise BudgetMisuseError("expected_turns cannot be negative")
    if expected_turns == 0:
        return 0.0
    profile = profile or TurnProfile.measured_npc()
    plan = cloud_plan or TierPlan.uniform(cloud_tier)

    per_turn = price_turn(profile.usage, book, plan).total_nusd
    if per_turn == 0:
        return 0.0
    allowance = ceiling_from_revenue(revenue_per_player_usd, margin_target)
    affordable_cloud_turns = allowance / per_turn
    if affordable_cloud_turns >= expected_turns:
        return 0.0
    return max(0.0, min(1.0, 1.0 - affordable_cloud_turns / expected_turns))
