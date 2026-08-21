"""Open-world NPC scenario — what it costs to voice a crowded district.

Simulates a market square: 240 NPCs around the player, one conversation in
progress, a walk across the square. Prints the two numbers a studio actually
decides on — how much of a CPU core the voices cost, and how much money the
script costs compared with recording it.

Run:  python examples/game_open_world.py
"""

from __future__ import annotations

import math
import random

from voicert.config import ConfigFactory
from voicert.game import (
    ComputeBudget,
    DialogueLOD,
    DialogueTier,
    LODConfig,
    NPCState,
    NPCVoicePool,
    VoiceCostModel,
)

RNG = random.Random(7)  # deterministic: the same square every run


def build_district(count: int = 240) -> list[NPCState]:
    """NPCs scattered over a 120 m square, a few of them story-relevant."""
    npcs: list[NPCState] = []
    for i in range(count):
        angle = RNG.uniform(0, 2 * math.pi)
        # sqrt keeps the density uniform instead of clumping at the centre
        radius = 120.0 * math.sqrt(RNG.random())
        npcs.append(
            NPCState(
                npc_id=f"npc-{i:03d}",
                distance_m=radius,
                audible=True,
                occluded=RNG.random() < 0.25,
                priority=RNG.choice([0.1, 0.1, 0.1, 0.4, 0.9]),
            )
        )
        npcs[-1].distance_m = round(npcs[-1].distance_m, 1)
    # The quest-giver the player walked up to.
    npcs[0].npc_id = "yorick-the-merchant"
    npcs[0].distance_m = 1.8
    npcs[0].occluded = False
    npcs[0].priority = 1.0
    npcs[0].in_conversation = True
    return npcs


def main() -> None:
    # 1. Size the pool from the CPU allowance, not from a guess.
    budget = ComputeBudget(rtf=0.10, cpu_share=0.35, speaking_duty=0.5)
    pool_size = budget.max_pool_size()

    print("=" * 66)
    print("COMPUTE BUDGET  (measure rtf on your minimum spec, not your dev box)")
    print("=" * 66)
    for k, v in budget.report().items():
        print(f"  {k:<26} {v}")
    print()

    # 2. Classify the whole district this tick.
    lod = DialogueLOD(LODConfig())
    district = build_district()
    buckets = lod.partition(district)

    print("=" * 66)
    print(f"DIALOGUE LOD    ({len(district)} NPCs in the square)")
    print("=" * 66)
    for tier in sorted(DialogueTier, key=lambda t: -t):
        members = buckets[tier]
        cost = {
            DialogueTier.LIVE: "full STT+LLM+TTS, one pool slot each",
            DialogueTier.BARK: "LLM picks a pre-baked line — no synthesis",
            DialogueTier.CROWD: "one shared murmur bed",
            DialogueTier.OFF: "nothing runs",
        }[tier]
        print(f"  {tier.name:<6} {len(members):>4} NPC   {cost}")
    print()

    # 3. Bind agents to the NPCs that earned a slot.
    pool = NPCVoicePool(
        capacity=pool_size,
        factory=lambda npc_id: ConfigFactory.build("npc"),
        on_evict=lambda npc_id: print(f"  ! {npc_id} evicted -> degraded to bark tier"),
    )
    live = pool.reconcile(buckets[DialogueTier.LIVE])

    print("=" * 66)
    print(f"VOICE POOL      (capacity {pool_size}, derived from the CPU budget)")
    print("=" * 66)
    print(f"  live agents in memory: {len(pool)} of {len(district)} NPCs")
    for voice in sorted(live, key=lambda v: -v.priority):
        print(f"    {voice.npc_id:<24} priority {voice.priority:.2f}")
    print()

    # 4. The player walks across the square: tiers shift, the pool follows.
    print("=" * 66)
    print("PLAYER WALKS 40 m ACROSS THE SQUARE")
    print("=" * 66)
    for step in range(1, 5):
        for npc in district:
            npc.distance_m = max(0.5, npc.distance_m - 10.0)
        district[0].in_conversation = step < 3  # conversation ends mid-walk
        buckets = lod.partition(district)
        pool.reconcile(buckets[DialogueTier.LIVE])
        print(
            f"  step {step}: "
            f"live={len(buckets[DialogueTier.LIVE]):>3}  "
            f"bark={len(buckets[DialogueTier.BARK]):>3}  "
            f"crowd={len(buckets[DialogueTier.CROWD]):>3}  "
            f"off={len(buckets[DialogueTier.OFF]):>3}  "
            f"| agents held: {len(pool)}"
        )
    print("\n  Agents held never exceeds the pool cap, whatever the crowd does.\n")

    # 5. The money question.
    model = VoiceCostModel(
        line_count=10_000,      # a modest open world
        vo_cost_per_line=12.0,  # replace with your own quote
        baked_tts_cost_per_line=0.02,
        rewrites=2,             # scripts always change
        dynamic_share=0.15,     # the lines that cannot be pre-baked
    )
    report = model.report()
    print("=" * 66)
    print("SCRIPT COST     (10,000 lines, 2 rewrites — substitute your quotes)")
    print("=" * 66)
    print(f"  recorded with actors      ${report['traditional_vo']:>12,.2f}")
    print(f"  every line baked (TTS)    ${report['baked_only']:>12,.2f}")
    print(f"  hybrid: baked + on-device ${report['hybrid_baked_plus_ondevice']:>12,.2f}")
    print(f"  runtime cost per player   ${report['runtime_cost_per_player']:>12,.2f}  <- the point")
    print()
    print("  On-device synthesis has no marginal cost: the player's silicon")
    print("  does the work, so dynamic dialogue does not scale with sales.")


if __name__ == "__main__":
    main()
