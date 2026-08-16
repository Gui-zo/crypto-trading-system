"""Exact liquidation prices from Binance's maintenance-margin tiers.

ADR-0009 replaces the sibling repo's ``max_loss = cost + fee`` invariant with a
liquidation-distance invariant, and says the tiers must not be approximated.
This module is the arithmetic that makes that possible: given a position and the
symbol's own versioned tier table, at what price does the venue close it.

The derivation, for a position of ``quantity`` at ``entry_price`` with
``wallet_balance`` of margin behind it. Liquidation happens when the margin
balance falls to the maintenance margin:

    wallet + unrealized_pnl <= notional * mmr - maintenance_amount

For a **short**, ``unrealized_pnl = quantity * (entry - price)`` and
``notional = quantity * price``, which rearranges to

    price = (wallet + quantity * entry + maintenance_amount)
            / (quantity * (1 + mmr))

For a **long** the pnl sign flips and the denominator becomes ``1 - mmr``. Both
are exact in ``Decimal``; nothing here is a linearisation.

**The tier depends on the notional, and the notional depends on the price**, so
selecting a tier from the entry notional and stopping is wrong whenever
liquidation would land in a different bracket. :func:`liquidation_price`
resolves that fixed point and fails closed if it cannot.

A short's loss is unbounded above, so its liquidation price always exists. A
long's does not: below a certain leverage the position simply cannot be
liquidated, and this module returns ``None`` rather than inventing a number.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from domain.errors import DomainError
from domain.instrument import MaintenanceMarginTier, MarginSchedule

#: A tier search that never settles means the tier table and the price disagree
#: about which bracket applies. Bounded so a pathological table cannot spin.
_MAX_TIER_ITERATIONS = 16


class LiquidationError(DomainError):
    """The tier table cannot support an exact liquidation price for this position."""


class PositionSide(StrEnum):
    SHORT = "SHORT"
    LONG = "LONG"


@dataclass(frozen=True, slots=True)
class Position:
    """A single leg, with the margin actually standing behind it.

    ``wallet_balance`` is the margin attributable to this position — isolated
    margin, or the cross-wallet equity a caller has decided to attribute. It is
    an input rather than something inferred, because guessing how much of a
    shared wallet protects one leg is exactly the mistake ADR-0009 warns about.
    """

    side: PositionSide
    quantity: Decimal
    entry_price: Decimal
    wallet_balance: Decimal

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise LiquidationError("position quantity must be positive")
        if self.entry_price <= 0:
            raise LiquidationError("entry price must be positive")
        if self.wallet_balance < 0:
            raise LiquidationError("wallet balance cannot be negative")
        for name in ("quantity", "entry_price", "wallet_balance"):
            value: Decimal = getattr(self, name)
            if not value.is_finite():
                raise LiquidationError(f"{name} must be finite")

    @property
    def entry_notional(self) -> Decimal:
        return self.quantity * self.entry_price


def tier_for(schedule: MarginSchedule, notional: Decimal) -> MaintenanceMarginTier:
    """The tier covering ``notional``. Fails closed above the top bracket.

    Binance's brackets are ``(floor, cap]`` in effect — a notional exactly on a
    boundary belongs to the lower bracket, because the tiers are validated as
    contiguous with ``current.floor == previous.cap``.
    """
    if notional < 0:
        raise LiquidationError("notional cannot be negative")
    for tier in schedule.tiers:
        if notional <= tier.notional_cap:
            return tier
    raise LiquidationError(
        f"{schedule.symbol}: notional {notional} exceeds the top margin bracket "
        f"{schedule.tiers[-1].notional_cap}; the venue would not accept this size"
    )


def _price_for_tier(position: Position, tier: MaintenanceMarginTier) -> Decimal | None:
    """Liquidation price assuming ``tier`` applies. ``None`` if unreachable."""
    mmr = tier.maintenance_margin_ratio
    numerator_common = position.wallet_balance + tier.cumulative
    if position.side is PositionSide.SHORT:
        denominator = position.quantity * (Decimal(1) + mmr)
        numerator = numerator_common + position.quantity * position.entry_price
        return numerator / denominator
    # Long: liquidation exists only while (1 - mmr) * notional exceeds the margin.
    denominator = position.quantity * (Decimal(1) - mmr)
    if denominator <= 0:
        return None
    numerator = position.quantity * position.entry_price - numerator_common
    price = numerator / denominator
    return price if price > 0 else None


def liquidation_price(position: Position, schedule: MarginSchedule) -> Decimal | None:
    """The exact price at which the venue liquidates ``position``.

    Returns ``None`` only for a long that cannot be liquidated at any positive
    price — a short always can, because its loss is unbounded above.

    Resolves the tier/notional fixed point: the bracket is chosen from the
    notional *at the liquidation price*, not at entry, because that is the
    notional the venue will be looking at when it decides.
    """
    tier = tier_for(schedule, position.entry_notional)
    seen: set[int] = set()
    for _ in range(_MAX_TIER_ITERATIONS):
        price = _price_for_tier(position, tier)
        if price is None:
            return None
        settled = tier_for(schedule, position.quantity * price)
        if settled.bracket == tier.bracket:
            return price
        if settled.bracket in seen:
            # Two brackets each point at the other. Refusing is the only honest
            # answer: there is no price consistent with the table.
            raise LiquidationError(
                f"{schedule.symbol}: liquidation price oscillates between margin "
                f"brackets {tier.bracket} and {settled.bracket}; no consistent price exists"
            )
        seen.add(tier.bracket)
        tier = settled
    raise LiquidationError(
        f"{schedule.symbol}: margin bracket selection did not settle within "
        f"{_MAX_TIER_ITERATIONS} iterations"
    )


def liquidation_distance_fraction(
    position: Position, schedule: MarginSchedule, *, mark_price: Decimal | None = None
) -> Decimal | None:
    """How far the price may move against the position before liquidation.

    Expressed as a fraction of the reference price, always non-negative and
    measured in the direction that hurts: **up** for a short, **down** for a
    long. ``None`` when the position cannot be liquidated.

    This is the quantity ADR-0009's stress band is compared against, which is why
    it is a fraction rather than a price — a stress band is a percentage move.
    """
    reference = mark_price if mark_price is not None else position.entry_price
    if reference <= 0:
        raise LiquidationError("reference price must be positive")
    price = liquidation_price(position, schedule)
    if price is None:
        return None
    distance = price - reference if position.side is PositionSide.SHORT else reference - price
    # A position already past its liquidation price has no remaining distance.
    return max(Decimal(0), distance) / reference
