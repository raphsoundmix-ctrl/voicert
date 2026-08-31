"""Enforcement — reserve, spend, settle, and degrade rather than fail.

A budget you can only *observe* is a dashboard. This module makes it a
guarantee, on the same footing as the latency budget every profile already
declares in :mod:`voicert.metrics`.

Three ideas carry the design.

**Denial is a value, not an exception.** Running out of budget is a normal
operating state that a game must handle gracefully, so :meth:`authorize`
*returns* a :class:`BudgetDenied` carrying an affordable ``fallback``. Only
programmer error raises (:class:`~voicert.economics.money.BudgetMisuseError`).
A player must never see an error dialog because a studio hit a spending cap;
they should see an NPC that answers from its pre-authored lines instead. The
type system enforces the distinction: you cannot use an ``Authorization`` you
did not check for, because the denial is a different type.

**Reserve then settle, not check then spend.** Two NPCs talking to the same
player can each pass an affordability check and then jointly overspend. The
check and the hold must be one atomic step, so :meth:`authorize` reserves an
estimate and :meth:`settle` records the truth. With ``reserve_headroom`` on,
the ceiling is a hard bound including work already in flight.

**Settle always tells the truth.** If the real cost overran the estimate,
:meth:`settle` records the overrun and does not raise. Refusing to record
money that was genuinely spent would corrupt the lifetime figure permanently;
the correct response is to deny the *next* turn, which it does.

Barge-in has a dedicated path. When a player cuts an NPC off mid-sentence,
the prompt has already been sent and is already owed, but the unspoken
remainder of the reply is not — :meth:`abandon` charges the input side and
only the output that was actually produced. That makes the framework's
existing interruption machinery a cost mechanism as well as a latency one.
"""

from __future__ import annotations

import itertools
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from voicert.economics.money import BudgetMisuseError, NanoUSD, format_usd, parse_usd
from voicert.economics.prices import CLOUD_CHEAP_PRICES, CostTier, PriceBook
from voicert.economics.usage import TierPlan, TurnCost, TurnUsage, price_turn

logger = logging.getLogger("voicert.economics")

Horizon = Literal["turn", "session", "lifetime", "policy"]


class DenialReason(StrEnum):
    """Machine-readable cause, for telemetry and for picking a narrative response."""

    PER_TURN_CEILING = "per_turn_ceiling"
    SESSION_CEILING = "session_ceiling"
    LIFETIME_CEILING = "lifetime_ceiling"
    TIER_UNAVAILABLE = "tier_unavailable"
    POLICY_DISABLED = "policy_disabled"


class BudgetExceeded(Exception):
    """Opt-in raising, for batch tooling and tests that *want* an exception.

    Deliberately not on the default path and deliberately not a subclass of
    ``BudgetMisuseError`` — running out of money is not a bug.
    """

    def __init__(self, denial: "BudgetDenied") -> None:
        super().__init__(denial.message)
        self.denial = denial


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    """Three ceilings and the routing knobs.

    Frozen: a policy that can be mutated mid-session makes the guarantee
    unstateable.

    The horizons fail differently and deserve different responses.
    ``per_turn`` tripping means one turn is pathologically large — almost
    always a runaway context — and is a developer bug. ``lifetime`` tripping
    means the player has simply had their money's worth, which is a design
    question, not an error.
    """

    per_turn_ceiling_nusd: NanoUSD
    per_session_ceiling_nusd: NanoUSD
    lifetime_ceiling_nusd: NanoUSD
    allowed_tiers: frozenset[CostTier] = frozenset({CostTier.CANNED, CostTier.LOCAL})
    #: Count in-flight reservations against the ceiling. On (the default) the
    #: stated ceiling is a hard bound; off, it can be exceeded by at most one
    #: turn, which some studios prefer to a mid-conversation downgrade.
    reserve_headroom: bool = True
    #: Where a denied turn lands. CANNED is free, so it is always affordable.
    degrade_to: CostTier = CostTier.CANNED
    #: NPC priority (0..1) at or above which a premium tier may be used.
    premium_priority_threshold: float = 0.7

    def __post_init__(self) -> None:
        for name in (
            "per_turn_ceiling_nusd",
            "per_session_ceiling_nusd",
            "lifetime_ceiling_nusd",
        ):
            if getattr(self, name) < 0:
                raise BudgetMisuseError(f"BudgetPolicy.{name} cannot be negative")
        if not (
            self.per_turn_ceiling_nusd
            <= self.per_session_ceiling_nusd
            <= self.lifetime_ceiling_nusd
        ):
            raise BudgetMisuseError(
                "ceilings must be ordered per_turn <= per_session <= lifetime; "
                f"got {self.per_turn_ceiling_nusd} / {self.per_session_ceiling_nusd} "
                f"/ {self.lifetime_ceiling_nusd}. An unordered set means the "
                "narrower ceiling can never bind, which is silently no ceiling."
            )
        if not 0.0 <= self.premium_priority_threshold <= 1.0:
            raise BudgetMisuseError("premium_priority_threshold must be within [0, 1]")
        if self.degrade_to > CostTier.LOCAL:
            # A paid fallback makes denial the most expensive path in the
            # system: the caller plays `denial.fallback` precisely when the
            # budget is gone, and nothing prices it against what is left.
            raise BudgetMisuseError(
                f"degrade_to={self.degrade_to.name} is a paid tier; a fallback "
                "must be free (CANNED or LOCAL) or exhausting the budget would "
                "start billing instead of stopping"
            )

    @classmethod
    def local_only(cls) -> "BudgetPolicy":
        """On-device only. Ceilings are zero because nothing can be billed."""
        return cls(0, 0, 0, allowed_tiers=frozenset({CostTier.CANNED, CostTier.LOCAL}))

    @classmethod
    def from_revenue(
        cls,
        revenue_per_player_usd: str | int | float,
        *,
        margin_target: float = 0.90,
        sessions: int = 20,
        allowed_tiers: frozenset[CostTier] | None = None,
        **kwargs: object,
    ) -> "BudgetPolicy":
        """Derive enforceable ceilings from a price point and a margin target.

        The bridge from a business decision to an engineering invariant: at
        $10 revenue and 90% target margin, the player's lifetime AI cost of
        goods may not exceed $1.00, and the framework will hold that.
        """
        if not 0.0 <= margin_target < 1.0:
            raise BudgetMisuseError("margin_target must be within [0, 1)")
        if sessions < 1:
            raise BudgetMisuseError("sessions must be at least 1")
        # Decimal for the same reason as ceiling_from_revenue: the float form
        # loses a nano-dollar off every ceiling.
        share = Decimal(1) - Decimal(str(margin_target))
        lifetime = int(parse_usd(revenue_per_player_usd) * share)
        per_session = lifetime // sessions
        # One turn may never cost more than a few percent of a whole session;
        # a turn that large is a runaway context, not a conversation.
        per_turn = max(1, per_session // 20) if per_session else 0
        return cls(
            per_turn_ceiling_nusd=per_turn,
            per_session_ceiling_nusd=per_session,
            lifetime_ceiling_nusd=lifetime,
            allowed_tiers=allowed_tiers
            or frozenset({CostTier.CANNED, CostTier.LOCAL, CostTier.CLOUD_CHEAP}),
            **kwargs,  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class TierDecision:
    """What the router chose, and what it was asked for.

    The gap between ``requested`` and ``chosen`` is the degradation signal —
    the number a studio watches to know whether its budget is actually biting.
    """

    plan: TierPlan
    chosen: CostTier
    requested: CostTier
    estimate: TurnCost
    reason: DenialReason | None = None

    @property
    def degraded(self) -> bool:
        return self.chosen < self.requested


@dataclass(frozen=True, slots=True)
class BudgetDenied:
    """"Denied — degrade gracefully." A return value, never raised by default.

    ``fallback`` is never ``None`` and is itself never denied: in the limit it
    is ``CANNED``, which costs zero and is therefore always affordable. That
    totality is what lets a caller handle a denial without a branch that ends
    in an error dialog.
    """

    reason: DenialReason
    horizon: Horizon
    requested_nusd: NanoUSD
    remaining_nusd: NanoUSD
    fallback: TierDecision
    message: str

    def as_log_record(self) -> dict[str, object]:
        return {
            "event": "budget_denied",
            "reason": str(self.reason),
            "horizon": self.horizon,
            "requested_nusd": self.requested_nusd,
            "remaining_nusd": self.remaining_nusd,
            "fallback_tier": self.fallback.chosen.name,
        }


@dataclass(frozen=True, slots=True)
class Authorization:
    """Proof that budget was atomically reserved before spending.

    Single-use. Settling one twice would double-bill the player and corrupt
    the lifetime figure, so it raises ``BudgetMisuseError`` rather than
    silently accumulating.
    """

    auth_id: int
    session_id: str
    player_id: str
    reserved_nusd: NanoUSD
    plan: TierPlan
    estimate: TurnCost
    #: The most this turn may cost and still leave every ceiling intact.
    #: The ledger cannot enforce this by itself — by the time `settle` runs,
    #: the provider has already generated the tokens and the money is spent.
    #: Convert it with :meth:`max_output_tokens` / :meth:`max_characters` and
    #: pass the result to the provider, or the guarantee degrades from "never
    #: more than $X" to "never more than $X plus one turn's overrun".
    max_settlement_nusd: NanoUSD = 0

    def max_output_tokens(self, book: PriceBook) -> int | None:
        """Output-token cap implied by ``max_settlement_nusd``.

        ``None`` when output is free on this plan (an on-device tier), which
        is the only case where an uncapped generation cannot overspend.
        """
        rate = book[self.plan.llm].llm.output_nusd_per_mtok
        if rate <= 0:
            return None
        headroom = max(0, self.max_settlement_nusd - self._input_side_nusd(book))
        return int(headroom * 1_000_000 // rate)

    def max_characters(self, book: PriceBook) -> int | None:
        """Synthesized-character cap implied by ``max_settlement_nusd``.

        This is the one that matters: TTS is ~90% of a cloud voice turn.
        """
        rate = book[self.plan.tts].tts.nusd_per_million_chars
        if rate <= 0:
            return None
        headroom = max(0, self.max_settlement_nusd - self._input_side_nusd(book))
        return int(headroom * 1_000_000 // rate)

    def _input_side_nusd(self, book: PriceBook) -> NanoUSD:
        """Cost already committed when the request left the machine."""
        spent = self.estimate.usage.with_output(output_tokens=0, characters_out=0)
        return price_turn(spent, book, self.plan).total_nusd


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """What one conversation cost — and what it would have cost fully cloud."""

    session_id: str
    player_id: str
    total_nusd: NanoUSD
    breakdown_nusd: Mapping[str, NanoUSD]
    turns: int
    turns_by_tier: Mapping[CostTier, int]
    denials: int
    overruns: int
    leaked_authorizations: int
    shadow_cloud_nusd: NanoUSD

    @property
    def saved_vs_cloud_nusd(self) -> NanoUSD:
        return max(0, self.shadow_cloud_nusd - self.total_nusd)

    def as_log_record(self) -> dict[str, object]:
        return {
            "event": "session_cost",
            "session_id": self.session_id,
            "total_nusd": self.total_nusd,
            "turns": self.turns,
            "denials": self.denials,
            "overruns": self.overruns,
            "leaked_authorizations": self.leaked_authorizations,
            "shadow_cloud_nusd": self.shadow_cloud_nusd,
            "saved_vs_cloud_nusd": self.saved_vs_cloud_nusd,
        }


@runtime_checkable
class CostObserver(Protocol):
    """Telemetry hook. Zero-dependency, so it plugs into any analytics stack."""

    def on_settled(self, cost: TurnCost, session_id: str, player_id: str) -> None: ...

    def on_denied(
        self, denial: BudgetDenied, session_id: str, player_id: str
    ) -> None: ...

    def on_session_closed(self, summary: SessionSummary) -> None: ...


class PlayerLedger:
    """The lifetime horizon, and the answer to "where does the money live?".

    Per player, with no global registry. A module-level singleton would leak
    one player's spend into another's ceiling in a multiplayer server and
    would contaminate tests across cases. The player owns the lock because the
    player is who the money is about: a lifetime ceiling spanning several
    concurrent sessions has to serialize somewhere, and any lower level would
    leave a race between sessions.
    """

    def __init__(self, player_id: str, policy: BudgetPolicy) -> None:
        self.player_id = player_id
        self.policy = policy
        self._lock = threading.RLock()
        self._spent: NanoUSD = 0
        self._reserved: NanoUSD = 0
        self._turns_settled = 0
        self._auth_ids = itertools.count(1)

    @property
    def lifetime_spent_nusd(self) -> NanoUSD:
        with self._lock:
            return self._spent

    @property
    def lifetime_remaining_nusd(self) -> NanoUSD:
        with self._lock:
            return max(0, self.policy.lifetime_ceiling_nusd - self._committed_locked())

    @property
    def worst_case_exposure_nusd(self) -> NanoUSD:
        """Spent plus everything currently in flight. The number that must
        never exceed the ceiling for the guarantee to hold."""
        with self._lock:
            return self._spent + self._reserved

    @property
    def turns_settled(self) -> int:
        with self._lock:
            return self._turns_settled

    def _committed_locked(self) -> NanoUSD:
        return self._spent + (self._reserved if self.policy.reserve_headroom else 0)


class SessionLedger:
    """One conversation. Owns per-turn and per-session enforcement.

    Delegates the lifetime horizon upward to the :class:`PlayerLedger`, and
    takes that ledger's lock for every mutation so the two horizons can never
    disagree.
    """

    def __init__(
        self,
        session_id: str,
        player: PlayerLedger,
        policy: BudgetPolicy,
        book: PriceBook,
        *,
        observer: CostObserver | None = None,
    ) -> None:
        if policy.lifetime_ceiling_nusd > player.policy.lifetime_ceiling_nusd:
            raise BudgetMisuseError(
                f"session policy lifetime ceiling ({policy.lifetime_ceiling_nusd}) "
                f"exceeds the player's ({player.policy.lifetime_ceiling_nusd}); a "
                "session may tighten a player's budget, never widen it"
            )
        if policy.reserve_headroom != player.policy.reserve_headroom:
            # The two ledgers each consult their own flag when summing
            # commitments; disagreeing makes the composite ceiling incoherent.
            raise BudgetMisuseError(
                "session and player policies must agree on reserve_headroom"
            )
        self.session_id = session_id
        self.player = player
        self.policy = policy
        self.book = book
        self.observer = observer
        self._spent: NanoUSD = 0
        self._reserved: NanoUSD = 0
        self._breakdown: dict[str, NanoUSD] = {"llm": 0, "stt": 0, "tts": 0}
        self._turns_by_tier: dict[CostTier, int] = {}
        self._open: dict[int, Authorization] = {}
        self._turns = 0
        self._denials = 0
        self._overruns = 0
        self._shadow_cloud: NanoUSD = 0
        self._leaked = 0
        self._closed = False

    # -- read-only views -------------------------------------------------

    @property
    def spent_nusd(self) -> NanoUSD:
        return self._spent

    @property
    def remaining_nusd(self) -> NanoUSD:
        """Whichever of session or lifetime is tighter, read at one instant.

        Advisory, like :meth:`can_afford` — by the time a caller acts on it
        another NPC may have reserved. Only :meth:`authorize` is
        authoritative, and it re-derives both horizons atomically.
        """
        with self.player._lock:
            return self._remaining_locked()

    def _committed_locked(self) -> NanoUSD:
        return self._spent + (self._reserved if self.policy.reserve_headroom else 0)

    def _remaining_locked(self) -> NanoUSD:
        """Both horizons read from one instant. Caller must hold the lock."""
        session_left = max(
            0, self.policy.per_session_ceiling_nusd - self._committed_locked()
        )
        lifetime_left = max(
            0,
            self.player.policy.lifetime_ceiling_nusd - self.player._committed_locked(),
        )
        return min(session_left, lifetime_left)

    def can_afford(self, cost: TurnCost) -> bool:
        """Advisory and racy by design — used to *rank* options, never to
        commit to one. Only :meth:`authorize` is authoritative."""
        return (
            cost.total_nusd <= self.policy.per_turn_ceiling_nusd
            and cost.total_nusd <= self.remaining_nusd
        )

    # -- the money path --------------------------------------------------

    def authorize(
        self, usage: TurnUsage, plan: TierPlan
    ) -> Authorization | BudgetDenied:
        """Atomically check every horizon and reserve the estimate.

        Returns an :class:`Authorization` or a :class:`BudgetDenied`. Never
        raises for lack of funds.

        The denial is built inside the lock but *reported* outside it, so a
        studio's analytics callback cannot stall every other NPC on the same
        player.
        """
        estimate = price_turn(usage, self.book, plan)
        denial: BudgetDenied | None = None

        with self.player._lock:
            # Checked under the lock: a close() interleaved with an unlocked
            # read would leave a reservation in a closed session that nothing
            # will ever reclaim.
            if self._closed:
                raise BudgetMisuseError(f"session {self.session_id} is closed")

            if plan.headline not in self.policy.allowed_tiers:
                denial = self._deny(
                    DenialReason.TIER_UNAVAILABLE,
                    "policy",
                    estimate,
                    usage,
                    self._remaining_locked(),
                )
            elif estimate.total_nusd > self.policy.per_turn_ceiling_nusd:
                denial = self._deny(
                    DenialReason.PER_TURN_CEILING,
                    "turn",
                    estimate,
                    usage,
                    self.policy.per_turn_ceiling_nusd,
                )
            else:
                session_left = max(
                    0, self.policy.per_session_ceiling_nusd - self._committed_locked()
                )
                # The player's own ceiling, not the session's. A per-NPC or
                # per-quest policy may tighten a session, never widen the
                # player's lifetime cap out from under it.
                lifetime_left = max(
                    0,
                    self.player.policy.lifetime_ceiling_nusd
                    - self.player._committed_locked(),
                )
                if estimate.total_nusd > session_left:
                    denial = self._deny(
                        DenialReason.SESSION_CEILING,
                        "session",
                        estimate,
                        usage,
                        session_left,
                    )
                elif estimate.total_nusd > lifetime_left:
                    denial = self._deny(
                        DenialReason.LIFETIME_CEILING,
                        "lifetime",
                        estimate,
                        usage,
                        lifetime_left,
                    )
                else:
                    auth = Authorization(
                        auth_id=next(self.player._auth_ids),
                        session_id=self.session_id,
                        player_id=self.player.player_id,
                        reserved_nusd=estimate.total_nusd,
                        plan=plan,
                        estimate=estimate,
                        max_settlement_nusd=min(
                            self.policy.per_turn_ceiling_nusd,
                            session_left,
                            lifetime_left,
                        ),
                    )
                    self._reserved += auth.reserved_nusd
                    self.player._reserved += auth.reserved_nusd
                    self._open[auth.auth_id] = auth
                    return auth

        return self._notify_denied(denial)

    def authorize_or_raise(self, usage: TurnUsage, plan: TierPlan) -> Authorization:
        """For batch tooling and tests that genuinely want an exception."""
        result = self.authorize(usage, plan)
        if isinstance(result, BudgetDenied):
            raise BudgetExceeded(result)
        return result

    def settle(self, auth: Authorization, actual: TurnUsage | None = None) -> TurnCost:
        """Record what the turn really cost. Releases the reservation.

        Never raises for an overrun. Money that was genuinely spent must be
        recorded or the lifetime figure is a lie; the response to an overrun
        is to deny the *next* turn, which the ceilings then do.
        """
        cost = self._close_authorization(
            auth, actual if actual is not None else auth.estimate.usage, auth.plan
        )
        self._notify_settled(cost)
        return cost

    def abandon(
        self, auth: Authorization, partial: TurnUsage | None = None
    ) -> TurnCost:
        """Settle a turn the player cut short.

        The prompt and the STT audio left the machine the moment the request
        was made and are owed regardless. Only the unspoken remainder of the
        reply is saved. ``partial=None`` charges the input side and nothing
        else — the correct bill for a turn interrupted before its first token.

        This is what makes barge-in a cost mechanism: the framework already
        cancels generation the instant the player speaks, and this is where
        that cancellation shows up on the invoice.
        """
        spent_usage = (
            partial
            if partial is not None
            else auth.estimate.usage.with_output(output_tokens=0, characters_out=0)
        )
        cost = self._close_authorization(auth, spent_usage, auth.plan)
        self._notify_settled(cost)
        return cost

    def record_free(self, usage: TurnUsage, plan: TierPlan) -> TurnCost:
        """Record an on-device turn without the reserve/settle ceremony.

        Free turns cannot exhaust a budget, so making callers authorize them
        would be pure overhead on the hot path — and would put a lock in front
        of the very configuration this framework recommends.
        """
        if self._closed:
            raise BudgetMisuseError(f"session {self.session_id} is closed")
        cost = price_turn(usage, self.book, plan)
        if not cost.is_free:
            raise BudgetMisuseError(
                f"record_free got a turn costing {format_usd(cost.total_nusd)}; "
                "use authorize()/settle() for anything billable"
            )
        self._account(cost)
        return cost

    def _close_authorization(
        self, auth: Authorization, usage: TurnUsage, plan: TierPlan
    ) -> TurnCost:
        if auth.session_id != self.session_id or auth.player_id != self.player.player_id:
            # auth_id comes from a PER-PLAYER counter, so ids collide across
            # players. Without this check, settling player A's authorization
            # through player B's session drives B's `_reserved` negative and
            # leaks A's reservation forever.
            raise BudgetMisuseError(
                f"authorization {auth.auth_id} belongs to session "
                f"{auth.session_id!r}/player {auth.player_id!r}, not to "
                f"{self.session_id!r}/{self.player.player_id!r}"
            )
        with self.player._lock:
            if self._open.pop(auth.auth_id, None) is None:
                raise BudgetMisuseError(
                    f"authorization {auth.auth_id} is unknown to session "
                    f"{self.session_id} — already settled or abandoned"
                )
            cost = price_turn(usage, self.book, plan)
            self._reserved -= auth.reserved_nusd
            self.player._reserved -= auth.reserved_nusd
            self._spent += cost.total_nusd
            self.player._spent += cost.total_nusd
            self.player._turns_settled += 1
            self._record_locked(cost)
            # Both settle() and abandon() route through here, so an overrun on
            # the barge-in path is counted too.
            overran = cost.total_nusd > auth.reserved_nusd
            if overran:
                self._overruns += 1
        if overran:
            logger.warning(
                "turn overran its reservation: %s > %s (session %s)",
                format_usd(cost.total_nusd),
                format_usd(auth.reserved_nusd),
                self.session_id,
            )
        return cost

    def _account(self, cost: TurnCost) -> None:
        with self.player._lock:
            self._spent += cost.total_nusd
            self.player._spent += cost.total_nusd
            self._record_locked(cost)

    def _record_locked(self, cost: TurnCost) -> None:
        self._turns += 1
        self._breakdown["llm"] += cost.llm_nusd
        self._breakdown["stt"] += cost.stt_nusd
        self._breakdown["tts"] += cost.tts_nusd
        tier = cost.plan.headline
        self._turns_by_tier[tier] = self._turns_by_tier.get(tier, 0) + 1
        # What the same usage would have cost entirely in the cloud. Computing
        # it here is what turns the framework's own logs into the evidence.
        # Falls back to the bundled cheap rates when the session's own book
        # has no cloud tier: the shadow is a counterfactual, and a fully
        # on-device game is exactly where the saving is largest.
        shadow_book = (
            self.book
            if self.book.has(CostTier.CLOUD_CHEAP)
            else PriceBook({CostTier.CLOUD_CHEAP: CLOUD_CHEAP_PRICES})
        )
        self._shadow_cloud += price_turn(
            cost.usage, shadow_book, TierPlan.uniform(CostTier.CLOUD_CHEAP)
        ).total_nusd

    def _notify_denied(self, denial: BudgetDenied) -> BudgetDenied:
        """Fire the observer OUTSIDE the lock and return the denial unchanged.

        `_deny` itself is called from inside the player lock on three of the
        four denial paths, so notifying there would run a studio's analytics
        callback while holding the lock every other NPC is waiting on.
        """
        observer = self.observer
        if observer is not None:
            self._safe_notify(
                lambda: observer.on_denied(denial, self.session_id, self.player.player_id)
            )
        return denial

    def _deny(
        self,
        reason: DenialReason,
        horizon: Horizon,
        estimate: TurnCost,
        usage: TurnUsage,
        remaining: NanoUSD,
    ) -> BudgetDenied:
        self._denials += 1
        fallback_plan = TierPlan.uniform(self.policy.degrade_to)
        fallback = TierDecision(
            plan=fallback_plan,
            chosen=self.policy.degrade_to,
            requested=estimate.plan.headline,
            estimate=price_turn(usage, self.book, fallback_plan),
            reason=reason,
        )
        denial = BudgetDenied(
            reason=reason,
            horizon=horizon,
            requested_nusd=estimate.total_nusd,
            remaining_nusd=remaining,
            fallback=fallback,
            message=(
                f"{reason} on the {horizon} horizon: turn needs "
                f"{format_usd(estimate.total_nusd)}, {format_usd(remaining)} left. "
                f"Falling back to {self.policy.degrade_to.name}."
            ),
        )
        return denial

    def _notify_settled(self, cost: TurnCost) -> None:
        observer = self.observer
        if observer is not None:
            self._safe_notify(
                lambda: observer.on_settled(cost, self.session_id, self.player.player_id)
            )

    @staticmethod
    def _safe_notify(call: Callable[[], None]) -> None:
        """Observers run outside the lock and may not break the ledger.

        A studio's analytics callback throwing must not corrupt accounting or
        deadlock a conversation, so it is logged and swallowed.
        """
        try:
            call()
        except Exception:
            logger.exception("cost observer raised; ignoring")

    def close(self) -> SessionSummary:
        """Settle anything still in flight and summarize. Idempotent.

        In-flight authorizations are settled at their **reserved** amount
        rather than dropped. Dropping them would under-count real spend: the
        request left the machine, the vendor will bill for it, and a lifetime
        figure that omits it is as broken as one that over-counts. The
        reserved amount is a documented conservative stand-in for an actual
        cost this session will never learn.

        A non-zero ``leaked_authorizations`` is still a caller bug -- a turn
        that never settled or abandoned -- and should fail CI, but it must not
        crash a shipped game.
        """
        with self.player._lock:
            if self._closed:
                return self._summary_locked(self._leaked)
            leaked = len(self._open)
            self._leaked = leaked
            for auth in list(self._open.values()):
                self._reserved -= auth.reserved_nusd
                self.player._reserved -= auth.reserved_nusd
                self._spent += auth.reserved_nusd
                self.player._spent += auth.reserved_nusd
            self._open.clear()
            self._closed = True
            summary = self._summary_locked(leaked)
        if leaked:
            logger.warning(
                "session %s closed with %d unsettled authorization(s); charged "
                "at their reserved amount",
                self.session_id,
                leaked,
            )
        observer = self.observer
        if observer is not None:
            self._safe_notify(lambda: observer.on_session_closed(summary))
        return summary

    def _summary_locked(self, leaked: int) -> SessionSummary:
        return SessionSummary(
            session_id=self.session_id,
            player_id=self.player.player_id,
            total_nusd=self._spent,
            breakdown_nusd=dict(self._breakdown),
            turns=self._turns,
            turns_by_tier=dict(self._turns_by_tier),
            denials=self._denials,
            overruns=self._overruns,
            leaked_authorizations=leaked,
            shadow_cloud_nusd=self._shadow_cloud,
        )


class TierRouter:
    """Picks a tier per turn from remaining budget and NPC importance.

    This is where the game layer's existing ``DialogueTier`` meets money. The
    ladder is deterministic and therefore testable:

    1. An NPC the player cannot properly hear (BARK/CROWD/OFF) is ``CANNED``
       immediately — those tiers play pre-baked assets by construction and
       must never reach a paid provider. This is the rule that keeps cost
       proportional to what the player is actually listening to rather than
       to how many NPCs the world contains.
    2. Premium is offered only to NPCs at or above the priority threshold —
       quest-critical characters, not ambient vendors.
    3. Each candidate is priced and the most capable affordable one wins.
    4. ``CANNED`` is free, so the ladder always terminates.
    """

    def __init__(
        self,
        policy: BudgetPolicy,
        book: PriceBook,
        *,
        local_available: bool = True,
    ) -> None:
        self.policy = policy
        self.book = book
        self.local_available = local_available

    def plan(
        self,
        *,
        session: SessionLedger,
        estimate: TurnUsage,
        dialogue_tier: int = 3,
        priority: float = 0.0,
        prefer_local_tts: bool = True,
    ) -> TierDecision:
        """Choose a tier. Always returns a decision; never denies."""
        requested = self._requested_tier(priority)

        if dialogue_tier < 3:  # anything the player is not in earshot of
            canned = TierPlan.uniform(CostTier.CANNED)
            return TierDecision(
                plan=canned,
                chosen=CostTier.CANNED,
                requested=requested,
                estimate=price_turn(estimate, self.book, canned),
                reason=None if requested == CostTier.CANNED else DenialReason.POLICY_DISABLED,
            )

        candidates = self._candidates()
        for tier in sorted(candidates, reverse=True):
            if tier > requested:
                continue
            # local_available=False means this device cannot run on-device
            # inference at all. Planning a local TTS anyway would price the
            # dominant cost component at zero on hardware that must pay for it.
            use_local_tts = (
                prefer_local_tts
                and self.local_available
                and CostTier.LOCAL in candidates
                and tier >= CostTier.CLOUD_CHEAP
            )
            candidate = (
                TierPlan.local_tts(tier) if use_local_tts else TierPlan.uniform(tier)
            )
            cost = price_turn(estimate, self.book, candidate)
            if cost.is_free or session.can_afford(cost):
                return TierDecision(
                    plan=candidate,
                    chosen=tier,
                    requested=requested,
                    estimate=cost,
                    reason=None if tier == requested else DenialReason.SESSION_CEILING,
                )

        canned = TierPlan.uniform(CostTier.CANNED)
        return TierDecision(
            plan=canned,
            chosen=CostTier.CANNED,
            requested=requested,
            estimate=price_turn(estimate, self.book, canned),
            reason=DenialReason.SESSION_CEILING,
        )

    def _requested_tier(self, priority: float) -> CostTier:
        allowed = self._candidates()
        if (
            priority >= self.policy.premium_priority_threshold
            and CostTier.CLOUD_PREMIUM in allowed
        ):
            return CostTier.CLOUD_PREMIUM
        return max(t for t in allowed if t <= CostTier.CLOUD_CHEAP)

    def _candidates(self) -> frozenset[CostTier]:
        allowed = set(self.policy.allowed_tiers) & set(self.book.tiers())
        allowed.add(CostTier.CANNED)  # always available, always free
        if not self.local_available:
            allowed.discard(CostTier.LOCAL)
        return frozenset(allowed)
