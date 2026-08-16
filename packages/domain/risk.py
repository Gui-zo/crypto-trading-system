"""The deterministic risk engine: it decides, and it explains every refusal.

ADR-0009's invariant, implemented:

    For every price path within a configured stress band, no leg may be
    liquidated, and total loss may not exceed the configured risk unit.

The shape of this module is deliberate. Every limit computes **the largest
quantity it would permit**, independently, and the decision is the minimum of
those with the name of whichever bound it. That is what makes a refusal
explainable — "this was refused" is not an answer a human can act on, but "the
stress band allowed 0.42 and the group cap allowed 0.18" is.

Three rules that are not negotiable here:

* **Nothing is a model output.** Probabilities come from elsewhere; this module
  only ever *rejects*. It cannot enlarge a position.
* **Round down when capping** (ADR-0011). Rounding a cap up defeats the cap.
* **The correlation group is the underlying asset, not the instrument**
  (ADR-0009). A BTC perp short and a BTC spot long are one ``ASSET:BTC``
  exposure. Counting them separately is how a hedged book turns out to be twice
  the size anyone intended.

This module never sizes *up* to a target. It finds a ceiling and quantizes down
to the venue's lot size; if what remains is below the venue's minimum notional,
the trade is refused rather than rounded into existence.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from domain.errors import DomainError
from domain.instrument import InstrumentSpecification, MarginSchedule
from domain.liquidation import (
    Position,
    PositionSide,
    liquidation_distance_fraction,
    tier_for,
)
from domain.precision import quantize_down

#: ADR-0009: start conservative and versioned. The gate is on *effective*
#: leverage after sizing, never on the account's leverage setting.
DEFAULT_MAX_EFFECTIVE_LEVERAGE = Decimal("2")

#: Survive a 3-sigma move over the intended holding horizon.
DEFAULT_STRESS_SIGMA_MULTIPLE = Decimal("3")

#: ...and never trust a vol model below this, whatever it says. A vol model that
#: underestimates is precisely what a stress band exists to survive.
DEFAULT_STRESS_BAND_FLOOR = Decimal("0.15")

#: How many lot steps the post-sizing verification may walk down before giving
#: up. A handful covers Decimal rounding; needing more would mean the closed-form
#: solve disagrees with the liquidation module, which is a defect, not a nudge.
_MAX_VERIFY_STEPS = 8

#: Unencumbered margin on the futures wallet that sizing may never allocate, as
#: a fraction of perp notional. An adverse move should trigger a top-up
#: decision, not a liquidation.
DEFAULT_MARGIN_BUFFER_FRACTION = Decimal("0.25")


class RiskError(DomainError):
    """Inputs that cannot produce a safe size. Always fails closed."""


class Constraint(StrEnum):
    """Every limit that can bind. The refusal names one of these."""

    STRESS_BAND = "STRESS_BAND"
    EFFECTIVE_LEVERAGE = "EFFECTIVE_LEVERAGE"
    MARGIN_BUFFER = "MARGIN_BUFFER"
    INSTRUMENT_NOTIONAL = "INSTRUMENT_NOTIONAL"
    TOTAL_NOTIONAL = "TOTAL_NOTIONAL"
    GROUP_NOTIONAL = "GROUP_NOTIONAL"
    AVAILABLE_CAPITAL = "AVAILABLE_CAPITAL"
    MARGIN_BRACKET_CEILING = "MARGIN_BRACKET_CEILING"
    MINIMUM_NOTIONAL = "MINIMUM_NOTIONAL"
    LOT_SIZE = "LOT_SIZE"
    NEGATIVE_CARRY = "NEGATIVE_CARRY"


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Versioned risk configuration. Changing one changes what is permitted."""

    max_effective_leverage: Decimal = DEFAULT_MAX_EFFECTIVE_LEVERAGE
    stress_sigma_multiple: Decimal = DEFAULT_STRESS_SIGMA_MULTIPLE
    stress_band_floor: Decimal = DEFAULT_STRESS_BAND_FLOOR
    margin_buffer_fraction: Decimal = DEFAULT_MARGIN_BUFFER_FRACTION
    max_instrument_notional: Decimal | None = None
    max_total_notional: Decimal | None = None
    max_group_notional: Decimal | None = None

    def __post_init__(self) -> None:
        if self.max_effective_leverage <= 0:
            raise RiskError("maximum effective leverage must be positive")
        if self.stress_sigma_multiple <= 0:
            raise RiskError("stress sigma multiple must be positive")
        if self.stress_band_floor <= 0:
            raise RiskError("stress band floor must be positive; an unfloored band is not a band")
        if self.margin_buffer_fraction < 0:
            raise RiskError("margin buffer fraction cannot be negative")
        for name in ("max_instrument_notional", "max_total_notional", "max_group_notional"):
            value: Decimal | None = getattr(self, name)
            if value is not None and value <= 0:
                raise RiskError(f"{name} must be positive when set")

    def stress_band(self, forecast_volatility: Decimal) -> Decimal:
        """The move the position must survive: the greater of model and floor."""
        if forecast_volatility < 0:
            raise RiskError("forecast volatility cannot be negative")
        return max(self.stress_sigma_multiple * forecast_volatility, self.stress_band_floor)


@dataclass(frozen=True, slots=True)
class BookExposure:
    """What is already on. Group keys are ``ASSET:<base>`` (ADR-0009)."""

    total_notional: Decimal = Decimal(0)
    instrument_notional: Decimal = Decimal(0)
    group_notional: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        for name in ("total_notional", "instrument_notional", "group_notional"):
            if getattr(self, name) < 0:
                raise RiskError(f"{name} cannot be negative")


#: An empty book. A module-level singleton because it is immutable and shared.
NO_EXPOSURE = BookExposure()


def group_key(specification: InstrumentSpecification) -> str:
    """``ASSET:BTC`` for BTCUSDT — the underlying, never the instrument."""
    return f"ASSET:{specification.base_asset.upper()}"


@dataclass(frozen=True, slots=True)
class SizingRequest:
    specification: InstrumentSpecification
    mark_price: Decimal
    #: Volatility over the intended holding horizon, as a fraction.
    forecast_volatility: Decimal
    #: Capital the caller is willing to commit across both legs.
    available_capital: Decimal
    #: Net carry on capital, in bps. Non-positive carry is refused outright.
    net_carry_bps: Decimal

    def __post_init__(self) -> None:
        if self.mark_price <= 0:
            raise RiskError("mark price must be positive")
        if self.available_capital < 0:
            raise RiskError("available capital cannot be negative")
        for name in ("mark_price", "forecast_volatility", "available_capital", "net_carry_bps"):
            value: Decimal = getattr(self, name)
            if not value.is_finite():
                raise RiskError(f"{name} must be finite")


@dataclass(frozen=True, slots=True)
class ConstraintOutcome:
    """What one limit permitted, and why."""

    constraint: Constraint
    permitted_quantity: Decimal
    detail: str


@dataclass(frozen=True, slots=True)
class SizingDecision:
    """The engine's answer, with the full working shown."""

    approved: bool
    quantity: Decimal
    notional: Decimal
    perp_margin: Decimal
    margin_buffer: Decimal
    capital_required: Decimal
    stress_band: Decimal
    binding: Constraint
    outcomes: tuple[ConstraintOutcome, ...]

    @property
    def explanation(self) -> str:
        binding_outcome = next(item for item in self.outcomes if item.constraint is self.binding)
        verdict = "approved" if self.approved else "refused"
        return f"{verdict}: bound by {self.binding.value} — {binding_outcome.detail}"


def max_quantity_for_stress_band(
    schedule: MarginSchedule,
    *,
    entry_price: Decimal,
    margin_fraction: Decimal,
    required_distance: Decimal,
    side: PositionSide = PositionSide.SHORT,
) -> Decimal:
    """Largest quantity whose liquidation sits at least ``required_distance`` away,
    when margin is posted **in proportion to notional**.

    The proportional form matters and is not a detail. Sizing from a fixed wallet
    and then posting ``notional * margin_fraction`` gives the position less
    margin than the solve assumed whenever the quantity is rounded down, which
    puts liquidation nearer than the band — the error Hypothesis found. Solving
    with ``wallet = quantity * price * margin_fraction`` substituted in removes
    the inconsistency instead of patching it.

    For a short, requiring ``(liq - entry) / entry >= d`` becomes::

        quantity * price * (factor - margin_fraction) <= maintenance_amount
        where factor = (1 + mmr) * (1 + d) - 1

    So the margin fraction alone decides whether the band is survivable at all.
    When ``factor <= margin_fraction`` the band holds at any size the tier
    covers; otherwise only the tier's maintenance-amount credit buys room, and in
    the first tier — where that credit is zero — nothing does.

    Returns zero when no positive quantity clears the band. That is a correct and
    expected outcome: some trades are simply not sizeable, and ADR-0009 says
    refusing them is right.
    """
    if entry_price <= 0:
        raise RiskError("entry price must be positive")
    if required_distance <= 0:
        raise RiskError("required distance must be positive")
    if margin_fraction <= 0:
        raise RiskError("margin fraction must be positive")

    best = Decimal(0)
    for tier in schedule.tiers:
        mmr = tier.maintenance_margin_ratio
        if side is PositionSide.SHORT:
            factor = (Decimal(1) + mmr) * (Decimal(1) + required_distance) - Decimal(1)
        else:
            factor = Decimal(1) - (Decimal(1) - mmr) * (Decimal(1) - required_distance)
        if factor <= margin_fraction:
            # The band survives anywhere this tier applies; the tier's own cap is
            # the most it can contribute.
            best = max(best, tier.notional_cap / entry_price)
            continue
        headroom = factor - margin_fraction
        candidate = tier.cumulative / (entry_price * headroom)
        if candidate <= 0:
            continue
        notional = candidate * entry_price
        # Only trust a candidate whose notional really lands in this tier;
        # otherwise its ratio and maintenance amount were the wrong ones.
        if tier.notional_floor <= notional <= tier.notional_cap:
            best = max(best, candidate)
        elif notional > tier.notional_cap:
            best = max(best, tier.notional_cap / entry_price)
    return best


def size_position(
    request: SizingRequest,
    limits: RiskLimits,
    exposure: BookExposure = NO_EXPOSURE,
) -> SizingDecision:
    """Size the perp leg of a delta-neutral carry position, or refuse it.

    Order follows ADR-0009: liquidation distance first, then leverage, then the
    buffer, then the book caps. Every limit is evaluated even when an earlier one
    already bound, because a refusal that names only the first failure hides how
    far the others were from binding.
    """
    specification = request.specification
    schedule = specification.margin_schedule
    price = request.mark_price
    band = limits.stress_band(request.forecast_volatility)

    # Capital splits across the spot leg (fully funded), the perp margin, and the
    # untouchable buffer. One unit of perp notional consumes this much capital.
    margin_fraction = Decimal(1) / limits.max_effective_leverage
    capital_multiple = Decimal(1) + margin_fraction + limits.margin_buffer_fraction
    capital_notional = request.available_capital / capital_multiple

    outcomes: list[ConstraintOutcome] = []

    # Carry is a *gate*, not a size limit: it decides whether the trade is worth
    # doing at all, and never how large it may be. Letting it into the minimum
    # would let a satisfied gate be reported as the binding constraint, which is
    # a misleading explanation — the whole reason the working is shown.
    if request.net_carry_bps <= 0:
        return _refusal(
            request,
            limits,
            band,
            outcomes,
            Constraint.NEGATIVE_CARRY,
            capital_multiple,
            f"net carry {request.net_carry_bps} bps on capital does not pay for the trade",
        )

    stress_quantity = max_quantity_for_stress_band(
        schedule,
        entry_price=price,
        margin_fraction=margin_fraction,
        required_distance=band,
    )
    outcomes.append(
        ConstraintOutcome(
            Constraint.STRESS_BAND,
            stress_quantity,
            f"surviving a {band * 100:.2f}% adverse move at {margin_fraction} margin per "
            f"unit notional",
        )
    )

    # AVAILABLE_CAPITAL, EFFECTIVE_LEVERAGE and MARGIN_BUFFER all yield the same
    # ceiling by construction — the leverage cap and the buffer are exactly what
    # decide how much notional a given capital supports. Ties resolve to the
    # first minimum, so capital is evaluated first: "you ran out of capital" is
    # the one an operator can act on.
    outcomes.append(
        ConstraintOutcome(
            Constraint.AVAILABLE_CAPITAL,
            capital_notional / price,
            f"{request.available_capital:.2f} of capital at {capital_multiple}x per unit notional",
        )
    )

    outcomes.append(
        ConstraintOutcome(
            Constraint.EFFECTIVE_LEVERAGE,
            capital_notional / price,
            f"effective leverage fixed at {limits.max_effective_leverage}x by the "
            f"{margin_fraction} margin fraction",
        )
    )

    outcomes.append(
        ConstraintOutcome(
            Constraint.MARGIN_BUFFER,
            capital_notional / price,
            f"{limits.margin_buffer_fraction} of notional held unencumbered",
        )
    )

    bracket_ceiling = schedule.tiers[-1].notional_cap / price
    outcomes.append(
        ConstraintOutcome(
            Constraint.MARGIN_BRACKET_CEILING,
            bracket_ceiling,
            f"top margin bracket caps notional at {schedule.tiers[-1].notional_cap}",
        )
    )

    for constraint, cap, already in (
        (
            Constraint.INSTRUMENT_NOTIONAL,
            limits.max_instrument_notional,
            exposure.instrument_notional,
        ),
        (Constraint.TOTAL_NOTIONAL, limits.max_total_notional, exposure.total_notional),
        (Constraint.GROUP_NOTIONAL, limits.max_group_notional, exposure.group_notional),
    ):
        if cap is None:
            continue
        headroom = max(Decimal(0), cap - already)
        outcomes.append(
            ConstraintOutcome(
                constraint,
                headroom / price,
                f"{already} of {cap} already used"
                + (
                    f" on {group_key(specification)}"
                    if constraint is Constraint.GROUP_NOTIONAL
                    else ""
                ),
            )
        )

    binding_outcome = min(outcomes, key=lambda item: item.permitted_quantity)
    ceiling = binding_outcome.permitted_quantity
    if ceiling <= 0:
        # Name the limit that actually refused, not whatever check runs next.
        return _refusal(
            request,
            limits,
            band,
            outcomes,
            binding_outcome.constraint,
            capital_multiple,
            binding_outcome.detail,
        )

    # Quantize down to the venue's lot size; rounding a cap up defeats the cap.
    quantity = quantize_down(ceiling, specification.quantity_filter.step_size)

    # Verify the chosen size against the liquidation module rather than trusting
    # the closed form that produced it. Two independent paths must agree, and
    # `Decimal` division is inexact — a margin fraction like 1/1.5 leaves the
    # algebraic answer a hair inside the band. Stepping down until the measured
    # distance really clears is defence in depth, not a rounding patch: if the
    # solve above were ever wrong, this is what would catch it.
    step = specification.quantity_filter.step_size
    for _ in range(_MAX_VERIFY_STEPS):
        if quantity <= 0:
            break
        measured = liquidation_distance_fraction(
            Position(
                side=PositionSide.SHORT,
                quantity=quantity,
                entry_price=price,
                wallet_balance=quantity * price * margin_fraction,
            ),
            schedule,
        )
        if measured is not None and measured >= band:
            break
        quantity -= step
    else:
        return _refusal(
            request,
            limits,
            band,
            outcomes,
            Constraint.STRESS_BAND,
            capital_multiple,
            f"no size within {_MAX_VERIFY_STEPS} lot steps clears a {band * 100:.2f}% band",
        )

    if quantity < specification.quantity_filter.min_quantity:
        return _refusal(
            request,
            limits,
            band,
            outcomes,
            Constraint.LOT_SIZE,
            capital_multiple,
            f"{ceiling} rounds below the venue minimum quantity "
            f"{specification.quantity_filter.min_quantity}",
        )
    notional = quantity * price
    if notional < specification.minimum_notional:
        return _refusal(
            request,
            limits,
            band,
            outcomes,
            Constraint.MINIMUM_NOTIONAL,
            capital_multiple,
            f"{notional} is below the venue minimum notional {specification.minimum_notional}",
        )
    if quantity <= 0:
        return _refusal(
            request,
            limits,
            band,
            outcomes,
            binding_outcome.constraint,
            capital_multiple,
            binding_outcome.detail,
        )
    # A size that clears every limit must still sit inside a real bracket.
    tier_for(schedule, notional)

    perp_margin = notional * margin_fraction
    buffer = notional * limits.margin_buffer_fraction
    return SizingDecision(
        approved=True,
        quantity=quantity,
        notional=notional,
        perp_margin=perp_margin,
        margin_buffer=buffer,
        capital_required=notional * capital_multiple,
        stress_band=band,
        binding=binding_outcome.constraint,
        outcomes=tuple(outcomes),
    )


def _refusal(
    request: SizingRequest,
    limits: RiskLimits,
    band: Decimal,
    outcomes: list[ConstraintOutcome],
    constraint: Constraint,
    capital_multiple: Decimal,
    detail: str,
) -> SizingDecision:
    recorded = [*outcomes, ConstraintOutcome(constraint, Decimal(0), detail)]
    return SizingDecision(
        approved=False,
        quantity=Decimal(0),
        notional=Decimal(0),
        perp_margin=Decimal(0),
        margin_buffer=Decimal(0),
        capital_required=Decimal(0),
        stress_band=band,
        binding=constraint,
        outcomes=tuple(recorded),
    )
