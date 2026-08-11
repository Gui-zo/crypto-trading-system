"""REST client tests against an in-process transport.

`httpx.MockTransport` replaces the network, so the retention, budget, and error
paths are exercised deterministically. The payloads served are the **recorded**
ones, so a client test and a contract test cannot disagree about what Binance
sends.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from domain.instrument import VenueEnvironment
from storage.raw_store import InMemoryRawStore
from venue_binance.auth import BinanceSigner
from venue_binance.client import BinanceRestClient
from venue_binance.endpoints import Market
from venue_binance.errors import (
    BinanceAPIError,
    BinanceAuthenticationError,
    BinanceRateLimitError,
    BinanceTransportError,
)
from venue_binance.rate_limit import RateLimitBudget, RateLimitExceeded

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "binance" / "recorded"

def recorded(name: str) -> bytes:
    return (RECORDED / name).read_bytes()


ROUTES: dict[str, str] = {
    "/fapi/v1/time": "fapi_time.json",
    "/fapi/v1/premiumIndex": "fapi_premiumIndex.json",
    "/fapi/v1/fundingRate": "fapi_fundingRate.json",
    "/fapi/v1/fundingInfo": "fapi_fundingInfo.trimmed.json",
    "/fapi/v1/exchangeInfo": "fapi_exchangeInfo.trimmed.json",
    "/fapi/v1/ticker/bookTicker": "fapi_bookTicker.json",
    "/fapi/v1/klines": "fapi_klines.json",
    "/api/v3/ticker/bookTicker": "spot_bookTicker.json",
}


def handler(
    *,
    status: int = 200,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.MockTransport:
    def respond(request: httpx.Request) -> httpx.Response:
        if body is not None or status != 200:
            return httpx.Response(
                status, content=body or b"{}", headers=headers or {}
            )
        name = ROUTES.get(request.url.path)
        if name is None:
            return httpx.Response(404, json={"code": -1121, "msg": "Invalid path."})
        return httpx.Response(
            200,
            content=recorded(name),
            headers={"x-mbx-used-weight-1m": "7", **(headers or {})},
        )

    return httpx.MockTransport(respond)


def client(
    transport: httpx.MockTransport,
    *,
    raw_store: InMemoryRawStore | None = None,
    budget: RateLimitBudget | None = None,
    environment: VenueEnvironment = VenueEnvironment.PRODUCTION,
    signer: BinanceSigner | None = None,
) -> BinanceRestClient:
    return BinanceRestClient(
        environment=environment,
        http=httpx.AsyncClient(transport=transport),
        raw_store=raw_store,
        budget=budget,
        signer=signer,
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


async def test_mark_price_returns_a_domain_snapshot() -> None:
    response = await client(handler()).mark_price("BTCUSDT")
    assert response.value.instrument.symbol == "BTCUSDT"
    assert response.value.mark_price > 0


async def test_funding_history_returns_observations() -> None:
    response = await client(handler()).funding_history("BTCUSDT", limit=3)
    assert len(response.value) == 3


async def test_funding_info_returns_every_entry() -> None:
    response = await client(handler()).funding_info()
    assert {w.symbol for w in response.value} >= {"BTCUSDT", "LPTUSDT"}


async def test_book_ticker_works_for_both_markets() -> None:
    api = client(handler())
    futures = await api.book_ticker("BTCUSDT", Market.USDM)
    spot = await api.book_ticker("BTCUSDT", Market.SPOT)
    assert futures.value.venue_time is not None
    assert spot.value.venue_time is None


async def test_the_still_forming_final_candle_is_dropped() -> None:
    """Look-ahead prevention: Binance's last kline is the candle in progress.

    The recorded fixture holds two rows; one is still forming, so one survives.
    """
    response = await client(handler()).klines("BTCUSDT", interval="8h", limit=2)
    assert len(response.value) == 1
    assert response.value[0].is_closed


async def test_clock_drift_is_measured_against_venue_time() -> None:
    drift, server_time = await client(handler()).clock_drift()
    assert isinstance(drift, float)
    assert server_time.tzinfo is not None


async def test_margin_brackets_use_a_signed_read_and_keep_decimal_exactness() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=recorded("fapi_leverageBracket.trimmed.json"))

    signer = BinanceSigner("test-api-key", "test-secret")
    response = await client(httpx.MockTransport(respond), signer=signer).margin_brackets()

    assert response.value[0].symbol == "BTCUSDT"
    assert response.value[0].brackets[0].maintMarginRatio == Decimal("0.004")
    assert seen[0].headers["X-MBX-APIKEY"] == "test-api-key"
    query = seen[0].url.query.decode()
    unsigned, separator, signature = query.rpartition("&signature=")
    assert separator
    assert signer.verify(unsigned, signature)


async def test_a_signed_read_without_a_signer_fails_before_transport() -> None:
    with pytest.raises(BinanceAuthenticationError, match="requires a configured"):
        await client(handler()).margin_brackets()


# ---------------------------------------------------------------------------
# Raw retention
# ---------------------------------------------------------------------------


async def test_responses_are_retained_byte_for_byte() -> None:
    store = InMemoryRawStore()
    response = await client(handler(), raw_store=store).mark_price("BTCUSDT")
    assert response.raw is not None
    assert store.get(response.raw.key) == recorded("fapi_premiumIndex.json")


async def test_retention_keys_are_environment_scoped() -> None:
    """A testnet payload must be unreachable from a production key (ADR-0010)."""
    store = InMemoryRawStore()
    api = client(handler(), raw_store=store, environment=VenueEnvironment.TESTNET)
    response = await api.mark_price("BTCUSDT")
    assert response.raw is not None
    assert response.raw.key.startswith("binance/testnet/usdm/premiumIndex/")


async def test_an_unparseable_payload_is_still_retained() -> None:
    """The payload that breaks the parser is the one worth keeping (ADR-0003)."""
    store = InMemoryRawStore()
    api = client(handler(body=b"<html>maintenance</html>"), raw_store=store)
    with pytest.raises(BinanceTransportError, match="not JSON"):
        await api.mark_price("BTCUSDT")
    assert store.get(next(iter(store._objects))) == b"<html>maintenance</html>"


async def test_an_error_response_is_retained_too() -> None:
    store = InMemoryRawStore()
    api = client(
        handler(status=400, body=recorded("fapi_error_invalid_symbol.json")), raw_store=store
    )
    with pytest.raises(BinanceAPIError):
        await api.mark_price("NOSUCHPAIR")
    assert len(store._objects) == 1


async def test_a_client_without_a_raw_store_still_works() -> None:
    response = await client(handler()).mark_price("BTCUSDT")
    assert response.raw is None


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


async def test_the_venue_weight_header_is_adopted() -> None:
    api = client(handler())
    response = await api.mark_price("BTCUSDT")
    assert response.weight is not None
    assert response.weight.used == 7


async def test_the_weight_limit_is_adopted_from_exchange_info() -> None:
    """2400/min was the observed USDⓈ-M ceiling; the constant is only a fallback."""
    api = client(handler())
    await api.exchange_info()
    assert api.budget.limit == 2400


async def test_a_request_over_budget_is_refused_before_it_is_sent() -> None:
    budget = RateLimitBudget(limit_per_minute=10, safety_fraction=0.5)
    budget.charge(5)
    with pytest.raises(RateLimitExceeded):
        await client(handler(), budget=budget).mark_price("BTCUSDT")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


async def test_an_error_status_becomes_a_typed_exception() -> None:
    api = client(handler(status=400, body=recorded("fapi_error_invalid_symbol.json")))
    with pytest.raises(BinanceAPIError) as exc:
        await api.mark_price("NOSUCHPAIR")
    assert exc.value.is_invalid_symbol
    assert exc.value.code == -1121


@pytest.mark.parametrize(("status", "banned"), [(429, False), (418, True)])
async def test_rate_limit_statuses_are_distinguished_from_ordinary_errors(
    status: int, banned: bool
) -> None:
    """418 is an IP ban, not a backoff signal; conflating them extends the ban."""
    api = client(handler(status=status, headers={"retry-after": "30"}))
    with pytest.raises(BinanceRateLimitError) as exc:
        await api.mark_price("BTCUSDT")
    assert exc.value.retry_after_seconds == 30.0
    assert exc.value.is_ip_banned is banned


async def test_a_server_error_is_marked_retryable() -> None:
    api = client(handler(status=503, body=b'{"code":-1001,"msg":"Internal error."}'))
    with pytest.raises(BinanceAPIError) as exc:
        await api.mark_price("BTCUSDT")
    assert exc.value.is_retryable


async def test_a_transport_failure_becomes_a_typed_exception() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    api = client(httpx.MockTransport(boom))
    with pytest.raises(BinanceTransportError, match="ConnectError"):
        await api.mark_price("BTCUSDT")


async def test_a_non_json_error_body_does_not_mask_the_status() -> None:
    api = client(handler(status=502, body=b"<html>bad gateway</html>"))
    with pytest.raises(BinanceAPIError) as exc:
        await api.mark_price("BTCUSDT")
    assert exc.value.status_code == 502
    assert exc.value.code is None


async def test_an_out_of_range_funding_limit_is_refused_locally() -> None:
    with pytest.raises(ValueError, match="limit must be in"):
        await client(handler()).funding_history("BTCUSDT", limit=5000)


# ---------------------------------------------------------------------------
# Environment routing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("environment", "expected_host"),
    [
        (VenueEnvironment.PRODUCTION, "fapi.binance.com"),
        (VenueEnvironment.TESTNET, "testnet.binancefuture.com"),
    ],
)
async def test_requests_go_to_the_host_for_their_environment(
    environment: VenueEnvironment, expected_host: str
) -> None:
    seen: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(200, content=recorded("fapi_premiumIndex.json"))

    api = client(httpx.MockTransport(record), environment=environment)
    await api.mark_price("BTCUSDT")
    assert seen == [expected_host]


async def test_the_symbol_is_upper_cased_on_the_wire() -> None:
    seen: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=recorded("fapi_premiumIndex.json"))

    await client(httpx.MockTransport(record)).mark_price("btcusdt")
    assert "symbol=BTCUSDT" in seen[0]


async def test_the_client_exposes_no_order_path() -> None:
    """A structural assertion, not a stylistic one: read-only means read-only."""
    forbidden = {"order", "post", "cancel", "submit", "transfer", "leverage"}
    methods = {name for name in dir(BinanceRestClient) if not name.startswith("_")}
    assert not {m for m in methods if any(word in m.lower() for word in forbidden)}


def test_json_payloads_are_never_parsed_into_floats() -> None:
    """Guards ADR-0011 at the boundary where exactness is still recoverable."""
    payload = json.loads(recorded("fapi_premiumIndex.json"))
    assert isinstance(payload["markPrice"], str)
