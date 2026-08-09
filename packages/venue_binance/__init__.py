"""Binance venue adapter (read-only).

Nothing in this package can place, cancel, or modify an order, and no module here
imports a signing path for a trading endpoint. That is a structural property, not
a convention: the order path does not exist yet (Phase 8), and the safety
boundary in the README says it must not be added casually.

Layering, and the single points of correction ADR-0003 requires:

* ``endpoints`` — every base URL and path. Change a URL in exactly one place.
* ``auth`` — the signed-message format. Change signing in exactly one place.
* ``errors`` — venue error codes to typed exceptions.
* ``rate_limit`` — request-weight budget, driven by the venue's own headers.
* ``schemas`` — tolerant wire models. Never used outside this package.
* ``mapping`` — wire to domain. The only place field names are interpreted.
* ``client`` / ``ws_client`` — transport.
"""

from venue_binance.endpoints import BinanceEndpoints, Market
from venue_binance.errors import (
    BinanceAPIError,
    BinanceError,
    BinanceRateLimitError,
    BinanceTransportError,
)
from venue_binance.rate_limit import RateLimitBudget, WeightSnapshot

__all__ = [
    "BinanceAPIError",
    "BinanceEndpoints",
    "BinanceError",
    "BinanceRateLimitError",
    "BinanceTransportError",
    "Market",
    "RateLimitBudget",
    "WeightSnapshot",
]
