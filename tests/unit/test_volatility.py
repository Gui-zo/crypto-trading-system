"""Phase-5 volatility and the trailing funding placeholder."""

from __future__ import annotations

from decimal import Decimal

import pytest

from domain.volatility import (
    VolatilityError,
    mean_funding_rate,
    realized_volatility,
)


def flat(count: int, value: str = "100") -> list[Decimal]:
    return [Decimal(value)] * count


def alternating(count: int) -> list[Decimal]:
    return [Decimal("100") if index % 2 == 0 else Decimal("101") for index in range(count)]


def test_a_flat_series_has_no_volatility() -> None:
    assert realized_volatility(flat(60), periods_ahead=1) == Decimal("0")


def test_volatility_scales_with_the_square_root_of_time() -> None:
    """A 24-period horizon should be about sqrt(24) times the one-period figure."""
    one = realized_volatility(alternating(200), periods_ahead=1)
    day = realized_volatility(alternating(200), periods_ahead=24)

    ratio = day / one
    assert Decimal("4.85") < ratio < Decimal("4.92")  # sqrt(24) = 4.899


def test_thin_history_fails_closed_rather_than_returning_a_small_number() -> None:
    """A small volatility here becomes a large position, so refusing is the
    only safe answer."""
    with pytest.raises(VolatilityError, match="need at least"):
        realized_volatility(flat(10), periods_ahead=24)


def test_non_positive_closes_are_discarded_not_logged() -> None:
    """log(0) is undefined; a zero close is bad data, not a 100% drawdown."""
    closes = [*flat(60), Decimal("0")]

    assert realized_volatility(closes, periods_ahead=1) == Decimal("0")


def test_a_zero_horizon_is_refused() -> None:
    with pytest.raises(VolatilityError, match="at least 1"):
        realized_volatility(flat(60), periods_ahead=0)


def test_the_result_is_decimal_so_no_float_escapes_the_estimator() -> None:
    result = realized_volatility(alternating(100), periods_ahead=24)

    assert isinstance(result, Decimal)


def test_mean_funding_is_exact_in_decimal() -> None:
    rates = [Decimal("0.0001"), Decimal("0.0002"), Decimal("0.0003")]

    assert mean_funding_rate(rates, minimum_observations=3) == Decimal("0.0002")


def test_mean_funding_keeps_the_sign_of_a_paying_regime() -> None:
    """A negative mean means shorts pay longs, which must reach carry as a cost."""
    rates = [Decimal("-0.0002")] * 20

    assert mean_funding_rate(rates) < 0


def test_too_few_settlements_fails_closed() -> None:
    with pytest.raises(VolatilityError, match="need at least"):
        mean_funding_rate([Decimal("0.0001")], minimum_observations=10)
