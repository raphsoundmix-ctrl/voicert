"""Runtime cost as an enforced property, not a dashboard.

Every profile in this framework already declares a *latency* budget and the
pipeline holds it. This package gives a profile a **money** budget on the same
footing: stated up front, checked before spending, and degraded gracefully
rather than failed when it binds.

The shortest useful path::

    from voicert.economics import (
        BudgetPolicy, PlayerLedger, PriceBook, SessionLedger,
        TierPlan, TurnUsage,
    )

    # $10 game, 90% target margin -> $1.00 of AI cost per player, enforced.
    policy = BudgetPolicy.from_revenue("10.00", margin_target=0.90)
    book = PriceBook.hybrid_local_tts()      # cloud brain, on-device voice
    player = PlayerLedger("player-1", policy)
    session = SessionLedger("tavern", player, policy, book)

    auth = session.authorize(estimate, TierPlan.local_tts())
    if isinstance(auth, BudgetDenied):
        play(auth.fallback)                  # always affordable, never an error
    else:
        session.settle(auth, actual_usage)

Two findings from building it are worth stating plainly, because both are
counter-intuitive and both are load-bearing:

* **Speech synthesis, not the language model, is the bill.** On a cheap cloud
  mix a short NPC reply is ~90% TTS and under 2% LLM. Chasing a cheaper model
  optimizes a rounding error; moving TTS on-device is a ~10x cut, and a
  Piper-class voice is ~60 MB of CPU-only inference that runs on the low-VRAM,
  non-NVIDIA majority of the installed base where a local LLM will not.
* **Barge-in is a cost mechanism.** The framework already cancels generation
  the instant a player interrupts. :meth:`SessionLedger.abandon` is where that
  cancellation reaches the invoice — the prompt is owed, the unspoken
  remainder of the reply is not.
"""

from voicert.economics.ledger import (
    Authorization,
    BudgetDenied,
    BudgetExceeded,
    BudgetPolicy,
    CostObserver,
    DenialReason,
    PlayerLedger,
    SessionLedger,
    SessionSummary,
    TierDecision,
    TierRouter,
)
from voicert.economics.money import (
    USD_TO_NUSD,
    BudgetMisuseError,
    NanoUSD,
    format_usd,
    parse_usd,
    usd,
)
from voicert.economics.planning import (
    AffordabilityReport,
    TurnProfile,
    affordable_turns,
    ceiling_from_revenue,
    required_local_share,
)
from voicert.economics.prices import (
    CLOUD_CHEAP_PRICES,
    CLOUD_PREMIUM_PRICES,
    FREE_PRICES,
    CostTier,
    LLMPrice,
    PriceBook,
    PriceTable,
    STTPrice,
    TTSPrice,
)
from voicert.economics.usage import TierPlan, TurnCost, TurnUsage, price_turn, shadow_cost

__all__ = [
    "AffordabilityReport",
    "Authorization",
    "BudgetDenied",
    "BudgetExceeded",
    "BudgetMisuseError",
    "BudgetPolicy",
    "CLOUD_CHEAP_PRICES",
    "CLOUD_PREMIUM_PRICES",
    "CostObserver",
    "CostTier",
    "DenialReason",
    "FREE_PRICES",
    "LLMPrice",
    "NanoUSD",
    "PlayerLedger",
    "PriceBook",
    "PriceTable",
    "STTPrice",
    "SessionLedger",
    "SessionSummary",
    "TTSPrice",
    "TierDecision",
    "TierPlan",
    "TierRouter",
    "TurnCost",
    "TurnProfile",
    "TurnUsage",
    "USD_TO_NUSD",
    "affordable_turns",
    "ceiling_from_revenue",
    "format_usd",
    "parse_usd",
    "price_turn",
    "required_local_share",
    "shadow_cost",
    "usd",
]
