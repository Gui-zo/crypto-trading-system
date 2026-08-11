"""Base URLs and paths — the single point of correction for routing (ADR-0003).

All four base URLs below were reached successfully on 2026-08-09. The paths were
exercised the same day except where marked.

Note the asymmetry in the testnet hostnames: futures testnet is
``testnet.binancefuture.com`` (no dot between "binance" and "future"), spot
testnet is ``testnet.binance.vision``. They follow no shared convention, which is
exactly the sort of thing that gets mistyped once and debugged for an hour.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from domain.instrument import VenueEnvironment

VENUE_CODE = "BINANCE"


class Market(StrEnum):
    """Which Binance product a request targets. They are separate APIs."""

    SPOT = "spot"
    USDM = "usdm"  # USDⓈ-M futures


_REST_BASE: dict[tuple[Market, VenueEnvironment], str] = {
    (Market.USDM, VenueEnvironment.PRODUCTION): "https://fapi.binance.com",
    (Market.USDM, VenueEnvironment.TESTNET): "https://testnet.binancefuture.com",
    (Market.SPOT, VenueEnvironment.PRODUCTION): "https://api.binance.com",
    (Market.SPOT, VenueEnvironment.TESTNET): "https://testnet.binance.vision",
}

_WS_BASE: dict[tuple[Market, VenueEnvironment], str] = {
    (Market.USDM, VenueEnvironment.PRODUCTION): "wss://fstream.binance.com",
    (Market.USDM, VenueEnvironment.TESTNET): "wss://fstream.binancefuture.com",
    (Market.SPOT, VenueEnvironment.PRODUCTION): "wss://stream.binance.com:9443",
    (Market.SPOT, VenueEnvironment.TESTNET): "wss://stream.testnet.binance.vision",
}

#: Path prefix per market. Spot is ``/api/v3``, futures ``/fapi/v1``.
_PREFIX: dict[Market, str] = {Market.SPOT: "/api/v3", Market.USDM: "/fapi/v1"}

#: Documented request weights, keyed by (market, path). **Advisory only** — the
#: authoritative number is the ``X-MBX-USED-WEIGHT-1M`` header the venue returns,
#: which `rate_limit.RateLimitBudget` consumes. These estimates exist so a budget
#: check can happen *before* a request is sent; they are never trusted after.
DOCUMENTED_WEIGHTS: dict[tuple[Market, str], int] = {
    (Market.USDM, "/fapi/v1/ping"): 1,
    (Market.USDM, "/fapi/v1/time"): 1,
    (Market.USDM, "/fapi/v1/exchangeInfo"): 1,
    (Market.USDM, "/fapi/v1/premiumIndex"): 1,
    (Market.USDM, "/fapi/v1/fundingRate"): 1,
    (Market.USDM, "/fapi/v1/fundingInfo"): 1,
    (Market.USDM, "/fapi/v1/leverageBracket"): 1,
    (Market.USDM, "/fapi/v1/ticker/bookTicker"): 2,
    (Market.USDM, "/fapi/v1/klines"): 5,
    (Market.SPOT, "/api/v3/ping"): 1,
    (Market.SPOT, "/api/v3/time"): 1,
    (Market.SPOT, "/api/v3/exchangeInfo"): 20,
    (Market.SPOT, "/api/v3/ticker/bookTicker"): 2,
    (Market.SPOT, "/api/v3/klines"): 2,
}

#: Observed request-weight ceiling per minute, from the `exchangeInfo` envelope
#: on 2026-08-09 (USDⓈ-M reported 2400). Read it from the venue at startup
#: rather than trusting this; it is a floor for safety, not a fact.
FALLBACK_WEIGHT_LIMIT_PER_MINUTE = 1200


@dataclass(frozen=True, slots=True)
class BinanceEndpoints:
    """Resolves URLs for one environment. Construct once, pass it around."""

    environment: VenueEnvironment

    def rest_base(self, market: Market) -> str:
        return _REST_BASE[(market, self.environment)]

    def ws_base(self, market: Market) -> str:
        return _WS_BASE[(market, self.environment)]

    def path(self, market: Market, endpoint: str) -> str:
        """Full path for a bare endpoint name, e.g. ``premiumIndex``."""
        return f"{_PREFIX[market]}/{endpoint.lstrip('/')}"

    def url(self, market: Market, endpoint: str) -> str:
        return f"{self.rest_base(market)}{self.path(market, endpoint)}"

    def combined_stream_url(self, market: Market, streams: tuple[str, ...]) -> str:
        """Combined-stream URL.

        Only the combined form (``/stream?streams=``) is used. The raw form
        (``/ws/<stream>``) was unreachable during the 2026-08-09 probe while the
        combined form worked, and the combined envelope names its stream in every
        frame, which the demultiplexer needs anyway.
        """
        if not streams:
            raise ValueError("at least one stream is required")
        return f"{self.ws_base(market)}/stream?streams={'/'.join(streams)}"


def documented_weight(market: Market, path: str) -> int:
    """Pre-request weight estimate; unknown paths are assumed expensive.

    Defaulting an unknown path to 1 would make the pre-flight check useless
    exactly when a new, heavy endpoint is added.
    """
    return DOCUMENTED_WEIGHTS.get((market, path), 10)
