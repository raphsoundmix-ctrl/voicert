"""Runtime cost as an enforced property.

The load-bearing claims are the ones about the *boundary*: a ceiling that can
be exceeded by a race, a rounding rule that loses sub-nano charges, or a
denial that reaches the player as an error are each enough to make the
guarantee false. Those get tests before the arithmetic does.
"""

import json
import threading
from datetime import date

import pytest

from voicert.economics import (
    BudgetDenied,
    BudgetExceeded,
    BudgetMisuseError,
    BudgetPolicy,
    CostTier,
    LLMPrice,
    PlayerLedger,
    PriceBook,
    PriceTable,
    STTPrice,
    SessionLedger,
    TierPlan,
    TierRouter,
    TTSPrice,
    TurnProfile,
    TurnUsage,
    affordable_turns,
    ceiling_from_revenue,
    parse_usd,
    price_turn,
    required_local_share,
    shadow_cost,
)

USD = 1_000_000_000


def policy(turn: int, session: int, lifetime: int, **kw) -> BudgetPolicy:
    kw.setdefault(
        "allowed_tiers",
        frozenset({CostTier.CANNED, CostTier.LOCAL, CostTier.CLOUD_CHEAP}),
    )
    return BudgetPolicy(turn, session, lifetime, **kw)


def ledgers(pol: BudgetPolicy, book: PriceBook | None = None):
    player = PlayerLedger("p1", pol)
    return player, SessionLedger("s1", player, pol, book or PriceBook.standard())


# -- money ---------------------------------------------------------------


def test_parse_usd_does_not_inherit_float_error():
    """0.1 must be 0.1, not 0.1000000000000000055."""
    assert parse_usd("0.1") == 100_000_000
    assert parse_usd(0.1) == 100_000_000
    assert parse_usd("0.05", per=1000) == 50_000


def test_parse_usd_rejects_nonsense():
    for bad in ("-1", float("inf"), float("nan"), "banana"):
        with pytest.raises(BudgetMisuseError):
            parse_usd(bad)


def test_sub_nano_charges_round_up_not_away():
    """A single token at the cheapest surveyed rate must still cost something.

    At $0.005/MTok one token is 5 nUSD. In micro-dollars it would truncate to
    zero and a 10,000-turn session would bill $0.00.
    """
    book = PriceBook(
        {
            CostTier.CLOUD_CHEAP: PriceTable(
                "t",
                LLMPrice.from_usd(input_per_mtok="0.005", output_per_mtok="0"),
                STTPrice.FREE,
                TTSPrice.FREE,
            )
        }
    )
    cost = price_turn(
        TurnUsage(input_tokens=1), book, TierPlan.uniform(CostTier.CLOUD_CHEAP)
    )
    assert cost.total_nusd == 5


def test_free_stays_exactly_zero():
    """Ceiling rounding must not accrete phantom pennies on the local path."""
    cost = price_turn(
        TurnProfile.measured_npc().usage,
        PriceBook.local_only(),
        TierPlan.uniform(CostTier.LOCAL),
    )
    assert cost.total_nusd == 0
    assert cost.is_free
    assert cost.dominant_component == "none"


# -- the finding that reframes the whole argument ------------------------


def test_tts_dominates_a_cloud_voice_turn_not_the_llm():
    """The headline result: 'tokens' are the cheapest part of a voice turn.

    If this ever inverts, the framework's cost advice is pointed at the wrong
    component and the README needs rewriting — hence a test, not a comment.
    """
    cost = price_turn(
        TurnProfile.measured_npc().usage,
        PriceBook.standard(),
        TierPlan.uniform(CostTier.CLOUD_CHEAP),
    )
    assert cost.dominant_component == "tts"
    assert cost.share("tts") > 0.85
    assert cost.share("llm") < 0.05


def test_moving_only_tts_on_device_is_a_large_multiple():
    """One modality moved on-device, an order of magnitude off the bill."""
    usage = TurnProfile.measured_npc().usage
    all_cloud = price_turn(
        usage, PriceBook.standard(), TierPlan.uniform(CostTier.CLOUD_CHEAP)
    )
    local_tts = price_turn(
        usage, PriceBook.hybrid_local_tts(), TierPlan.local_tts(CostTier.CLOUD_CHEAP)
    )
    assert local_tts.total_nusd * 8 < all_cloud.total_nusd


def test_disjoint_cached_contract_survives_both_vendor_conventions():
    """OpenAI reports prompt_tokens INCLUSIVE of cached; Anthropic EXCLUSIVE.

    Getting this backwards misprices the measured turn by ~70%, so the two
    readers must converge on the same disjoint split.
    """
    openai = TurnUsage.from_openai_usage(
        {
            "prompt_tokens": 982,
            "prompt_tokens_details": {"cached_tokens": 700},
            "completion_tokens": 23,
        }
    )
    anthropic = TurnUsage.from_anthropic_usage(
        {"input_tokens": 282, "cache_read_input_tokens": 700, "output_tokens": 23}
    )
    assert openai.input_tokens == anthropic.input_tokens == 282
    assert openai.cached_input_tokens == anthropic.cached_input_tokens == 700
    assert openai.total_prompt_tokens == 982


def test_openai_reader_rejects_impossible_payload():
    with pytest.raises(BudgetMisuseError):
        TurnUsage.from_openai_usage(
            {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 500}}
        )


def test_ollama_usage_is_counted_as_uncached_upper_bound():
    """Ollama cannot separate reused KV prefix from fresh prefill, so the
    shadow price must err upward rather than flatter the local case."""
    usage = TurnUsage.from_ollama_usage(
        {"prompt_eval_count": 982, "eval_count": 23}, characters_out=92
    )
    assert usage.input_tokens == 982
    assert usage.cached_input_tokens == 0
    assert shadow_cost(usage, PriceBook.standard()).total_nusd > 0


# -- enforcement ---------------------------------------------------------


def test_denial_is_a_value_not_an_exception():
    """A player must never meet an error dialog because of a spending cap."""
    _, session = ledgers(policy(0, 0, 0))
    result = session.authorize(
        TurnProfile.measured_npc().usage, TierPlan.uniform(CostTier.CLOUD_CHEAP)
    )
    assert isinstance(result, BudgetDenied)
    assert not isinstance(result, Exception)
    assert result.fallback.chosen is CostTier.CANNED
    assert result.fallback.estimate.is_free


def test_every_denial_carries_an_affordable_fallback():
    """Totality: CANNED costs zero, so the ladder always terminates."""
    usage = TurnProfile.measured_npc().usage
    for ceilings in [(0, 0, 0), (1, 1, 1), (100, 200, 300), (10**6, 10**6, 10**6)]:
        _, session = ledgers(policy(*ceilings))
        result = session.authorize(usage, TierPlan.uniform(CostTier.CLOUD_CHEAP))
        if isinstance(result, BudgetDenied):
            assert result.fallback.estimate.total_nusd <= result.remaining_nusd or (
                result.fallback.estimate.is_free
            )


def test_authorize_or_raise_is_the_only_raising_path():
    _, session = ledgers(policy(0, 0, 0))
    with pytest.raises(BudgetExceeded) as exc:
        session.authorize_or_raise(
            TurnProfile.measured_npc().usage, TierPlan.uniform(CostTier.CLOUD_CHEAP)
        )
    assert isinstance(exc.value.denial, BudgetDenied)


def test_concurrent_turns_cannot_jointly_overspend():
    """Two NPCs on one player each pass a check, then jointly bust the cap.

    This is the race that makes check-then-spend wrong and reserve-then-settle
    necessary.
    """
    pol = policy(60, 100, 100)
    player, session = ledgers(pol, PriceBook.standard())
    book = PriceBook(
        {
            CostTier.CLOUD_CHEAP: PriceTable(
                "flat",
                LLMPrice(input_nusd_per_mtok=60_000, output_nusd_per_mtok=0),
                STTPrice.FREE,
                TTSPrice.FREE,
            )
        }
    )
    session = SessionLedger("s", player, pol, book)
    usage = TurnUsage(input_tokens=1000)  # exactly 60 nUSD
    first = session.authorize(usage, TierPlan.uniform(CostTier.CLOUD_CHEAP))
    second = session.authorize(usage, TierPlan.uniform(CostTier.CLOUD_CHEAP))
    assert not isinstance(first, BudgetDenied)
    assert isinstance(second, BudgetDenied), "reservation must block the second"
    assert second.reason.value == "session_ceiling"


def test_ceiling_holds_under_concurrent_threads():
    """The engine bridge thread and the asyncio loop touch the same ledger."""
    pol = policy(10, 10**9, 10**9)
    book = PriceBook(
        {
            CostTier.CLOUD_CHEAP: PriceTable(
                "unit",
                LLMPrice(input_nusd_per_mtok=1_000_000, output_nusd_per_mtok=0),
                STTPrice.FREE,
                TTSPrice.FREE,
            )
        }
    )
    player = PlayerLedger("p", pol)
    session = SessionLedger("s", player, pol, book)
    usage = TurnUsage(input_tokens=1)  # 1 nUSD

    def worker() -> None:
        for _ in range(200):
            auth = session.authorize(usage, TierPlan.uniform(CostTier.CLOUD_CHEAP))
            if not isinstance(auth, BudgetDenied):
                session.settle(auth)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert player.lifetime_spent_nusd == 8 * 200
    assert player.turns_settled == 8 * 200


def test_reserve_headroom_makes_the_ceiling_a_hard_bound():
    """Worst-case exposure — spent plus in-flight — never exceeds the cap."""
    pol = policy(50, 500, 500, reserve_headroom=True)
    book = PriceBook(
        {
            CostTier.CLOUD_CHEAP: PriceTable(
                "unit",
                LLMPrice(input_nusd_per_mtok=50_000, output_nusd_per_mtok=0),
                STTPrice.FREE,
                TTSPrice.FREE,
            )
        }
    )
    player = PlayerLedger("p", pol)
    session = SessionLedger("s", player, pol, book)
    usage = TurnUsage(input_tokens=1000)
    open_auths = []
    for _ in range(40):
        auth = session.authorize(usage, TierPlan.uniform(CostTier.CLOUD_CHEAP))
        if isinstance(auth, BudgetDenied):
            break
        open_auths.append(auth)
        assert player.worst_case_exposure_nusd <= pol.lifetime_ceiling_nusd
    assert open_auths, "some turns must have been authorized"
    assert player.worst_case_exposure_nusd <= pol.lifetime_ceiling_nusd


def test_settle_records_an_overrun_and_denies_the_next_turn():
    """Money genuinely spent must be recorded even when it busts the estimate;
    refusing to record it would corrupt the lifetime figure permanently."""
    pol = policy(10**9, 10**9, 10**9)
    book = PriceBook(
        {
            CostTier.CLOUD_CHEAP: PriceTable(
                "unit",
                LLMPrice(input_nusd_per_mtok=1_000_000, output_nusd_per_mtok=0),
                STTPrice.FREE,
                TTSPrice.FREE,
            )
        }
    )
    player = PlayerLedger("p", pol)
    session = SessionLedger("s", player, pol, book)
    auth = session.authorize(
        TurnUsage(input_tokens=10), TierPlan.uniform(CostTier.CLOUD_CHEAP)
    )
    assert not isinstance(auth, BudgetDenied)
    cost = session.settle(auth, TurnUsage(input_tokens=400))  # 40x the estimate
    assert cost.total_nusd == 400
    assert player.lifetime_spent_nusd == 400
    assert session.close().overruns == 1


def test_double_settle_is_misuse_not_denial():
    _, session = ledgers(policy(10**9, 10**9, 10**9))
    auth = session.authorize(
        TurnProfile.measured_npc().usage, TierPlan.uniform(CostTier.CLOUD_CHEAP)
    )
    assert not isinstance(auth, BudgetDenied)
    session.settle(auth)
    with pytest.raises(BudgetMisuseError):
        session.settle(auth)


def test_leaked_authorization_is_reclaimed_and_reported():
    """A turn that never settles is a caller bug that must fail CI without
    crashing a shipped game."""
    player, session = ledgers(policy(10**9, 10**9, 10**9))
    session.authorize(
        TurnProfile.measured_npc().usage, TierPlan.uniform(CostTier.CLOUD_CHEAP)
    )
    summary = session.close()
    assert summary.leaked_authorizations == 1
    assert player.worst_case_exposure_nusd == player.lifetime_spent_nusd


# -- barge-in as a cost mechanism ----------------------------------------


def test_barge_in_charges_the_prompt_but_not_the_unspoken_reply():
    """The prompt left the machine and is owed; the rest of the reply is not.

    This is where the framework's existing interruption machinery reaches the
    invoice.
    """
    _, session = ledgers(policy(10**9, 10**9, 10**9))
    planned = TurnUsage(
        input_tokens=282,
        cached_input_tokens=700,
        output_tokens=200,
        audio_ms_in=3000,
        characters_out=800,
    )
    auth = session.authorize(planned, TierPlan.uniform(CostTier.CLOUD_CHEAP))
    assert not isinstance(auth, BudgetDenied)
    spoken = planned.with_output(output_tokens=6, characters_out=24)
    cost = session.abandon(auth, spoken)
    assert cost.total_nusd < auth.reserved_nusd
    assert cost.stt_nusd == auth.estimate.stt_nusd, "audio was already sent"
    assert cost.tts_nusd * 10 < auth.estimate.tts_nusd


def test_abandon_without_partial_charges_input_side_only():
    _, session = ledgers(policy(10**9, 10**9, 10**9))
    planned = TurnUsage(input_tokens=282, output_tokens=200, characters_out=800)
    auth = session.authorize(planned, TierPlan.uniform(CostTier.CLOUD_CHEAP))
    assert not isinstance(auth, BudgetDenied)
    cost = session.abandon(auth)
    assert cost.tts_nusd == 0
    assert cost.llm_nusd > 0


def test_record_free_refuses_a_billable_turn():
    _, session = ledgers(policy(10**9, 10**9, 10**9))
    session.record_free(
        TurnProfile.measured_npc().usage, TierPlan.uniform(CostTier.LOCAL)
    )
    with pytest.raises(BudgetMisuseError):
        session.record_free(
            TurnProfile.measured_npc().usage, TierPlan.uniform(CostTier.CLOUD_CHEAP)
        )


# -- policy --------------------------------------------------------------


def test_unordered_ceilings_are_rejected():
    """An unordered set silently means the narrower ceiling never binds."""
    with pytest.raises(BudgetMisuseError):
        BudgetPolicy(100, 10, 1000)
    with pytest.raises(BudgetMisuseError):
        BudgetPolicy(-1, 10, 100)


def test_policy_from_revenue_encodes_the_margin_decision():
    """A $10 game at 90% margin may spend $1.00 per player, and no more."""
    pol = BudgetPolicy.from_revenue("10.00", margin_target=0.90)
    assert pol.lifetime_ceiling_nusd == 1 * USD
    assert pol.per_session_ceiling_nusd <= pol.lifetime_ceiling_nusd
    assert pol.per_turn_ceiling_nusd <= pol.per_session_ceiling_nusd


def test_local_only_policy_cannot_bill():
    pol = BudgetPolicy.local_only()
    player = PlayerLedger("p", pol)
    session = SessionLedger("s", player, pol, PriceBook.local_only())
    for _ in range(1000):
        session.record_free(
            TurnProfile.measured_npc().usage, TierPlan.uniform(CostTier.LOCAL)
        )
    assert player.lifetime_spent_nusd == 0


# -- routing -------------------------------------------------------------


def test_npcs_out_of_earshot_never_reach_a_paid_provider():
    """Cost must scale with what the player can hear, not with NPC count."""
    pol = policy(10**9, 10**9, 10**9)
    _, session = ledgers(pol)
    router = TierRouter(pol, PriceBook.standard())
    for tier in (0, 1, 2):  # OFF, CROWD, BARK
        decision = router.plan(
            session=session,
            estimate=TurnProfile.measured_npc().usage,
            dialogue_tier=tier,
        )
        assert decision.chosen is CostTier.CANNED
        assert decision.estimate.is_free


def test_router_degrades_instead_of_failing_when_budget_runs_out():
    pol = policy(1, 1, 1)
    _, session = ledgers(pol)
    router = TierRouter(pol, PriceBook.standard(), local_available=False)
    decision = router.plan(
        session=session, estimate=TurnProfile.measured_npc().usage, dialogue_tier=3
    )
    assert decision.chosen is CostTier.CANNED
    assert decision.degraded


def test_downgrade_never_upgrades_a_cheaper_modality():
    """The classic bug: a global 'fall back to CLOUD_CHEAP' promotes an
    on-device TTS into a paid one."""
    plan = TierPlan.local_tts(CostTier.CLOUD_PREMIUM)
    assert plan.tts is CostTier.LOCAL
    downgraded = plan.downgraded_to(CostTier.CLOUD_CHEAP)
    assert downgraded.tts is CostTier.LOCAL, "TTS must stay local"
    assert downgraded.llm is CostTier.CLOUD_CHEAP


# -- the arithmetic that settles the dispute -----------------------------


def test_affordable_turns_reproduces_the_published_figure():
    report = affordable_turns(
        book=PriceBook.standard(),
        revenue_per_player_usd="10.00",
        margin_target=0.90,
        cloud_tier=CostTier.CLOUD_CHEAP,
    )
    assert report.cogs_allowance_nusd == 1 * USD
    assert report.dominant_cloud_component == "tts"
    assert report.cloud_component_share("tts") > 0.85
    assert 500 < report.affordable_turns < 900


def test_local_share_of_one_is_unbounded_not_zero():
    """`None` means unbounded. Conflating it with zero inverts the answer."""
    report = affordable_turns(
        book=PriceBook.local_only(),
        revenue_per_player_usd="10.00",
        local_share=1.0,
        cloud_tier=CostTier.LOCAL,
    )
    assert report.affordable_turns is None
    assert report.blended_cost_per_turn_nusd == 0


def test_required_local_share_answers_the_studios_question():
    book = PriceBook.standard()
    # Cheap enough that cloud alone clears the bar.
    assert (
        required_local_share(
            book=book, revenue_per_player_usd="10.00", expected_turns=100
        )
        == 0.0
    )
    # A chatty player forces work onto the device.
    heavy = required_local_share(
        book=book, revenue_per_player_usd="10.00", expected_turns=10_000
    )
    assert 0.0 < heavy < 1.0


def test_ceiling_from_revenue_floors_against_the_house():
    """Every other rounding favours the house; this one must not, or the
    stated margin could be breached."""
    assert ceiling_from_revenue("10.00", 0.90) == 1 * USD
    assert ceiling_from_revenue("9.99", 0.90) <= int(9.99 * USD * 0.1) + 1


def test_margin_target_must_be_a_fraction():
    with pytest.raises(BudgetMisuseError):
        ceiling_from_revenue("10.00", 1.0)
    with pytest.raises(BudgetMisuseError):
        ceiling_from_revenue("10.00", -0.1)


# -- provenance ----------------------------------------------------------


def test_expired_promotional_rate_is_flagged_but_never_alters_cost():
    """A shipped game whose promo lapsed must keep talking, not crash."""
    book = PriceBook.standard()
    stale = book.stale_tables(today=date(2027, 6, 1))
    assert stale, "the premium table carries a dated promotional STT rate"
    before = price_turn(
        TurnProfile.measured_npc().usage,
        book,
        TierPlan.uniform(CostTier.CLOUD_PREMIUM),
    )
    assert before.total_nusd > 0  # pricing is unaffected by staleness


def test_session_summary_is_json_serializable():
    """Telemetry has to survive the trip to a studio's analytics pipeline."""
    _, session = ledgers(policy(10**9, 10**9, 10**9))
    auth = session.authorize(
        TurnProfile.measured_npc().usage, TierPlan.local_tts(CostTier.CLOUD_CHEAP)
    )
    assert not isinstance(auth, BudgetDenied)
    session.settle(auth)
    summary = session.close()
    payload = json.dumps(summary.as_log_record())
    assert "shadow_cloud_nusd" in payload
    assert summary.shadow_cloud_nusd > summary.total_nusd, (
        "local TTS must show a saving against the all-cloud shadow"
    )


# -- regressions: each of these was a real hole the ledger did not plug ---


def test_session_policy_cannot_widen_the_players_lifetime_cap():
    """`authorize` once checked the SESSION's lifetime ceiling while summing
    the PLAYER's spend, so a per-NPC policy silently overrode the player cap."""
    player = PlayerLedger("p", policy(10, 100, 1_000))
    with pytest.raises(BudgetMisuseError):
        SessionLedger("s", player, policy(10, 100, 10**9), PriceBook.standard())


def test_lifetime_ceiling_is_the_players_not_the_sessions():
    book = PriceBook(
        {
            CostTier.CLOUD_CHEAP: PriceTable(
                "unit",
                LLMPrice(input_nusd_per_mtok=1_000_000, output_nusd_per_mtok=0),
                STTPrice.FREE,
                TTSPrice.FREE,
            )
        }
    )
    player = PlayerLedger("p", policy(50, 50, 50))
    session = SessionLedger("s", player, policy(50, 50, 50), book)
    for _ in range(200):
        auth = session.authorize(
            TurnUsage(input_tokens=10), TierPlan.uniform(CostTier.CLOUD_CHEAP)
        )
        if isinstance(auth, BudgetDenied):
            break
        session.settle(auth)
    assert player.lifetime_spent_nusd <= 50, "the player's ceiling must bind"


def test_authorization_cannot_be_settled_through_another_players_session():
    """auth_id comes from a per-player counter, so ids collide across players.

    Settling one player's authorization through another player's session used
    to drive the second ledger's reserved total negative and leak the first
    reservation forever.
    """
    pol = policy(10**9, 10**9, 10**9)
    book = PriceBook.standard()
    alice = PlayerLedger("alice", pol)
    bob = PlayerLedger("bob", pol)
    a_session = SessionLedger("a", alice, pol, book)
    b_session = SessionLedger("b", bob, pol, book)

    a_auth = a_session.authorize(
        TurnProfile.measured_npc().usage, TierPlan.uniform(CostTier.CLOUD_CHEAP)
    )
    assert not isinstance(a_auth, BudgetDenied)
    with pytest.raises(BudgetMisuseError):
        b_session.settle(a_auth)
    assert bob.worst_case_exposure_nusd >= 0, "reserved must never go negative"
    assert bob.lifetime_remaining_nusd <= pol.lifetime_ceiling_nusd


def test_fallback_tier_must_be_free():
    """A paid `degrade_to` made denial the most expensive path in the system:
    the caller plays the fallback exactly when the budget is gone."""
    with pytest.raises(BudgetMisuseError):
        BudgetPolicy(
            10,
            100,
            1000,
            allowed_tiers=frozenset({CostTier.CANNED, CostTier.CLOUD_CHEAP}),
            degrade_to=CostTier.CLOUD_CHEAP,
        )


def test_every_denial_fallback_is_actually_free():
    _, session = ledgers(policy(0, 0, 0))
    denial = session.authorize(
        TurnProfile.measured_npc().usage, TierPlan.uniform(CostTier.CLOUD_CHEAP)
    )
    assert isinstance(denial, BudgetDenied)
    assert denial.fallback.estimate.is_free


def test_closing_a_session_charges_work_still_in_flight():
    """Dropping an in-flight reservation under-counts real spend: the request
    left the machine and the vendor will bill for it."""
    player, session = ledgers(policy(10**9, 10**9, 10**9))
    auth = session.authorize(
        TurnProfile.measured_npc().usage, TierPlan.uniform(CostTier.CLOUD_CHEAP)
    )
    assert not isinstance(auth, BudgetDenied)
    summary = session.close()
    assert summary.leaked_authorizations == 1
    assert player.lifetime_spent_nusd == auth.reserved_nusd, "money must be recorded"
    assert player.worst_case_exposure_nusd == player.lifetime_spent_nusd


def test_close_is_idempotent_and_a_closed_session_refuses_work():
    _, session = ledgers(policy(10**9, 10**9, 10**9))
    first = session.close()
    second = session.close()
    assert first.total_nusd == second.total_nusd
    with pytest.raises(BudgetMisuseError):
        session.authorize(
            TurnProfile.measured_npc().usage, TierPlan.uniform(CostTier.CLOUD_CHEAP)
        )
    with pytest.raises(BudgetMisuseError):
        session.record_free(
            TurnProfile.measured_npc().usage, TierPlan.uniform(CostTier.LOCAL)
        )


def test_router_does_not_plan_local_tts_on_a_device_without_local():
    """Pricing TTS at zero on hardware that must pay for it understates the
    dominant cost component tenfold."""
    pol = policy(10**9, 10**9, 10**9)
    _, session = ledgers(pol)
    router = TierRouter(pol, PriceBook.standard(), local_available=False)
    decision = router.plan(
        session=session, estimate=TurnProfile.measured_npc().usage, dialogue_tier=3
    )
    assert decision.plan.tts is not CostTier.LOCAL
    assert decision.estimate.tts_nusd > 0


def test_authorization_exposes_a_cap_the_caller_can_enforce():
    """The ledger cannot bound a turn on its own — by settle() the tokens are
    already generated. It hands the caller the cap to pass to the provider."""
    pol = policy(2_000_000, 10**9, 10**9)
    _, session = ledgers(pol, PriceBook.standard())
    auth = session.authorize(
        TurnProfile.measured_npc().usage, TierPlan.uniform(CostTier.CLOUD_CHEAP)
    )
    assert not isinstance(auth, BudgetDenied)
    assert auth.max_settlement_nusd <= pol.per_turn_ceiling_nusd
    chars = auth.max_characters(PriceBook.standard())
    assert chars is not None and chars > 0
    capped = auth.estimate.usage.with_output(output_tokens=23, characters_out=chars)
    assert (
        price_turn(capped, PriceBook.standard(), auth.plan).total_nusd
        <= pol.per_turn_ceiling_nusd
    )


def test_local_tts_cap_is_unbounded_because_it_cannot_overspend():
    _, session = ledgers(policy(10**9, 10**9, 10**9), PriceBook.hybrid_local_tts())
    auth = session.authorize(
        TurnProfile.measured_npc().usage, TierPlan.local_tts(CostTier.CLOUD_CHEAP)
    )
    assert not isinstance(auth, BudgetDenied)
    assert auth.max_characters(PriceBook.hybrid_local_tts()) is None


def test_shadow_cloud_saving_is_reported_for_a_fully_local_game():
    """The one field that quantifies the local-inference saving must not read
    zero exactly where the saving is total."""
    pol = BudgetPolicy.local_only()
    player = PlayerLedger("p", pol)
    session = SessionLedger("s", player, pol, PriceBook.local_only())
    for _ in range(10):
        session.record_free(
            TurnProfile.measured_npc().usage, TierPlan.uniform(CostTier.LOCAL)
        )
    summary = session.close()
    assert summary.total_nusd == 0
    assert summary.shadow_cloud_nusd > 0
    assert summary.saved_vs_cloud_nusd == summary.shadow_cloud_nusd


def test_abandon_overrun_is_counted_too():
    _, session = ledgers(policy(10**9, 10**9, 10**9))
    small = TurnUsage(input_tokens=10, characters_out=1)
    auth = session.authorize(small, TierPlan.uniform(CostTier.CLOUD_CHEAP))
    assert not isinstance(auth, BudgetDenied)
    session.abandon(auth, TurnUsage(input_tokens=10, characters_out=100_000))
    assert session.close().overruns == 1
