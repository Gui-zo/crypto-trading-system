"""Wire to domain — the only place Binance field names are interpreted.

Everything that could be wrong about our reading of the API is concentrated here
and in `schemas`, so correcting it after a wire change is a localized edit rather
than a hunt (ADR-0003).

Three rules hold throughout:

* **Missing required data raises rather than defaults.** A mark price of ``0``
  because the field was absent is worse than an exception: it flows into sizing.
* **Numbers go through `parse_decimal`.** No floats, ever (ADR-0011).
* **Timestamps become timezone-aware UTC.** Binance sends epoch milliseconds;
  a naive datetime here would silently compare wrong against a UTC-aware one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from domain.instrument import (
    ContractType,
    FundingSchedule,
    InstrumentRef,
    InstrumentStatus,
)
from domain.market_data import BookTicker, FundingRateObservation, Kline, MarkPriceSnapshot
from domain.precision import PrecisionError, parse_decimal
from venue_binance.schemas import (
    BookTickerStreamWire,
    BookTickerWire,
    FundingInfoWire,
    FundingRateWire,
    PremiumIndexWire,
    SymbolWire,
)


class MappingError(ValueError):
    """A payload parsed as JSON but cannot be read as the fact it claims to be."""


def _require(value: str | None, field: str, symbol: str) -> Decimal:
    if value is None:
        raise MappingError(f"{symbol}: required numeric field {field!r} is absent")
    try:
        return parse_decimal(value)
    except PrecisionError as exc:
        raise MappingError(f"{symbol}: field {field!r} is not a valid decimal: {value!r}") from exc


def _optional(value: str | None, field: str, symbol: str) -> Decimal | None:
    return None if value is None else _require(value, field, symbol)


def to_utc(epoch_millis: int | None, field: str, symbol: str) -> datetime:
    if epoch_millis is None:
        raise MappingError(f"{symbol}: required timestamp {field!r} is absent")
    return datetime.fromtimestamp(epoch_millis / 1000, tz=UTC)


def to_utc_optional(epoch_millis: int | None) -> datetime | None:
    return None if epoch_millis is None else datetime.fromtimestamp(epoch_millis / 1000, tz=UTC)


def to_mark_price(
    wire: PremiumIndexWire, *, instrument: InstrumentRef, collected_at: datetime
) -> MarkPriceSnapshot:
    return MarkPriceSnapshot(
        instrument=instrument,
        mark_price=_require(wire.markPrice, "markPrice", wire.symbol),
        index_price=_require(wire.indexPrice, "indexPrice", wire.symbol),
        last_funding_rate=_require(wire.lastFundingRate, "lastFundingRate", wire.symbol),
        interest_rate=_require(wire.interestRate, "interestRate", wire.symbol),
        next_funding_time=to_utc(wire.nextFundingTime, "nextFundingTime", wire.symbol),
        venue_time=to_utc(wire.time, "time", wire.symbol),
        collected_at=collected_at,
        estimated_settle_price=_optional(
            wire.estimatedSettlePrice, "estimatedSettlePrice", wire.symbol
        ),
    )


def to_funding_rate(
    wire: FundingRateWire, *, instrument: InstrumentRef, collected_at: datetime
) -> FundingRateObservation:
    return FundingRateObservation(
        instrument=instrument,
        funding_time=to_utc(wire.fundingTime, "fundingTime", wire.symbol),
        funding_rate=_require(wire.fundingRate, "fundingRate", wire.symbol),
        collected_at=collected_at,
        mark_price=_optional(wire.markPrice, "markPrice", wire.symbol),
        rate_type=wire.rateType,
    )


def to_funding_schedule(wire: FundingInfoWire) -> FundingSchedule:
    if wire.fundingIntervalHours is None:
        raise MappingError(f"{wire.symbol}: fundingIntervalHours is absent")
    return FundingSchedule(
        interval_hours=wire.fundingIntervalHours,
        rate_cap=_optional(wire.adjustedFundingRateCap, "adjustedFundingRateCap", wire.symbol),
        rate_floor=_optional(
            wire.adjustedFundingRateFloor, "adjustedFundingRateFloor", wire.symbol
        ),
    )


def to_book_ticker(
    wire: BookTickerWire, *, instrument: InstrumentRef, collected_at: datetime
) -> BookTicker:
    return BookTicker(
        instrument=instrument,
        bid_price=_require(wire.bidPrice, "bidPrice", wire.symbol),
        bid_qty=_require(wire.bidQty, "bidQty", wire.symbol),
        ask_price=_require(wire.askPrice, "askPrice", wire.symbol),
        ask_qty=_require(wire.askQty, "askQty", wire.symbol),
        collected_at=collected_at,
        venue_time=to_utc_optional(wire.time),
        last_update_id=wire.lastUpdateId,
    )


def stream_to_book_ticker(
    wire: BookTickerStreamWire, *, instrument: InstrumentRef, collected_at: datetime
) -> BookTicker:
    """Map the WebSocket book ticker, whose field names share nothing with REST."""
    symbol = wire.s or instrument.symbol
    return BookTicker(
        instrument=instrument,
        bid_price=_require(wire.b, "b", symbol),
        bid_qty=_require(wire.B, "B", symbol),
        ask_price=_require(wire.a, "a", symbol),
        ask_qty=_require(wire.A, "A", symbol),
        collected_at=collected_at,
        venue_time=to_utc_optional(wire.T),
        last_update_id=wire.u,
    )


#: Positional layout of a kline array. Binance sends candles as arrays, so this
#: tuple is the schema — there is no field name to be tolerant about, and an
#: off-by-one here silently swaps close for volume.
_KLINE_OPEN_TIME = 0
_KLINE_OPEN = 1
_KLINE_HIGH = 2
_KLINE_LOW = 3
_KLINE_CLOSE = 4
_KLINE_VOLUME = 5
_KLINE_CLOSE_TIME = 6
_KLINE_QUOTE_VOLUME = 7
_KLINE_TRADES = 8
_KLINE_MIN_FIELDS = 9


def to_kline(
    row: list[object],
    *,
    instrument: InstrumentRef,
    collected_at: datetime,
    is_closed: bool = True,
) -> Kline:
    if len(row) < _KLINE_MIN_FIELDS:
        raise MappingError(
            f"{instrument.symbol}: kline row has {len(row)} fields, expected at least "
            f"{_KLINE_MIN_FIELDS}"
        )

    def number(index: int, name: str) -> Decimal:
        value = row[index]
        if not isinstance(value, str | int):
            raise MappingError(f"{instrument.symbol}: kline field {name} is {type(value).__name__}")
        return _require(str(value), name, instrument.symbol)

    def millis(index: int, name: str) -> datetime:
        value = row[index]
        if not isinstance(value, int):
            raise MappingError(f"{instrument.symbol}: kline field {name} is not an integer")
        return to_utc(value, name, instrument.symbol)

    trades = row[_KLINE_TRADES]
    return Kline(
        instrument=instrument,
        open_time=millis(_KLINE_OPEN_TIME, "openTime"),
        close_time=millis(_KLINE_CLOSE_TIME, "closeTime"),
        open=number(_KLINE_OPEN, "open"),
        high=number(_KLINE_HIGH, "high"),
        low=number(_KLINE_LOW, "low"),
        close=number(_KLINE_CLOSE, "close"),
        volume=number(_KLINE_VOLUME, "volume"),
        quote_volume=number(_KLINE_QUOTE_VOLUME, "quoteVolume"),
        trades=int(trades) if isinstance(trades, int) else 0,
        collected_at=collected_at,
        is_closed=is_closed,
    )


def to_instrument_status(raw: str | None) -> InstrumentStatus:
    """Map a symbol status, defaulting **unknown values to non-tradeable**.

    Fail-closed: a status Binance introduces later must not be treated as
    tradeable just because our enum does not recognise it.
    """
    if raw is None:
        return InstrumentStatus.UNKNOWN
    try:
        return InstrumentStatus(raw.strip().upper())
    except ValueError:
        return InstrumentStatus.UNKNOWN


def to_contract_type(raw: str | None) -> ContractType:
    if raw is None:
        return ContractType.UNKNOWN
    try:
        return ContractType(raw.strip().upper())
    except ValueError:
        return ContractType.UNKNOWN


def is_carry_candidate(wire: SymbolWire) -> bool:
    """Whether a symbol belongs in the funding-carry universe at all.

    Three independent exclusions, each of which the 2026-08-09 capture proved is
    load-bearing (ADR-0016):

    * status must be ``TRADING`` — 128 of 854 symbols were not;
    * contract type must be ``PERPETUAL`` — ``TRADIFI_PERPETUAL`` covers 153
      tokenised equities and metals (AAPLUSDT, TSLAUSDT, XAUUSDT) that are
      indistinguishable by shape and have entirely different risk;
    * quote asset must be USDT, so the spot hedge leg exists in the same unit.
    """
    return (
        to_instrument_status(wire.status).is_tradeable
        and to_contract_type(wire.contractType) is ContractType.PERPETUAL
        and (wire.quoteAsset or "").upper() == "USDT"
    )
