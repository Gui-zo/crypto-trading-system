"""Read-only Binance REST client.

Every request goes through :meth:`BinanceRestClient._get`, which is the single
place that:

1. checks the local weight budget **before** sending (a refusal here is always
   cheaper than a 429, and vastly cheaper than the 418 ban that follows);
2. sends the request;
3. **retains the raw response bytes** under an environment-scoped key, *before*
   any parsing, so a schema correction is a replay rather than a re-collection
   (ADR-0003, ADR-0010);
4. adopts the venue's own used-weight header, overriding the local estimate;
5. converts an error status into a typed exception.

Retention happens before parsing on purpose. A payload that fails to parse is
precisely the payload worth keeping, and a client that stores only what it
understood would discard exactly the evidence needed to fix itself.

There is **no order path here and none may be added.** The client exposes market
data only; it does not even hold a signer.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from domain.instrument import InstrumentRef, VenueEnvironment, VenueScope
from domain.market_data import BookTicker, FundingRateObservation, Kline, MarkPriceSnapshot
from storage.raw_store import RawObjectRef, RawStore
from venue_binance import mapping
from venue_binance.endpoints import (
    FALLBACK_WEIGHT_LIMIT_PER_MINUTE,
    VENUE_CODE,
    BinanceEndpoints,
    Market,
    documented_weight,
)
from venue_binance.errors import (
    BinanceAPIError,
    BinanceRateLimitError,
    BinanceTransportError,
)
from venue_binance.rate_limit import RateLimitBudget, WeightSnapshot, parse_retry_after
from venue_binance.schemas import (
    BookTickerWire,
    ExchangeInfoWire,
    FundingInfoWire,
    FundingRateWire,
    PremiumIndexWire,
    ServerTimeWire,
)

DEFAULT_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class RawRecord:
    """Where a response was retained, so a row can point at its own source."""

    key: str
    sha256: str
    size: int
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class Response[T]:
    """A parsed payload plus the provenance needed to audit it."""

    value: T
    raw: RawRecord | None
    weight: WeightSnapshot | None


class BinanceRestClient:
    """Read-only market-data client for one environment.

    Not thread-safe, and deliberately scoped to a single :class:`VenueEnvironment`
    — an instance that could switch environments mid-life is how testnet and
    production data end up in the same series (ADR-0010).
    """

    def __init__(
        self,
        *,
        environment: VenueEnvironment,
        http: httpx.AsyncClient,
        raw_store: RawStore | None = None,
        budget: RateLimitBudget | None = None,
    ) -> None:
        self._environment = environment
        self._http = http
        self._raw_store = raw_store
        self._budget = budget or RateLimitBudget(
            limit_per_minute=FALLBACK_WEIGHT_LIMIT_PER_MINUTE
        )
        self._endpoints = BinanceEndpoints(environment)
        self._scope = VenueScope(venue=VENUE_CODE, environment=environment)

    @property
    def environment(self) -> VenueEnvironment:
        return self._environment

    @property
    def scope(self) -> VenueScope:
        return self._scope

    @property
    def budget(self) -> RateLimitBudget:
        return self._budget

    def instrument(self, symbol: str, market: Market) -> InstrumentRef:
        return InstrumentRef(scope=self._scope, symbol=symbol, market=market.value)

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _raw_key(self, market: Market, endpoint: str, fetched_at: datetime) -> str:
        """Environment-scoped, endpoint-scoped, time-ordered retention key.

        The environment comes first so a whole environment's payloads can be
        swept, and so no key is reachable from the wrong one (ADR-0010).
        """
        stamp = fetched_at.strftime("%Y/%m/%d/%H%M%S%f")
        return f"binance/{self._environment.value}/{market.value}/{endpoint}/{stamp}.json"

    async def _get(
        self,
        market: Market,
        endpoint: str,
        params: Sequence[tuple[str, str | int]] = (),
    ) -> tuple[Any, RawRecord | None, WeightSnapshot | None]:
        path = self._endpoints.path(market, endpoint)
        weight = documented_weight(market, path)
        self._budget.check(weight)

        url = self._endpoints.url(market, endpoint)
        try:
            response = await self._http.get(url, params=list(params))
        except httpx.HTTPError as exc:
            raise BinanceTransportError(f"{path}: {type(exc).__name__}: {exc}") from exc

        fetched_at = datetime.now(UTC)
        self._budget.charge(weight)
        observed = self._budget.observe_headers(dict(response.headers))

        # Retain before parsing: an unparseable payload is the one worth keeping.
        raw = self._retain(market, endpoint, response.content, fetched_at)

        if response.status_code in (418, 429):
            raise BinanceRateLimitError(
                status_code=response.status_code,
                retry_after_seconds=parse_retry_after(dict(response.headers)),
                path=path,
            )
        if response.status_code >= 400:
            code, message = _error_body(response.content)
            raise BinanceAPIError(
                status_code=response.status_code, code=code, message=message, path=path
            )

        try:
            payload = json.loads(response.content)
        except json.JSONDecodeError as exc:
            raise BinanceTransportError(f"{path}: response was not JSON: {exc}") from exc
        return payload, raw, observed

    def _retain(
        self, market: Market, endpoint: str, content: bytes, fetched_at: datetime
    ) -> RawRecord | None:
        if self._raw_store is None:
            return None
        ref: RawObjectRef = self._raw_store.put(
            self._raw_key(market, endpoint, fetched_at), content
        )
        return RawRecord(key=ref.key, sha256=ref.sha256, size=ref.size, fetched_at=fetched_at)

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    async def ping(self, market: Market = Market.USDM) -> bool:
        await self._get(market, "ping")
        return True

    async def server_time(self, market: Market = Market.USDM) -> Response[datetime]:
        payload, raw, weight = await self._get(market, "time")
        wire = ServerTimeWire.model_validate(payload)
        return Response(
            value=mapping.to_utc(wire.serverTime, "serverTime", "server"),
            raw=raw,
            weight=weight,
        )

    async def clock_drift(self, market: Market = Market.USDM) -> tuple[float, datetime]:
        """Seconds by which our clock leads the venue's.

        A signed request is rejected outside ``recvWindow``, and funding settles
        on UTC boundaries, so drift is a first-class operational reading rather
        than a diagnostic curiosity.
        """
        before = datetime.now(UTC)
        response = await self.server_time(market)
        after = datetime.now(UTC)
        local_mid = before + (after - before) / 2
        return (local_mid - response.value).total_seconds(), response.value

    async def exchange_info(self, market: Market = Market.USDM) -> Response[ExchangeInfoWire]:
        payload, raw, weight = await self._get(market, "exchangeInfo")
        wire = ExchangeInfoWire.model_validate(payload)
        # Adopt the venue's own weight ceiling rather than trusting our constant.
        limit = wire.weight_limit_per_minute()
        if limit is not None:
            self._budget.adopt_limit(limit)
        return Response(value=wire, raw=raw, weight=weight)

    async def funding_info(self) -> Response[list[FundingInfoWire]]:
        """Per-symbol funding cadence and caps. USDⓈ-M only.

        The only source of the funding interval — `exchangeInfo` does not carry
        it, and the interval is per symbol (ADR-0016).
        """
        payload, raw, weight = await self._get(Market.USDM, "fundingInfo")
        if not isinstance(payload, list):
            raise BinanceTransportError("fundingInfo: expected a JSON array")
        return Response(
            value=[FundingInfoWire.model_validate(item) for item in payload],
            raw=raw,
            weight=weight,
        )

    async def mark_price(self, symbol: str) -> Response[MarkPriceSnapshot]:
        payload, raw, weight = await self._get(
            Market.USDM, "premiumIndex", (("symbol", symbol.upper()),)
        )
        wire = PremiumIndexWire.model_validate(payload)
        return Response(
            value=mapping.to_mark_price(
                wire,
                instrument=self.instrument(symbol, Market.USDM),
                collected_at=datetime.now(UTC),
            ),
            raw=raw,
            weight=weight,
        )

    async def funding_history(
        self, symbol: str, *, limit: int = 100
    ) -> Response[list[FundingRateObservation]]:
        if not 0 < limit <= 1000:
            raise ValueError("funding history limit must be in (0, 1000]")
        payload, raw, weight = await self._get(
            Market.USDM,
            "fundingRate",
            (("symbol", symbol.upper()), ("limit", limit)),
        )
        if not isinstance(payload, list):
            raise BinanceTransportError("fundingRate: expected a JSON array")
        instrument = self.instrument(symbol, Market.USDM)
        collected_at = datetime.now(UTC)
        return Response(
            value=[
                mapping.to_funding_rate(
                    FundingRateWire.model_validate(item),
                    instrument=instrument,
                    collected_at=collected_at,
                )
                for item in payload
            ],
            raw=raw,
            weight=weight,
        )

    async def book_ticker(self, symbol: str, market: Market) -> Response[BookTicker]:
        payload, raw, weight = await self._get(
            market, "ticker/bookTicker", (("symbol", symbol.upper()),)
        )
        # Spot returns an array when no symbol is given; with one it returns an
        # object. Guard anyway — the shapes differ between markets (ADR-0015).
        if isinstance(payload, list):
            if not payload:
                raise BinanceTransportError("bookTicker: empty array for a symbol query")
            payload = payload[0]
        wire = BookTickerWire.model_validate(payload)
        return Response(
            value=mapping.to_book_ticker(
                wire,
                instrument=self.instrument(symbol, market),
                collected_at=datetime.now(UTC),
            ),
            raw=raw,
            weight=weight,
        )

    async def klines(
        self, symbol: str, *, interval: str, limit: int = 500, market: Market = Market.USDM
    ) -> Response[list[Kline]]:
        """Candles, **with the still-forming final candle dropped**.

        Binance's last row is the candle in progress. Treating it as settled is
        look-ahead bias, and it is the single easiest way to make a backtest look
        brilliant, so it is removed here rather than left to every caller.
        """
        payload, raw, weight = await self._get(
            market,
            "klines",
            (("symbol", symbol.upper()), ("interval", interval), ("limit", limit)),
        )
        if not isinstance(payload, list):
            raise BinanceTransportError("klines: expected a JSON array")
        instrument = self.instrument(symbol, market)
        collected_at = datetime.now(UTC)
        closed = payload[:-1] if payload else []
        return Response(
            value=[
                mapping.to_kline(row, instrument=instrument, collected_at=collected_at)
                for row in closed
                if isinstance(row, list)
            ],
            raw=raw,
            weight=weight,
        )


def _error_body(content: bytes) -> tuple[int | None, str]:
    """Extract Binance's ``code``/``msg`` from an error body, tolerantly."""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None, content.decode("utf-8", errors="replace")[:200]
    if not isinstance(parsed, dict):
        return None, str(parsed)[:200]
    code = parsed.get("code")
    message = parsed.get("msg", "")
    return (code if isinstance(code, int) else None), str(message)


def create_http_client(*, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> httpx.AsyncClient:
    """An httpx client with sane defaults and an identifying user agent."""
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        headers={"User-Agent": "crypto-trading-system/0.1 (read-only market data)"},
    )
