"""Instrument identity and the venue scope every market fact is keyed by.

A bare symbol string is not an identity. ``BTCUSDT`` exists on Binance testnet and
on production, and — as the 2026-08-09 recording showed (ADR-0015) — the two carry
*plausibly similar* prices, so a mixed series looks entirely normal and every
statistic computed from it is wrong. :class:`VenueScope` is therefore part of the
key, not metadata attached to it (ADR-0010).

Phase 2 extends the identity with exact filters, per-symbol funding, account-
specific margin tiers, canonical catalogs, and explicit review state. Persistence
stays outside the domain; every value and invariant here remains pure.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise


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
    rate_cap: Decimal | None = None
    rate_floor: Decimal | None = None

    def __post_init__(self) -> None:
        if self.interval_hours <= 0:
            raise ValueError("funding interval must be positive")
        if 24 % self.interval_hours != 0:
            # Every interval observed divides the day evenly (1, 4, 8). A value
            # that does not would make settlement times drift across UTC days,
            # which every downstream point-in-time join assumes cannot happen.
            raise ValueError(f"funding interval {self.interval_hours}h does not divide 24h")
        if self.rate_cap is not None and self.rate_cap <= 0:
            raise ValueError("funding rate cap must be positive")
        if self.rate_floor is not None and self.rate_floor >= 0:
            raise ValueError("funding rate floor must be negative")
        if (
            self.rate_cap is not None
            and self.rate_floor is not None
            and self.rate_floor >= self.rate_cap
        ):
            raise ValueError("funding rate floor must be below the cap")

    @property
    def interval(self) -> timedelta:
        return timedelta(hours=self.interval_hours)

    @property
    def settlements_per_day(self) -> int:
        return 24 // self.interval_hours

    def as_dict(self) -> dict[str, object]:
        return {
            "interval_hours": self.interval_hours,
            "rate_cap": str(self.rate_cap) if self.rate_cap is not None else None,
            "rate_floor": str(self.rate_floor) if self.rate_floor is not None else None,
        }


class InstrumentSpecReviewStatus(StrEnum):
    """Human-review state of an immutable instrument-catalog version."""

    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class InstrumentReviewAction(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


@dataclass(frozen=True, slots=True)
class PriceFilter:
    min_price: Decimal
    max_price: Decimal
    tick_size: Decimal

    def __post_init__(self) -> None:
        if self.min_price < 0:
            raise ValueError("minimum price cannot be negative")
        if self.max_price <= self.min_price:
            raise ValueError("maximum price must be above minimum price")
        if self.tick_size <= 0:
            raise ValueError("tick size must be positive")

    def as_dict(self) -> dict[str, str]:
        return {
            "min_price": str(self.min_price),
            "max_price": str(self.max_price),
            "tick_size": str(self.tick_size),
        }


@dataclass(frozen=True, slots=True)
class QuantityFilter:
    min_quantity: Decimal
    max_quantity: Decimal
    step_size: Decimal

    def __post_init__(self) -> None:
        if self.min_quantity < 0:
            raise ValueError("minimum quantity cannot be negative")
        if self.max_quantity <= self.min_quantity:
            raise ValueError("maximum quantity must be above minimum quantity")
        if self.step_size <= 0:
            raise ValueError("quantity step size must be positive")

    def as_dict(self) -> dict[str, str]:
        return {
            "min_quantity": str(self.min_quantity),
            "max_quantity": str(self.max_quantity),
            "step_size": str(self.step_size),
        }


@dataclass(frozen=True, slots=True)
class MaintenanceMarginTier:
    """One exact tier from Binance's account-specific bracket response."""

    bracket: int
    initial_leverage: int
    notional_floor: Decimal
    notional_cap: Decimal
    maintenance_margin_ratio: Decimal
    cumulative: Decimal

    def __post_init__(self) -> None:
        if self.bracket <= 0:
            raise ValueError("margin bracket number must be positive")
        if self.initial_leverage <= 0:
            raise ValueError("initial leverage must be positive")
        if self.notional_floor < 0:
            raise ValueError("notional floor cannot be negative")
        if self.notional_cap <= self.notional_floor:
            raise ValueError("notional cap must be above its floor")
        if self.maintenance_margin_ratio < 0:
            raise ValueError("maintenance margin ratio cannot be negative")
        if self.cumulative < 0:
            raise ValueError("cumulative maintenance amount cannot be negative")

    def as_dict(self) -> dict[str, object]:
        return {
            "bracket": self.bracket,
            "initial_leverage": self.initial_leverage,
            "notional_floor": str(self.notional_floor),
            "notional_cap": str(self.notional_cap),
            "maintenance_margin_ratio": str(self.maintenance_margin_ratio),
            "cumulative": str(self.cumulative),
        }


@dataclass(frozen=True, slots=True)
class MarginSchedule:
    """Complete, ordered maintenance-margin tiers for one symbol."""

    symbol: str
    tiers: tuple[MaintenanceMarginTier, ...]
    notional_coefficient: Decimal | None = None

    def __post_init__(self) -> None:
        normalized_symbol = self.symbol.strip().upper()
        if not normalized_symbol:
            raise ValueError("margin schedule symbol cannot be empty")
        object.__setattr__(self, "symbol", normalized_symbol)
        if not self.tiers:
            raise ValueError(f"{normalized_symbol}: margin schedule has no tiers")
        if self.notional_coefficient is not None and self.notional_coefficient <= 0:
            raise ValueError("notional coefficient must be positive")

        ordered = tuple(sorted(self.tiers, key=lambda tier: tier.bracket))
        if tuple(tier.bracket for tier in ordered) != tuple(range(1, len(ordered) + 1)):
            raise ValueError(f"{normalized_symbol}: margin brackets must be consecutive from 1")
        if ordered[0].notional_floor != 0:
            raise ValueError(f"{normalized_symbol}: first margin tier must start at zero")
        for previous, current in pairwise(ordered):
            if current.notional_floor != previous.notional_cap:
                raise ValueError(f"{normalized_symbol}: margin tiers must be contiguous")
            if current.maintenance_margin_ratio < previous.maintenance_margin_ratio:
                raise ValueError(
                    f"{normalized_symbol}: maintenance margin ratios must not decrease"
                )
            if current.initial_leverage > previous.initial_leverage:
                raise ValueError(f"{normalized_symbol}: maximum leverage must not increase")
        object.__setattr__(self, "tiers", ordered)

    def as_dict(self) -> dict[str, object]:
        return {
            "notional_coefficient": (
                str(self.notional_coefficient)
                if self.notional_coefficient is not None
                else None
            ),
            "tiers": [tier.as_dict() for tier in self.tiers],
        }


@dataclass(frozen=True, slots=True)
class InstrumentSpecification:
    """A complete, risk-usable futures instrument specification.

    An instance can only exist when every input Phase 5 will need for sizing is
    present. Missing funding metadata, filters, or margin tiers therefore produce
    an exclusion rather than a partially-populated specification.
    """

    instrument: InstrumentRef
    status: InstrumentStatus
    contract_type: ContractType
    base_asset: str
    quote_asset: str
    margin_asset: str
    price_filter: PriceFilter
    quantity_filter: QuantityFilter
    minimum_notional: Decimal
    funding_schedule: FundingSchedule
    margin_schedule: MarginSchedule
    liquidation_fee: Decimal

    def __post_init__(self) -> None:
        if self.instrument.market != "usdm":
            raise ValueError("instrument specifications currently cover USD-M futures only")
        for field_name in ("base_asset", "quote_asset", "margin_asset"):
            value = str(getattr(self, field_name)).strip().upper()
            if not value:
                raise ValueError(f"{field_name} cannot be empty")
            object.__setattr__(self, field_name, value)
        if self.status is not InstrumentStatus.TRADING:
            raise ValueError("a complete instrument specification must be TRADING")
        if self.contract_type is not ContractType.PERPETUAL:
            raise ValueError("a complete instrument specification must be PERPETUAL")
        if self.quote_asset != "USDT" or self.margin_asset != "USDT":
            raise ValueError("the v1 carry universe requires USDT quote and margin assets")
        if self.minimum_notional <= 0:
            raise ValueError("minimum notional must be positive")
        if self.liquidation_fee < 0:
            raise ValueError("liquidation fee cannot be negative")
        if self.margin_schedule.symbol != self.instrument.symbol:
            raise ValueError("margin schedule symbol does not match the instrument")
        if self.funding_schedule.rate_cap is None or self.funding_schedule.rate_floor is None:
            raise ValueError("funding cap and floor are required for a complete specification")

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.instrument.symbol,
            "market": self.instrument.market,
            "status": self.status.value,
            "contract_type": self.contract_type.value,
            "base_asset": self.base_asset,
            "quote_asset": self.quote_asset,
            "margin_asset": self.margin_asset,
            "price_filter": self.price_filter.as_dict(),
            "quantity_filter": self.quantity_filter.as_dict(),
            "minimum_notional": str(self.minimum_notional),
            "funding_schedule": self.funding_schedule.as_dict(),
            "margin_schedule": self.margin_schedule.as_dict(),
            "liquidation_fee": str(self.liquidation_fee),
        }


@dataclass(frozen=True, slots=True)
class InstrumentExclusion:
    symbol: str
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        reasons = tuple(
            sorted({reason.strip().upper() for reason in self.reasons if reason.strip()})
        )
        if not symbol:
            raise ValueError("excluded instrument symbol cannot be empty")
        if not reasons:
            raise ValueError("an instrument exclusion requires at least one reason")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "reasons", reasons)

    def as_dict(self) -> dict[str, object]:
        return {"symbol": self.symbol, "reasons": list(self.reasons)}


@dataclass(frozen=True, slots=True)
class InstrumentCatalog:
    """Canonical, content-addressed Phase-2 instrument universe."""

    scope: VenueScope
    total_symbols: int
    candidate_symbols: int
    specifications: tuple[InstrumentSpecification, ...]
    exclusions: tuple[InstrumentExclusion, ...] = ()

    def __post_init__(self) -> None:
        if self.total_symbols <= 0:
            raise ValueError("instrument catalog source cannot be empty")
        if self.candidate_symbols <= 0:
            raise ValueError("instrument catalog has no carry candidates")
        specifications = tuple(
            sorted(self.specifications, key=lambda specification: specification.instrument.symbol)
        )
        exclusions = tuple(sorted(self.exclusions, key=lambda exclusion: exclusion.symbol))
        symbols = [specification.instrument.symbol for specification in specifications]
        excluded_symbols = [exclusion.symbol for exclusion in exclusions]
        if len(symbols) != len(set(symbols)):
            raise ValueError("instrument catalog contains duplicate specifications")
        if len(excluded_symbols) != len(set(excluded_symbols)):
            raise ValueError("instrument catalog contains duplicate exclusions")
        if set(symbols) & set(excluded_symbols):
            raise ValueError("an instrument cannot be both specified and excluded")
        if self.candidate_symbols != len(specifications) + len(exclusions):
            raise ValueError("every carry candidate must be specified or explicitly excluded")
        if not specifications:
            raise ValueError("instrument catalog has no complete specifications")
        for specification in specifications:
            if specification.instrument.scope != self.scope:
                raise ValueError("instrument catalog mixes venue scopes")
        object.__setattr__(self, "specifications", specifications)
        object.__setattr__(self, "exclusions", exclusions)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "venue": self.scope.venue,
            "environment": self.scope.environment.value,
            "total_symbols": self.total_symbols,
            "candidate_symbols": self.candidate_symbols,
            "specifications": [specification.as_dict() for specification in self.specifications],
            "exclusions": [exclusion.as_dict() for exclusion in self.exclusions],
        }

    @property
    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()
