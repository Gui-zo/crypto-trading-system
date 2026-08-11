"""Tolerant wire models for Binance responses (ADR-0003).

Every model here sets ``extra="allow"`` and makes all but the identifying fields
optional, so an added, removed, or renamed field never crashes ingestion. The raw
bytes are retained regardless, so a field we failed to model is recoverable by
replay rather than lost.

**Numbers stay strings.** Binance sends prices and quantities as JSON strings
precisely so they can be parsed exactly, and these models keep them that way;
conversion to :class:`~decimal.Decimal` happens in `mapping`, through
`domain.precision.parse_decimal`, which refuses floats outright (ADR-0011).
Typing a price as ``float`` here would discard the venue's care in the one place
it cannot be recovered.

Two shapes in here exist because of what the 2026-08-09 recording showed, not
because anyone predicted them:

* ``FundingInfoWire.updateTime`` is ``int | None`` — it is ``null`` for BTCUSDT
  and ETHUSDT and an integer for other symbols. A strict ``int`` crashes on the
  two symbols this project cares about most.
* ``BookTickerWire.time`` and ``lastUpdateId`` are optional, because the spot and
  USDⓈ-M responses to the *same* endpoint name carry different fields.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class _Wire(BaseModel):
    """Base for every wire model: tolerant of anything the venue adds."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class ServerTimeWire(_Wire):
    serverTime: int | None = None


class RateLimitWire(_Wire):
    rateLimitType: str | None = None
    interval: str | None = None
    intervalNum: int | None = None
    limit: int | None = None


class SymbolFilterWire(_Wire):
    """One entry from a symbol's ``filters`` array.

    Note ``MIN_NOTIONAL`` carries its value under ``notional``, **not**
    ``minNotional``. The founding README used the latter name; the venue does
    not (ADR-0015).
    """

    filterType: str | None = None
    tickSize: str | None = None
    minPrice: str | None = None
    maxPrice: str | None = None
    stepSize: str | None = None
    minQty: str | None = None
    maxQty: str | None = None
    notional: str | None = None
    limit: int | None = None


class SymbolWire(_Wire):
    symbol: str
    pair: str | None = None
    contractType: str | None = None
    status: str | None = None
    baseAsset: str | None = None
    quoteAsset: str | None = None
    marginAsset: str | None = None
    pricePrecision: int | None = None
    quantityPrecision: int | None = None
    baseAssetPrecision: int | None = None
    quotePrecision: int | None = None
    maintMarginPercent: str | None = None
    requiredMarginPercent: str | None = None
    liquidationFee: str | None = None
    onboardDate: int | None = None
    deliveryDate: int | None = None
    filters: list[SymbolFilterWire] = []

    def filter_of(self, filter_type: str) -> SymbolFilterWire | None:
        for entry in self.filters:
            if entry.filterType == filter_type:
                return entry
        return None


class ExchangeInfoWire(_Wire):
    timezone: str | None = None
    serverTime: int | None = None
    futuresType: str | None = None
    rateLimits: list[RateLimitWire] = []
    symbols: list[SymbolWire] = []

    def weight_limit_per_minute(self) -> int | None:
        """The ``REQUEST_WEIGHT`` ceiling, so the budget need not hardcode it."""
        for entry in self.rateLimits:
            if (
                entry.rateLimitType == "REQUEST_WEIGHT"
                and entry.interval == "MINUTE"
                and entry.intervalNum == 1
                and entry.limit is not None
            ):
                return entry.limit
        return None


class PremiumIndexWire(_Wire):
    """`premiumIndex`: mark/index price and the in-progress funding rate."""

    symbol: str
    markPrice: str | None = None
    indexPrice: str | None = None
    estimatedSettlePrice: str | None = None
    lastFundingRate: str | None = None
    interestRate: str | None = None
    nextFundingTime: int | None = None
    time: int | None = None


class FundingRateWire(_Wire):
    """One settled funding payment from `fundingRate`."""

    symbol: str
    fundingTime: int | None = None
    fundingRate: str | None = None
    markPrice: str | None = None
    #: Observed value: "Regular". Undocumented in our founding assumptions.
    rateType: str | None = None


class FundingInfoWire(_Wire):
    """Per-symbol funding cadence and rate caps from `fundingInfo`.

    This endpoint is the *only* source of the funding interval — `exchangeInfo`
    does not carry it — and the interval is emphatically not a constant: 442 of
    742 symbols were 4-hourly on 2026-08-09 (ADR-0016).
    """

    symbol: str
    adjustedFundingRateCap: str | None = None
    adjustedFundingRateFloor: str | None = None
    fundingIntervalHours: int | None = None
    disclaimer: bool | None = None
    #: `null` for BTCUSDT and ETHUSDT, an integer elsewhere. See module docstring.
    updateTime: int | None = None


class MarginBracketTierWire(_Wire):
    """One tier from the authenticated ``leverageBracket`` response.

    Unlike most Binance price fields, the endpoint emits ratios and notionals as
    JSON numbers. The REST client parses JSON floating-point tokens directly into
    :class:`Decimal` before validation, so binary float error never enters here.
    """

    bracket: int | None = None
    initialLeverage: int | None = None
    notionalCap: Decimal | None = None
    notionalFloor: Decimal | None = None
    maintMarginRatio: Decimal | None = None
    cum: Decimal | None = None


class LeverageBracketWire(_Wire):
    symbol: str
    notionalCoef: Decimal | None = None
    brackets: list[MarginBracketTierWire] = []


class BookTickerWire(_Wire):
    """Best bid/ask. ``time`` and ``lastUpdateId`` are futures-only."""

    symbol: str
    bidPrice: str | None = None
    bidQty: str | None = None
    askPrice: str | None = None
    askQty: str | None = None
    time: int | None = None
    lastUpdateId: int | None = None


class BookTickerStreamWire(_Wire):
    """The WebSocket form of a book ticker, which shares no field names with the
    REST form. Recorded 2026-08-09 from ``btcusdt@bookTicker``."""

    e: str | None = None  # event type
    u: int | None = None  # order-book update id
    s: str | None = None  # symbol
    b: str | None = None  # best bid price
    B: str | None = None  # best bid qty
    a: str | None = None  # best ask price
    A: str | None = None  # best ask qty
    T: int | None = None  # transaction time
    E: int | None = None  # event time


class CombinedStreamWire(_Wire):
    """The combined-stream envelope: ``{"stream": ..., "data": {...}}``."""

    stream: str | None = None
    data: dict[str, object] = {}
