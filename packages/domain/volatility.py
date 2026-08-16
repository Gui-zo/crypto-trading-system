"""Realized volatility from closed candles, for the risk engine's stress band.

This is the one place in the codebase where ``float`` appears in a calculation,
and the exception is deliberate and bounded. Volatility needs logarithms and a
square root, which ``Decimal`` does not provide exactly; pretending otherwise
would mean a home-grown series expansion nobody can check. The rule this bends —
``Decimal`` everywhere — exists to protect *money*: prices, quantities,
thresholds, anything that must round the way the venue rounds. A volatility
estimate is none of those. It is a statistical input whose fourth decimal place
carries no economic meaning.

The boundary is explicit: floats live inside :func:`realized_volatility` and
never escape it. The result is quantized to ``Decimal`` before return, and
:class:`domain.risk.RiskLimits` floors it regardless — ADR-0009 requires an
absolute floor precisely because a vol model that underestimates is what a
stress band exists to survive.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import Decimal
from itertools import pairwise

from domain.errors import DomainError

#: Volatility is reported to this many places. Beyond it the estimate is noise.
_QUANTUM = Decimal("0.000001")

#: Fewer returns than this cannot support an estimate worth acting on.
DEFAULT_MINIMUM_OBSERVATIONS = 30


class VolatilityError(DomainError):
    """Not enough usable history to estimate volatility. Always fails closed."""


def realized_volatility(
    closes: Sequence[Decimal],
    *,
    periods_ahead: int,
    minimum_observations: int = DEFAULT_MINIMUM_OBSERVATIONS,
) -> Decimal:
    """Close-to-close volatility over ``periods_ahead`` periods, as a fraction.

    ``closes`` are consecutive closed candles at a single interval, oldest first.
    ``periods_ahead`` scales the per-period estimate to the intended holding
    horizon by the square root of time — 24 for a one-day horizon on 1h candles.

    Fails closed on thin history rather than returning a small number, because a
    small number here becomes a large position.
    """
    if periods_ahead < 1:
        raise VolatilityError("periods_ahead must be at least 1")
    if minimum_observations < 2:
        raise VolatilityError("minimum_observations must be at least 2")

    usable = [value for value in closes if value > 0]
    if len(usable) - 1 < minimum_observations:
        raise VolatilityError(
            f"need at least {minimum_observations} returns, have {max(0, len(usable) - 1)}"
        )

    # --- float boundary opens ---
    returns = [math.log(float(later) / float(earlier)) for earlier, later in pairwise(usable)]
    mean = sum(returns) / len(returns)
    # Sample variance: the estimate is of a population we only sampled.
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    scaled = math.sqrt(variance * periods_ahead)
    # --- float boundary closes ---

    if not math.isfinite(scaled) or scaled < 0:
        raise VolatilityError("volatility estimate is not a finite non-negative number")
    return Decimal(repr(scaled)).quantize(_QUANTUM)


def mean_funding_rate(rates: Sequence[Decimal], *, minimum_observations: int = 10) -> Decimal:
    """Trailing mean settled funding rate, exact in ``Decimal``.

    A placeholder for the champion model's forecast, and labelled as one. The
    Phase-4 model predicts ``P(funding >= threshold)`` — a probability, not a
    rate — and turning one into the other is a modelling decision that has not
    been made or tested. Until it is, the scan uses what actually settled and
    says so, rather than inventing a bridge.
    """
    if minimum_observations < 1:
        raise VolatilityError("minimum_observations must be positive")
    if len(rates) < minimum_observations:
        raise VolatilityError(
            f"need at least {minimum_observations} settlements, have {len(rates)}"
        )
    return sum(rates, Decimal(0)) / Decimal(len(rates))
