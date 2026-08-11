"""Pure market-data invariants used by persistence and point-in-time replay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from domain.instrument import InstrumentRef, VenueEnvironment, VenueScope
from domain.market_data import BookTicker, FundingRateObservation, Kline, kline_interval

NOW = datetime(2026, 7, 1, tzinfo=UTC)


def instrument(market: str = "usdm") -> InstrumentRef:
    return InstrumentRef(
        scope=VenueScope(venue="BINANCE", environment=VenueEnvironment.PRODUCTION),
        symbol="BTCUSDT",
        market=market,
    )


def test_fixed_width_kline_intervals_are_explicit() -> None:
    assert kline_interval("1h") == timedelta(hours=1)
    assert kline_interval("1w") == timedelta(weeks=1)
    with pytest.raises(ValueError, match="unsupported fixed-width"):
        kline_interval("1M")


def test_historical_funding_interval_must_divide_a_day() -> None:
    with pytest.raises(ValueError, match="divide 24"):
        FundingRateObservation(
            instrument=instrument(),
            funding_time=NOW,
            funding_rate=Decimal("0.0001"),
            collected_at=NOW,
            interval_hours=5,
        )


def test_crossed_or_locked_books_are_detected() -> None:
    locked = BookTicker(
        instrument=instrument("spot"),
        bid_price=Decimal("100"),
        bid_qty=Decimal("1"),
        ask_price=Decimal("100"),
        ask_qty=Decimal("1"),
        collected_at=NOW,
    )
    assert locked.is_crossed


def test_impossible_ohlc_is_refused() -> None:
    with pytest.raises(ValueError, match="high is below"):
        Kline(
            instrument=instrument(),
            open_time=NOW,
            close_time=NOW + timedelta(hours=1) - timedelta(milliseconds=1),
            open=Decimal("100"),
            high=Decimal("99"),
            low=Decimal("90"),
            close=Decimal("95"),
            volume=Decimal("1"),
            quote_volume=Decimal("95"),
            trades=1,
            collected_at=NOW + timedelta(hours=2),
        )


def test_market_data_timestamps_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FundingRateObservation(
            instrument=instrument(),
            funding_time=datetime(2026, 7, 1),
            funding_rate=Decimal("0.0001"),
            collected_at=NOW,
        )
