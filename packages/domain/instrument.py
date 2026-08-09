"""Instrument identity and the venue scope every market fact is keyed by.

A bare symbol string is not an identity. ``BTCUSDT`` exists on Binance testnet and
on production, and — as the 2026-08-09 recording showed (ADR-0015) — the two carry
*plausibly similar* prices, so a mixed series looks entirely normal and every
statistic computed from it is wrong. :class:`VenueScope` is therefore part of the
key, not metadata attached to it (ADR-0010).

Full instrument *specification* — filters, margin tiers, their versioning and
approval — is Phase 2. What lives here is the identity and the handful of
attributes Phase 1 must understand to decide what it is even looking at.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum


class VenueEnvironment(StrEnum):
    """Mirrors ``config.settings.BinanceEnv`` in the domain, without importing it.

    Duplicated deliberately: `packages/domain/` may not depend on configuration,
    and a domain value object that carries a settings enum would invert that.
    """

    TESTNET = "testnet"
    PRODUCTION = "production"


class InstrumentStatus(StrEnum):
    """Symbol status as `exchangeInfo` reports it.

    The vocabulary is the venue's, recorded rather than assumed: the
    2026-08-09 capture contained exactly ``TRADING`` (726), ``SETTLING`` (127),
    and ``PENDING_TRADING`` (1). ``UNKNOWN`` covers anything Binance adds later —
    a value we have never seen must never parse as tradeable.
    """

    TRADING = "TRADING"
    SETTLING = "SETTLING"
    PENDING_TRADING = "PENDING_TRADING"
    UNKNOWN = "UNKNOWN"

    @property
    def is_tradeable(self) -> bool:
        return self is InstrumentStatus.TRADING


class ContractType(StrEnum):
    """Contract type from `exchangeInfo`.

    ``TRADIFI_PERPETUAL`` is the surprise: 153 of 854 symbols in the capture are
    tokenised equities and metals (AAPLUSDT, TSLAUSDT, XAUUSDT). They are
    perpetuals on the same venue with the same shape, so nothing about the wire
    format excludes them — only this field does. See ADR-0016.
    """

    PERPETUAL = "PERPETUAL"
    TRADIFI_PERPETUAL = "TRADIFI_PERPETUAL"
    CURRENT_QUARTER = "CURRENT_QUARTER"
    NEXT_QUARTER = "NEXT_QUARTER"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class VenueScope:
    """Which venue and environment a fact belongs to."""

    venue: str
    environment: VenueEnvironment

    def __post_init__(self) -> None:
        if not self.venue.strip():
            raise ValueError("venue code cannot be empty")
        object.__setattr__(self, "venue", self.venue.strip().upper())


@dataclass(frozen=True, slots=True)
class InstrumentRef:
    """A symbol on a specific venue, environment, and market.

    ``market`` separates the two legs of a carry: Binance quotes ``BTCUSDT`` on
    both spot and USDⓈ-M futures, at different prices, under the same name.
    """

    scope: VenueScope
    symbol: str
    market: str  # "spot" | "usdm"

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("symbol cannot be empty")
        if self.market not in {"spot", "usdm"}:
            raise ValueError(f"unknown market {self.market!r}; expected 'spot' or 'usdm'")
        object.__setattr__(self, "symbol", self.symbol.strip().upper())

    @property
    def key(self) -> str:
        """A stable, environment-qualified identity for storage keys and logs."""
        return f"{self.scope.venue}:{self.scope.environment.value}:{self.market}:{self.symbol}"


@dataclass(frozen=True, slots=True)
class FundingSchedule:
    """How often a symbol settles funding, and the caps applied to its rate.

    **The interval is per symbol and must never be assumed.** In the 2026-08-09
    capture, 442 of 742 symbols funded every 4 hours and only 296 every 8 — the
    4-hour cadence is the *majority*, not the exception the founding README
    described. Four symbols fund hourly. BTCUSDT and ETHUSDT are 8-hourly, which
    is why an 8-hour default looks correct right up until the universe widens.

    The caps matter as much: BTCUSDT is capped at ±0.30% while a long-tail symbol
    like GTCUSDT is capped at ±2.00%, so the maximum harvestable carry differs by
    nearly an order of magnitude between symbols.
    """

    interval_hours: int
    rate_cap: object | None = None  # Decimal; typed loosely to keep this module import-free
    rate_floor: object | None = None

    def __post_init__(self) -> None:
        if self.interval_hours <= 0:
            raise ValueError("funding interval must be positive")
        if 24 % self.interval_hours != 0:
            # Every interval observed divides the day evenly (1, 4, 8). A value
            # that does not would make settlement times drift across UTC days,
            # which every downstream point-in-time join assumes cannot happen.
            raise ValueError(f"funding interval {self.interval_hours}h does not divide 24h")

    @property
    def interval(self) -> timedelta:
        return timedelta(hours=self.interval_hours)

    @property
    def settlements_per_day(self) -> int:
        return 24 // self.interval_hours
