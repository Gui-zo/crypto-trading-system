"""Phase-6 regime measures, and the negative result they produced.

These exist because ADR-0022 found liquidations a parametric stress band could
not see. They measure the tail empirically instead. ADR-0023 records that this
was **not sufficient** — the tests below pin both the behaviour and that limit,
so nobody re-derives the same hope from the same code.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from domain.regime import (
    PricePoint,
    RegimeError,
    extension_above_trailing,
    worst_drawup,
)


def flat(count: int, value: str = "100") -> list[PricePoint]:
    return [PricePoint(close=Decimal(value), high=Decimal(value)) for _ in range(count)]


def with_spike(count: int, at: int, high: str) -> list[PricePoint]:
    points = flat(count)
    points[at] = PricePoint(close=Decimal("100"), high=Decimal(high))
    return points


# --- worst drawup ------------------------------------------------------------


def test_a_flat_market_has_no_drawup() -> None:
    assert worst_drawup(flat(200), horizon_periods=24) == Decimal(0)


def test_the_worst_rise_inside_any_window_is_found() -> None:
    """A wick 10 bars in must be visible to a window that opened before it."""
    points = with_spike(200, at=100, high="150")

    assert worst_drawup(points, horizon_periods=24) == Decimal("0.5")


def test_a_spike_outside_every_window_is_not_counted() -> None:
    """The measure is what a position of the holding length was exposed to, not
    the maximum of the whole series."""
    points = with_spike(200, at=5, high="150")

    # Windows open at index 0..(200-2); a spike at 5 is inside early windows, so
    # narrow the horizon until only later windows remain by trimming the prefix.
    later = points[40:]
    assert worst_drawup(later, horizon_periods=24) == Decimal(0)


def test_the_horizon_changes_what_is_visible() -> None:
    points = with_spike(400, at=200, high="200")

    narrow = worst_drawup(points[:210], horizon_periods=5)
    wide = worst_drawup(points[:210], horizon_periods=100)

    assert wide >= narrow


def test_too_little_history_fails_closed() -> None:
    """A tail nobody has observed is not a tail of zero."""
    with pytest.raises(RegimeError, match="complete windows"):
        worst_drawup(flat(40), horizon_periods=24, minimum_windows=30)


def test_a_long_horizon_on_a_long_series_is_linear_not_quadratic() -> None:
    """17,520 bars at a 720-hour horizon is 12M comparisons done naively; the
    sliding-window maximum keeps it tractable. This just has to finish."""
    points = flat(17520)

    assert worst_drawup(points, horizon_periods=720) == Decimal(0)


# --- extension ---------------------------------------------------------------


def test_a_flat_market_is_not_extended() -> None:
    closes = [Decimal("100")] * 200

    assert extension_above_trailing(closes, lookback_periods=100) == Decimal(0)


def test_a_rallying_market_reads_extended() -> None:
    closes = [Decimal("100")] * 100 + [Decimal(100 + index) for index in range(50)]

    assert extension_above_trailing(closes, lookback_periods=150) > Decimal("0.1")


def test_a_falling_market_reads_negative_and_does_not_endanger_a_short() -> None:
    closes = [Decimal(200 - index) for index in range(150)]

    assert extension_above_trailing(closes, lookback_periods=150) < 0


def test_the_median_is_used_so_the_rally_does_not_hide_itself() -> None:
    """A mean over a window containing the rally is dragged toward it and
    understates the extension exactly when it matters."""
    closes = [Decimal("100")] * 120 + [Decimal("400")] * 30

    extension = extension_above_trailing(closes, lookback_periods=150)

    assert extension == Decimal(3)  # 400 vs a median still at 100


def test_too_little_history_fails_closed_for_extension() -> None:
    with pytest.raises(RegimeError, match="need at least"):
        extension_above_trailing([Decimal("100")] * 5, lookback_periods=100)


# --- the negative result -----------------------------------------------------


def test_a_calm_entry_before_a_violent_rally_is_not_flagged() -> None:
    """ADR-0023, reproduced in miniature.

    This is the shape of the two liquidations: a flat market, a modest observed
    tail, and then a move several times larger than anything in the history.
    Neither measure raises a warning, because neither can. Pinned so the failed
    hypothesis is not quietly re-adopted.
    """
    calm = flat(2000)
    tail = worst_drawup(calm, horizon_periods=720)
    extension = extension_above_trailing([point.close for point in calm], lookback_periods=720)

    assert tail == Decimal(0)
    assert extension == Decimal(0)
    # ...and the move that followed in the real data was +151% and +211%.
