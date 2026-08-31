"""Money — one integer unit, and the single place a dollar figure becomes one.

The module's product is the sentence *"this player will never cost me more
than $X"*. That is an invariant over a running sum, so the representation is
not a style question:

* **Not float.** Float addition is not associative, so ``spent + a + b`` can
  differ in the last bits from ``spent + b + a``. Two players who made the
  same turns in a different order would get different answers from the
  affordability check. An invariant you cannot state order-independently is
  not an invariant.
* **Not Decimal.** Correct, but the wrong shape here. Its rounding context is
  *thread-local*, and this ledger is touched from the asyncio loop and from
  the engine bridge thread — two rounding behaviours in one ledger is exactly
  the bug this module cannot have. It is also ~50-100x slower per operation
  and invites silent ``Decimal * float`` mixing that mypy does not reject.
* **Nano, not micro.** At ``gpt-5-nano`` cached-input rates a single token
  costs $0.000000005. In micro-dollars that truncates to zero and a
  10,000-turn session bills $0.00. Nano keeps the cheapest priced unit on the
  current market representable as a positive integer.

Rounding is **ceiling**, always toward the house. That biases each charge by
at most one nano-dollar and makes the ledger a provable *upper* bound on real
spend. Rounding to nearest would make it an estimate, and an estimate cannot
be enforced.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Final, TypeAlias

#: Integer nano-dollars. Aliased so an ``int`` in a signature is never
#: ambiguous between "tokens", "milliseconds" and "money".
NanoUSD: TypeAlias = int

USD_TO_NUSD: Final[int] = 1_000_000_000

#: Rate cards are quoted per million units (tokens, characters). Storing the
#: per-million figure keeps a vendor's own number exact: AssemblyAI's $0.15/hr
#: is 150_000_000 nUSD/hr with no remainder, where a per-second field would
#: have to hold 41_666.67 and drift.
PER_MILLION: Final[int] = 1_000_000


class BudgetMisuseError(Exception):
    """Programmer error. Never raised because a player ran out of money.

    Kept structurally separate from :class:`BudgetExceeded` so a caller can
    catch "I am out of budget" without also swallowing "I passed a negative
    token count".
    """


def parse_usd(amount: str | int | float | Decimal, *, per: int = 1) -> NanoUSD:
    """Convert a human-readable dollar figure to nano-dollars.

    The one boundary where a float is allowed to exist. Conversion goes
    through ``Decimal(str(amount))`` so ``0.1`` becomes exactly ``0.1`` and
    not ``0.1000000000000000055``.

    ``per`` divides the amount — ``parse_usd("0.05", per=1000)`` reads a
    $/1k-characters rate card entry into a per-character figure without the
    caller doing float division first.
    """
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError) as exc:
        raise BudgetMisuseError(f"not a monetary amount: {amount!r}") from exc
    if not value.is_finite():
        raise BudgetMisuseError(f"monetary amount must be finite, got {amount!r}")
    if value < 0:
        raise BudgetMisuseError(f"monetary amount cannot be negative, got {amount!r}")
    if per < 1:
        raise BudgetMisuseError(f"`per` must be at least 1, got {per}")
    nanos = (value * USD_TO_NUSD) / per
    # Quantize toward the house. A price that lands between two nano-dollars
    # bills the higher one, keeping every downstream total an upper bound.
    return int(nanos.to_integral_value(rounding="ROUND_CEILING"))


def usd(nanos: NanoUSD) -> Decimal:
    """Nano-dollars back to exact dollars, for display and reports only.

    Returns ``Decimal`` rather than ``float`` so a printed total is the total.
    The result must never be fed back into ledger arithmetic.
    """
    return Decimal(nanos) / USD_TO_NUSD


def format_usd(nanos: NanoUSD, places: int = 6) -> str:
    """Human-facing dollar string. Reports only, never a computation input."""
    return f"${usd(nanos):.{places}f}"


def ceil_div(numerator: int, denominator: int) -> int:
    """Integer ceiling division, for pricing a partial unit.

    Used per cost component rather than on the summed total: rounding once at
    the end would let three sub-nano components each round to zero and price a
    real turn at nothing.
    """
    if denominator <= 0:
        raise BudgetMisuseError("denominator must be positive")
    return -((-numerator) // denominator)
