"""Domain value objects for observed market facts.

Every one of these is a *point-in-time observation*: it carries the venue's own
timestamp for when the fact was true, separately from when we collected it. Both
are needed — the venue timestamp is what a point-in-time backtest may join on,
and the collection timestamp is what freshness gates measure (ADR-0007).

All prices and quantities are :class:`~decimal.Decimal`, parsed from the strings
Binance sends, never through float (ADR-0011).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from domain.instrument import InstrumentRef


class MarketDataSource(StrEnum):
    """Where an immutable observation came from."""

    ARCHIVE = "ARCHIVE"
    REST = "REST"


_KLINE_INTERVALS: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "3m": timedelta(minutes=3),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "6h": timedelta(hours=6),
    "8h": timedelta(hours=8),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
    "3d": timedelta(days=3),
    "1w": timedelta(weeks=1),
}


def kline_interval(value: str) -> timedelta:
    """Return an exact fixed-width interval or reject an unsupported calendar bar.

    Binance also exposes ``1M``/``1mo`` calendar-month candles. Their duration is
    not fixed, so treating them as a timedelta would make gap detection wrong.
    Phase 3 deliberately refuses them instead of approximating a month.
    """

    normalized = value.strip()
    try:
        return _KLINE_INTERVALS[normalized]
    except KeyError:
        raise ValueError(f"unsupported fixed-width kline interval {value!r}") from None


def _aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _finite(value: Decimal, field: str) -> None:
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")


@dataclass(frozen=True, slots=True)
class MarkPriceSnapshot:
    """`premiumIndex`: mark price, index price, and the *current* funding rate.

    ``last_funding_rate`` is the rate for the interval **in progress**, not the
    one last settled — a distinction that is easy to get backwards and produces a
    carry estimate one interval stale. The settled series comes from
    :class:`FundingRateObservation`.
    """

    instrument: InstrumentRef
    mark_price: Decimal
    index_price: Decimal
    last_funding_rate: Decimal
    interest_rate: Decimal
    next_funding_time: datetime
    venue_time: datetime
    collected_at: datetime
    estimated_settle_price: Decimal | None = None

    def __post_init__(self) -> None:
        if self.instrument.market != "usdm":
            raise ValueError("mark-price snapshots require a USD-M instrument")
        for field in ("mark_price", "index_price"):
            value = getattr(self, field)
            _finite(value, field)
            if value <= 0:
                raise ValueError(f"{field} must be positive")
        for field in ("last_funding_rate", "interest_rate"):
            _finite(getattr(self, field), field)
        if self.estimated_settle_price is not None:
            _finite(self.estimated_settle_price, "estimated_settle_price")
            if self.estimated_settle_price <= 0:
                raise ValueError("estimated_settle_price must be positive")
        _aware(self.next_funding_time, "next_funding_time")
        _aware(self.venue_time, "venue_time")
        _aware(self.collected_at, "collected_at")


@dataclass(frozen=True, slots=True)
class FundingRateObservation:
    """`fundingRate`: one settled funding payment.

    ``funding_time`` is **not** reliably on the interval boundary. The 2026-08-09
    capture contains ``2026-08-09T08:00:00.005Z`` — five milliseconds late. Any
    join that tests equality against a computed boundary will silently drop rows;
    join on an interval window instead (ADR-0015).
    """

    instrument: InstrumentRef
    funding_time: datetime
    funding_rate: Decimal
    collected_at: datetime
    mark_price: Decimal | None = None
    #: Binance sends "Regular" here. The field was undocumented in our founding
    #: assumptions and is retained verbatim rather than dropped, because a value
    #: other than "Regular" would mean this payment is not an ordinary settlement.
    rate_type: str | None = None
    #: Present in the bulk archive, absent from the REST history response. It is
    #: historical evidence and must not be replaced with today's schedule.
    interval_hours: int | None = None

    def __post_init__(self) -> None:
        if self.instrument.market != "usdm":
            raise ValueError("funding observations require a USD-M instrument")
        _finite(self.funding_rate, "funding_rate")
        if self.mark_price is not None:
            _finite(self.mark_price, "mark_price")
            if self.mark_price <= 0:
                raise ValueError("mark_price must be positive")
        if self.interval_hours is not None and (
            self.interval_hours <= 0 or 24 % self.interval_hours != 0
        ):
            raise ValueError("historical funding interval must divide 24 hours")
        if self.rate_type is not None and not self.rate_type.strip():
            raise ValueError("rate_type cannot be blank")
        _aware(self.funding_time, "funding_time")
        _aware(self.collected_at, "collected_at")


@dataclass(frozen=True, slots=True)
class BookTicker:
    """Best bid/ask.

    ``venue_time`` and ``last_update_id`` are optional because **spot and futures
    disagree**: the USDⓈ-M response carries both, the spot response carries
    neither, under the same endpoint name. Requiring them would have made the
    spot leg unparseable (ADR-0015).
    """

    instrument: InstrumentRef
    bid_price: Decimal
    bid_qty: Decimal
    ask_price: Decimal
    ask_qty: Decimal
    collected_at: datetime
    venue_time: datetime | None = None
    last_update_id: int | None = None

    def __post_init__(self) -> None:
        for field in ("bid_price", "bid_qty", "ask_price", "ask_qty"):
            value = getattr(self, field)
            _finite(value, field)
            if value < 0:
                raise ValueError(f"{field} cannot be negative")
        if self.bid_price <= 0 or self.ask_price <= 0:
            raise ValueError("book prices must be positive")
        _aware(self.collected_at, "collected_at")
        if self.venue_time is not None:
            _aware(self.venue_time, "venue_time")
        if self.last_update_id is not None and self.last_update_id < 0:
            raise ValueError("last_update_id cannot be negative")

    @property
    def spread(self) -> Decimal:
        return self.ask_price - self.bid_price

    @property
    def mid(self) -> Decimal:
        return (self.ask_price + self.bid_price) / 2

    @property
    def is_crossed(self) -> bool:
        """A crossed or locked book is not a book to price against; fail closed."""
        return self.bid_price >= self.ask_price


@dataclass(frozen=True, slots=True)
class Kline:
    """One OHLCV candle.

    Binance sends klines as positional arrays, not objects, so the mapping layer
    is the only place that knows what index 7 means. ``is_closed`` matters for
    point-in-time correctness: the final element of a `klines` response is the
    candle still forming, and using it as if it were settled is look-ahead.
    """

    instrument: InstrumentRef
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trades: int
    collected_at: datetime
    is_closed: bool = True

    def __post_init__(self) -> None:
        _aware(self.open_time, "open_time")
        _aware(self.close_time, "close_time")
        _aware(self.collected_at, "collected_at")
        if self.close_time <= self.open_time:
            raise ValueError("kline close_time must be after open_time")
        for field in ("open", "high", "low", "close"):
            value = getattr(self, field)
            _finite(value, field)
            if value <= 0:
                raise ValueError(f"kline {field} must be positive")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("kline high is below an OHLC value")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("kline low is above an OHLC value")
        for field in ("volume", "quote_volume"):
            value = getattr(self, field)
            _finite(value, field)
            if value < 0:
                raise ValueError(f"kline {field} cannot be negative")
        if self.trades < 0:
            raise ValueError("kline trades cannot be negative")
