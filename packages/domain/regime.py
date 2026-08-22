"""Tail-aware regime measurement, for sizing that respects fat upside.

ADR-0022 found that a 3-sigma band on trailing realized volatility badly
underestimates crypto upside: DOT moved +151% and XRP +211% against bands near
28%. Both are ordinary outcomes for a distribution with fat tails and a
catastrophe for a parametric band that assumes otherwise.

This module measures the tail **empirically** instead of assuming a shape.

* :func:`worst_drawup` asks the only question a short perp cares about: over any
  window of the intended holding length in the history available so far, what is
  the largest rise from the window's opening price to the highest price reached
  inside it? No distribution is fitted and no sigma is multiplied.
* :func:`extension_above_trailing` asks whether the symbol is *already* moving.
  It was written on the guess that the ADR-0022 liquidations opened into a rally
  already underway. **That guess was wrong** — measured at those entries, DOT was
  +0.1% and XRP -1.1% against their trailing medians, which is as flat as a
  market gets (ADR-0023). It is kept as a filter against entering an established
  move, not as a defence of the liquidation invariant.

Both take a prefix of history and nothing else, so a caller cannot leak the
future into them by accident. Everything is ``Decimal``.

**On overfitting.** Two liquidations in one replay is not a sample. These
measures are defined on what a short perp is structurally exposed to — upside
over the holding horizon, and entering a move already underway — rather than
tuned until those two trades disappear. As it turned out neither made them
disappear, which is the more useful result: the tail that kills was three to five
times anything in the prior history, and arrived from a flat market (ADR-0023).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from domain.errors import DomainError


class RegimeError(DomainError):
    """History too short to measure a regime. Always fails closed."""


@dataclass(frozen=True, slots=True)
class PricePoint:
    """The two prices a drawup needs: where a window opens and how high it got."""

    close: Decimal
    high: Decimal


def worst_drawup(
    points: Sequence[PricePoint],
    *,
    horizon_periods: int,
    minimum_windows: int = 30,
) -> Decimal:
    """Largest ``(max high in window) / (window's opening close) - 1`` observed.

    This is the move that would have killed a short opened at the worst moment
    in the history provided. Computed with a sliding-window maximum, so the cost
    is linear in the series rather than quadratic — a 17,520-bar history at a
    720-hour horizon is otherwise 12 million comparisons per symbol.

    Fails closed when there are too few complete windows to have seen anything.
    """
    if horizon_periods < 1:
        raise RegimeError("horizon_periods must be at least 1")
    total = len(points)
    windows = total - horizon_periods
    if windows < minimum_windows:
        raise RegimeError(
            f"need at least {minimum_windows} complete windows, have {max(0, windows)}"
        )

    # Monotonic deque of indices whose highs are candidates for the window max.
    candidates: deque[int] = deque()
    worst = Decimal(0)
    for index in range(total):
        while candidates and points[candidates[-1]].high <= points[index].high:
            candidates.pop()
        candidates.append(index)

        start = index - horizon_periods
        if start < 0:
            continue
        while candidates and candidates[0] <= start:
            candidates.popleft()
        opening = points[start].close
        if opening <= 0:
            continue
        peak = points[candidates[0]].high if candidates else points[start].high
        drawup = (peak - opening) / opening
        if drawup > worst:
            worst = drawup
    return worst


def extension_above_trailing(
    closes: Sequence[Decimal],
    *,
    lookback_periods: int,
    minimum_observations: int = 30,
) -> Decimal:
    """How far the latest close sits above its trailing median, as a fraction.

    The median rather than the mean, because a mean over a period that already
    contains the rally is dragged toward it and understates the extension —
    exactly when the measure matters most.

    Negative when the symbol is below its trailing median. Shorts are not
    endangered by that, so callers compare against a positive threshold.
    """
    if lookback_periods < 2:
        raise RegimeError("lookback_periods must be at least 2")
    usable = [value for value in closes[-lookback_periods:] if value > 0]
    if len(usable) < minimum_observations:
        raise RegimeError(f"need at least {minimum_observations} closes, have {len(usable)}")
    ordered = sorted(usable)
    middle = len(ordered) // 2
    median = (
        ordered[middle]
        if len(ordered) % 2 == 1
        else (ordered[middle - 1] + ordered[middle]) / Decimal(2)
    )
    if median <= 0:
        raise RegimeError("trailing median must be positive")
    return (usable[-1] - median) / median
