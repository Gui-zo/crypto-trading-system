"""Instrument identity and funding-schedule tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from domain.instrument import (
    ContractType,
    FundingSchedule,
    InstrumentRef,
    InstrumentStatus,
    VenueEnvironment,
    VenueScope,
)
from domain.market_data import BookTicker

PRODUCTION = VenueScope(venue="BINANCE", environment=VenueEnvironment.PRODUCTION)
TESTNET = VenueScope(venue="BINANCE", environment=VenueEnvironment.TESTNET)
NOW = datetime(2026, 8, 9, 16, 0, tzinfo=UTC)


def test_the_same_symbol_in_two_environments_is_two_identities() -> None:
    """The corruption ADR-0010 exists to prevent, as an equality assertion."""
    production = InstrumentRef(scope=PRODUCTION, symbol="BTCUSDT", market="usdm")
    testnet = InstrumentRef(scope=TESTNET, symbol="BTCUSDT", market="usdm")
    assert production != testnet
    assert production.key != testnet.key


def test_the_same_symbol_on_spot_and_futures_is_two_identities() -> None:
    """The two legs of a carry share a name and are not the same instrument."""
    spot = InstrumentRef(scope=PRODUCTION, symbol="BTCUSDT", market="spot")
    futures = InstrumentRef(scope=PRODUCTION, symbol="BTCUSDT", market="usdm")
    assert spot != futures


def test_the_key_is_fully_qualified() -> None:
    ref = InstrumentRef(scope=PRODUCTION, symbol="BTCUSDT", market="usdm")
    assert ref.key == "BINANCE:production:usdm:BTCUSDT"


def test_symbols_and_venues_are_normalised() -> None:
    ref = InstrumentRef(
        scope=VenueScope(venue=" binance ", environment=VenueEnvironment.PRODUCTION),
        symbol=" btcusdt ",
        market="usdm",
    )
    assert ref.symbol == "BTCUSDT"
    assert ref.scope.venue == "BINANCE"


@pytest.mark.parametrize("market", ["futures", "coinm", "", "USDM"])
def test_an_unknown_market_is_refused(market: str) -> None:
    with pytest.raises(ValueError, match="unknown market"):
        InstrumentRef(scope=PRODUCTION, symbol="BTCUSDT", market=market)


def test_a_blank_symbol_or_venue_is_refused() -> None:
    with pytest.raises(ValueError, match="symbol cannot be empty"):
        InstrumentRef(scope=PRODUCTION, symbol="  ", market="usdm")
    with pytest.raises(ValueError, match="venue code cannot be empty"):
        VenueScope(venue=" ", environment=VenueEnvironment.PRODUCTION)


def test_only_trading_is_tradeable() -> None:
    assert InstrumentStatus.TRADING.is_tradeable
    for status in (
        InstrumentStatus.SETTLING,
        InstrumentStatus.PENDING_TRADING,
        InstrumentStatus.UNKNOWN,
    ):
        assert not status.is_tradeable


def test_the_contract_type_vocabulary_includes_tokenised_equities() -> None:
    assert ContractType.TRADIFI_PERPETUAL in set(ContractType)


# ---------------------------------------------------------------------------
# Funding schedule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("hours", "per_day"), [(1, 24), (4, 6), (8, 3), (12, 2), (24, 1)])
def test_settlements_per_day_follows_the_interval(hours: int, per_day: int) -> None:
    assert FundingSchedule(interval_hours=hours).settlements_per_day == per_day


def test_the_interval_is_exposed_as_a_timedelta() -> None:
    assert FundingSchedule(interval_hours=4).interval == timedelta(hours=4)


def test_a_four_hour_symbol_accrues_twice_as_often_as_an_eight_hour_one() -> None:
    """442 of 742 symbols were 4-hourly on 2026-08-09; this is the common case."""
    four = FundingSchedule(interval_hours=4)
    eight = FundingSchedule(interval_hours=8)
    assert four.settlements_per_day == 2 * eight.settlements_per_day


def test_caps_are_carried_per_symbol() -> None:
    schedule = FundingSchedule(
        interval_hours=8, rate_cap=Decimal("0.00300"), rate_floor=Decimal("-0.00300")
    )
    assert schedule.rate_cap == Decimal("0.00300")


@pytest.mark.parametrize("hours", [0, -1])
def test_a_nonpositive_interval_is_refused(hours: int) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        FundingSchedule(interval_hours=hours)


@pytest.mark.parametrize("hours", [5, 7, 9])
def test_an_interval_that_does_not_divide_the_day_is_refused(hours: int) -> None:
    """Settlements would drift across UTC days, which every join assumes cannot happen."""
    with pytest.raises(ValueError, match="does not divide"):
        FundingSchedule(interval_hours=hours)


# ---------------------------------------------------------------------------
# Book ticker arithmetic
# ---------------------------------------------------------------------------


def ticker(bid: str, ask: str) -> BookTicker:
    return BookTicker(
        instrument=InstrumentRef(scope=PRODUCTION, symbol="BTCUSDT", market="usdm"),
        bid_price=Decimal(bid),
        bid_qty=Decimal("1"),
        ask_price=Decimal(ask),
        ask_qty=Decimal("1"),
        collected_at=NOW,
    )


def test_spread_and_mid_are_exact_decimals() -> None:
    book = ticker("65100.60", "65100.70")
    assert book.spread == Decimal("0.10")
    assert book.mid == Decimal("65100.65")


def test_a_normal_book_is_not_crossed() -> None:
    assert not ticker("65100.60", "65100.70").is_crossed


@pytest.mark.parametrize(("bid", "ask"), [("100", "100"), ("101", "100")])
def test_a_locked_or_crossed_book_is_flagged(bid: str, ask: str) -> None:
    """Not a book to price against; the gates fail closed on it."""
    assert ticker(bid, ask).is_crossed
