"""Game layer acceptance: dialogue LOD tiers + hysteresis, bounded voice pool
with priority eviction, budget maths, and the middleware sink contract."""

import pytest

from voicert.config import ConfigFactory
from voicert.frames import AudioFrame, InterruptionFrame, InterruptionReason
from voicert.game import (
    ComputeBudget,
    DialogueLOD,
    DialogueTier,
    LODConfig,
    NPCState,
    NPCVoicePool,
    NullSink,
    VoiceCostModel,
)


# -- dialogue LOD ------------------------------------------------------


def test_tiers_by_distance():
    lod = DialogueLOD(LODConfig())
    assert lod.evaluate(NPCState("a", distance_m=2.0)) is DialogueTier.LIVE
    assert lod.evaluate(NPCState("b", distance_m=15.0)) is DialogueTier.BARK
    assert lod.evaluate(NPCState("c", distance_m=45.0)) is DialogueTier.CROWD
    assert lod.evaluate(NPCState("d", distance_m=200.0)) is DialogueTier.OFF


def test_hysteresis_prevents_tier_thrash():
    """An NPC at 7.5 m is inside the live *exit* band but outside the *enter*
    band: already-live stays live, newly-approaching does not get promoted."""
    lod = DialogueLOD(LODConfig())

    approaching = NPCState("walk-in", distance_m=7.5, tier=DialogueTier.BARK)
    assert lod.evaluate(approaching) is DialogueTier.BARK

    leaving = NPCState("walk-out", distance_m=7.5, tier=DialogueTier.LIVE)
    assert lod.evaluate(leaving) is DialogueTier.LIVE


def test_inaudible_npc_is_off():
    lod = DialogueLOD(LODConfig())
    assert lod.evaluate(NPCState("x", distance_m=1.0, audible=False)) is DialogueTier.OFF


def test_occlusion_costs_one_tier():
    lod = DialogueLOD(LODConfig())
    clear = lod.evaluate(NPCState("clear", distance_m=3.0))
    walled = lod.evaluate(NPCState("walled", distance_m=3.0, occluded=True))
    assert clear is DialogueTier.LIVE
    assert walled is DialogueTier.BARK


def test_conversation_overrides_distance():
    """Walking away mid-sentence must not cut the character off."""
    lod = DialogueLOD(LODConfig())
    npc = NPCState("quest-giver", distance_m=40.0, in_conversation=True)
    assert lod.evaluate(npc) is DialogueTier.LIVE


def test_partition_groups_a_whole_tick():
    lod = DialogueLOD(LODConfig())
    crowd = [NPCState(f"npc-{i}", distance_m=float(i)) for i in range(0, 100, 5)]
    buckets = lod.partition(crowd)
    assert sum(len(v) for v in buckets.values()) == len(crowd)
    # An open-world tick: a couple of live NPCs, the rest cheap or free.
    assert len(buckets[DialogueTier.LIVE]) <= 2


def test_lod_config_rejects_overlapping_bands():
    with pytest.raises(ValueError):
        LODConfig(live_enter_m=10.0, live_exit_m=5.0)
    with pytest.raises(ValueError):
        LODConfig(bark_enter_m=100.0)  # would swallow the crowd band


# -- voice pool ---------------------------------------------------------


def _factory(npc_id: str):
    return ConfigFactory.build("npc")


def test_pool_caps_concurrent_agents():
    pool = NPCVoicePool(capacity=3, factory=_factory)
    district = [NPCState(f"vendor-{i}", distance_m=2.0, priority=float(i)) for i in range(50)]
    for npc in district:
        pool.acquire(npc)
    assert len(pool) == 3, "500 NPCs in the world, 3 agents in memory"


def test_pool_evicts_lowest_priority_and_degrades_it():
    evicted: list[str] = []
    pool = NPCVoicePool(capacity=2, factory=_factory, on_evict=evicted.append)

    pool.acquire(NPCState("ambient", distance_m=3.0, priority=0.1))
    pool.acquire(NPCState("merchant", distance_m=3.0, priority=0.5))
    pool.acquire(NPCState("quest-giver", distance_m=3.0, priority=0.9))

    assert "quest-giver" in pool
    assert "ambient" not in pool
    assert evicted == ["ambient"], "evicted NPC is handed back to the bark tier"


def test_pool_refuses_to_evict_for_lower_priority():
    pool = NPCVoicePool(capacity=1, factory=_factory)
    pool.acquire(NPCState("boss", distance_m=3.0, priority=1.0))
    got = pool.acquire(NPCState("nobody", distance_m=3.0, priority=0.1))
    assert got is None
    assert "boss" in pool


def test_acquire_is_idempotent_per_npc():
    pool = NPCVoicePool(capacity=2, factory=_factory)
    first = pool.acquire(NPCState("yorick", distance_m=2.0, priority=0.5))
    second = pool.acquire(NPCState("yorick", distance_m=2.0, priority=0.7))
    assert first is second
    assert len(pool) == 1
    assert second is not None and second.priority == 0.7


def test_reconcile_releases_npcs_that_left_the_live_tier():
    pool = NPCVoicePool(capacity=3, factory=_factory)
    a = NPCState("a", distance_m=2.0, priority=0.9)
    b = NPCState("b", distance_m=2.0, priority=0.8)
    pool.reconcile([a, b])
    assert pool.active_ids() == {"a", "b"}

    pool.reconcile([a])  # b walked away
    assert pool.active_ids() == {"a"}


def test_pool_capacity_must_be_positive():
    with pytest.raises(ValueError):
        NPCVoicePool(capacity=0, factory=_factory)


# -- budget -------------------------------------------------------------


def test_pool_size_derived_from_cpu_budget():
    budget = ComputeBudget(rtf=0.10, cpu_share=0.35, speaking_duty=0.5)
    assert budget.cost_per_live_npc == pytest.approx(0.05)
    assert budget.max_pool_size() == 7


def test_faster_tts_buys_more_voices():
    slow = ComputeBudget(rtf=0.20, cpu_share=0.30, speaking_duty=1.0)
    fast = ComputeBudget(rtf=0.05, cpu_share=0.30, speaking_duty=1.0)
    assert fast.max_pool_size() > slow.max_pool_size()


def test_unaffordable_budget_fails_loudly():
    """A silent game is worse than a startup error."""
    with pytest.raises(ValueError, match="cannot afford a single voice"):
        ComputeBudget(rtf=0.9, cpu_share=0.1, speaking_duty=1.0).max_pool_size()


def test_hybrid_costs_less_than_recorded_vo():
    model = VoiceCostModel(line_count=10_000, rewrites=2, dynamic_share=0.15)
    report = model.report()
    assert report["hybrid_baked_plus_ondevice"] < report["traditional_vo"]
    assert report["runtime_cost_per_player"] == 0.0


def test_vo_cost_derived_from_session_rate():
    """SAG-AFTRA IMA minimum: $1,134.95 per 4-hour day, up to 3 voices."""
    model = VoiceCostModel.from_session_rate(
        line_count=1_000, session_cost=1_134.95, lines_per_session=250, overhead_multiplier=1.6
    )
    assert model.vo_cost_per_line == pytest.approx(7.26, abs=0.01)
    assert model.traditional_vo() > model.hybrid() * 100


def test_session_rate_rejects_zero_throughput():
    with pytest.raises(ValueError):
        VoiceCostModel.from_session_rate(line_count=100, lines_per_session=0)


def test_rewrites_multiply_vo_but_barely_touch_synthesis():
    once = VoiceCostModel(line_count=5_000, rewrites=0)
    thrice = VoiceCostModel(line_count=5_000, rewrites=2)
    vo_growth = thrice.traditional_vo() - once.traditional_vo()
    hybrid_growth = thrice.hybrid() - once.hybrid()
    assert vo_growth > hybrid_growth * 50


# -- middleware sink ----------------------------------------------------


async def test_sink_receives_agent_audio_and_flushes_on_barge_in():
    sink = NullSink("yorick")
    await sink(AudioFrame(pcm=b"\x00\x01" * 160, source="agent"))
    await sink(AudioFrame(pcm=b"\x00\x01" * 160, source="agent"))
    assert sink.total_bytes == 640

    await sink(InterruptionFrame(reason=InterruptionReason.USER_BARGE_IN, turn_id=1))
    assert sink.total_bytes == 0, "queued audio must not play after the cut"
    assert sink.flushes == 1


async def test_sink_ignores_user_audio():
    sink = NullSink()
    await sink(AudioFrame(pcm=b"\x00\x01" * 160, source="user"))
    assert sink.chunks == []


async def test_full_npc_turn_reaches_the_sink():
    """End-to-end: the NPC profile pipeline drives a middleware sink."""
    sink = NullSink("yorick")
    runtime = ConfigFactory.build("npc")
    runtime.pipeline._sink = sink  # engine wiring: middleware replaces transport
    await runtime.start()
    try:
        await runtime.say("What is for sale today?")
        for _ in range(200):
            if sink.chunks:
                break
            await __import__("asyncio").sleep(0.01)
        assert sink.chunks, "generated NPC voice must reach the game audio bus"
    finally:
        await runtime.stop()
