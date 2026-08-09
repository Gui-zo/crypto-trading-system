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
from datetime import datetime
from decimal import Decimal

from domain.instrument import InstrumentRef


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
