"""Print the unit economics of a voice NPC, from measured usage and cited rates.

    python tools/unit_economics.py
    python tools/unit_economics.py --revenue 10 --margin 0.9 --turns 500

Every number is computed by :mod:`voicert.economics` from one measured turn
profile and the rate cards in ``prices.py``. Nothing here is typed in by hand,
so disagreeing with the conclusion means disagreeing with an input — which is
the point. Change ``--revenue``, ``--turns`` or the profile and watch it move.
"""

from __future__ import annotations

import argparse

from voicert.economics import (
    CostTier,
    PriceBook,
    TierPlan,
    TurnProfile,
    affordable_turns,
    format_usd,
    price_turn,
    required_local_share,
)

STACKS: list[tuple[str, PriceBook, TierPlan]] = [
    ("all cloud, cheap", PriceBook.standard(), TierPlan.uniform(CostTier.CLOUD_CHEAP)),
    (
        "all cloud, premium",
        PriceBook.standard(),
        TierPlan.uniform(CostTier.CLOUD_PREMIUM),
    ),
    (
        "cloud brain, local voice",
        PriceBook.hybrid_local_tts(),
        TierPlan.local_tts(CostTier.CLOUD_CHEAP),
    ),
    (
        "premium brain, local voice",
        PriceBook.hybrid_local_tts(),
        TierPlan.local_tts(CostTier.CLOUD_PREMIUM),
    ),
    ("fully on-device", PriceBook.local_only(), TierPlan.uniform(CostTier.LOCAL)),
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--revenue", default="10.00", help="net revenue per player, USD")
    ap.add_argument("--margin", type=float, default=0.90, help="target gross margin")
    ap.add_argument(
        "--turns", type=int, default=500, help="expected voice turns per player"
    )
    args = ap.parse_args()

    profile = TurnProfile.measured_npc()
    u = profile.usage
    print(f"\nTurn profile — {profile.label}")
    print(
        f"  {u.input_tokens} fresh + {u.cached_input_tokens} cached prompt tokens, "
        f"{u.output_tokens} output tokens,\n"
        f"  {u.audio_ms_in} ms audio in (assumption), {u.characters_out} characters synthesized"
    )

    print(f"\nCost per turn, and what {format_usd(15 * 10**9, 2)} of spend buys:\n")
    header = f"  {'stack':<28}{'per turn':>12}{'LLM':>7}{'STT':>7}{'TTS':>7}   {'$15 buys':>12}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for label, book, plan in STACKS:
        cost = price_turn(u, book, plan)
        buys = "unbounded" if cost.is_free else f"{15 * 10**9 // cost.total_nusd:,}"
        print(
            f"  {label:<28}{format_usd(cost.total_nusd):>12}"
            f"{cost.share('llm'):>6.1%}{cost.share('stt'):>7.1%}{cost.share('tts'):>7.1%}"
            f"   {buys:>12}"
        )

    report = affordable_turns(
        book=PriceBook.standard(),
        revenue_per_player_usd=args.revenue,
        margin_target=args.margin,
    )
    print(
        f"\nAt {format_usd(report.revenue_per_player_nusd, 2)} net per player and "
        f"{args.margin:.0%} target margin, the AI budget is "
        f"{format_usd(report.cogs_allowance_nusd, 2)} per player."
    )
    print(
        f"  All-cloud, that funds {report.affordable_turns:,} turns "
        f"(dominated by {report.dominant_cloud_component.upper()}, "
        f"{report.cloud_component_share(report.dominant_cloud_component):.0%} of the bill)."
    )

    hybrid = affordable_turns(
        book=PriceBook.hybrid_local_tts(),
        revenue_per_player_usd=args.revenue,
        margin_target=args.margin,
        cloud_plan=TierPlan.local_tts(CostTier.CLOUD_CHEAP),
    )
    print(f"  Moving only TTS on-device: {hybrid.affordable_turns:,} turns.")

    mixes: list[tuple[str, PriceBook, TierPlan | None]] = [
        ("all cloud", PriceBook.standard(), None),
        (
            "cloud brain, local voice",
            PriceBook.hybrid_local_tts(),
            TierPlan.local_tts(CostTier.CLOUD_CHEAP),
        ),
    ]
    for mix_label, mix_book, mix_plan in mixes:
        share = required_local_share(
            book=mix_book,
            revenue_per_player_usd=args.revenue,
            expected_turns=args.turns,
            margin_target=args.margin,
            cloud_plan=mix_plan,
        )
        verdict = (
            "cloud alone clears the margin"
            if share == 0.0
            else f"{share:.0%} of turns must run on the player's hardware"
        )
        print(f"\nTo serve {args.turns:,} turns/player — {mix_label}: {verdict}.")

    print(
        "\nRates: "
        + PriceBook.standard()[CostTier.CLOUD_CHEAP].source
        + "\nRe-verify before shipping; two of the surveyed rates were promotional.\n"
    )


if __name__ == "__main__":
    main()
