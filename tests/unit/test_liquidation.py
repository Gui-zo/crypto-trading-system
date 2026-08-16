"""Phase-5 liquidation arithmetic: exact tiers, the fixed point, and the invariant.

ADR-0009: the maintenance-margin tiers must not be approximated, and the
property test over price paths is the most important test in this repository.
The property here is the one the whole risk engine will rest on — *the margin
balance is exactly exhausted at the computed liquidation price* — checked
directly against Binance's own maintenance-margin definition rather than against
a reimplementation of the same formula.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from domain.instrument import MaintenanceMarginTier, MarginSchedule
from domain.liquidation import (
    LiquidationError,
    Position,
    PositionSide,
    liquidation_distance_fraction,
    liquidation_price,
    tier_for,
)


def tier(
    bracket: int,
    floor: str,
    cap: str,
    mmr: str,
    cumulative: str,
    leverage: int = 125,
) -> MaintenanceMarginTier:
    return MaintenanceMarginTier(
        bracket=bracket,
        initial_leverage=leverage,
        notional_floor=Decimal(floor),
        notional_cap=Decimal(cap),
        maintenance_margin_ratio=Decimal(mmr),
        cumulative=Decimal(cumulative),
    )


#: Shaped like a real BTCUSDT bracket table: rising MMR, falling max leverage,
#: contiguous notional bands, cumulative deduction rising with the tier.
SCHEDULE = MarginSchedule(
    symbol="BTCUSDT",
    tiers=(
        tier(1, "0", "50000", "0.004", "0", leverage=125),
        tier(2, "50000", "500000", "0.005", "50", leverage=100),
        tier(3, "500000", "5000000", "0.010", "2550", leverage=50),
        tier(4, "5000000", "20000000", "0.025", "77550", leverage=20),
        tier(5, "20000000", "100000000", "0.050", "577550", leverage=10),
    ),
)


def short(quantity: str, entry: str, wallet: str) -> Position:
    return Position(
        side=PositionSide.SHORT,
        quantity=Decimal(quantity),
        entry_price=Decimal(entry),
        wallet_balance=Decimal(wallet),
    )


def long(quantity: str, entry: str, wallet: str) -> Position:
    return Position(
        side=PositionSide.LONG,
        quantity=Decimal(quantity),
        entry_price=Decimal(entry),
        wallet_balance=Decimal(wallet),
    )


def margin_balance_at(position: Position, price: Decimal) -> Decimal:
    """Wallet plus unrealized PnL — the venue's left-hand side."""
    if position.side is PositionSide.SHORT:
        pnl = position.quantity * (position.entry_price - price)
    else:
        pnl = position.quantity * (price - position.entry_price)
    return position.wallet_balance + pnl


def maintenance_margin_at(
    position: Position, price: Decimal, schedule: MarginSchedule = SCHEDULE
) -> Decimal:
    """Binance's requirement: notional * mmr - maintenance amount."""
    notional = position.quantity * price
    applicable = tier_for(schedule, notional)
    return notional * applicable.maintenance_margin_ratio - applicable.cumulative


# --- tier selection ----------------------------------------------------------


def test_a_notional_on_a_boundary_belongs_to_the_lower_bracket() -> None:
    assert tier_for(SCHEDULE, Decimal("50000")).bracket == 1
    assert tier_for(SCHEDULE, Decimal("50000.01")).bracket == 2


def test_a_notional_above_the_top_bracket_fails_closed() -> None:
    """The venue would not accept the size, so neither may we."""
    with pytest.raises(LiquidationError, match="exceeds the top margin bracket"):
        tier_for(SCHEDULE, Decimal("100000000.01"))


def test_a_negative_notional_is_refused() -> None:
    with pytest.raises(LiquidationError):
        tier_for(SCHEDULE, Decimal("-1"))


# --- the arithmetic ----------------------------------------------------------


def test_a_short_liquidates_above_entry_and_a_long_below() -> None:
    rising = liquidation_price(short("1", "50000", "5000"), SCHEDULE)
    falling = liquidation_price(long("1", "50000", "5000"), SCHEDULE)

    assert rising is not None and rising > Decimal("50000")
    assert falling is not None and falling < Decimal("50000")


def test_more_margin_moves_liquidation_further_away() -> None:
    thin = liquidation_price(short("1", "50000", "2500"), SCHEDULE)
    thick = liquidation_price(short("1", "50000", "10000"), SCHEDULE)

    assert thin is not None and thick is not None
    assert thick > thin


def test_a_long_with_margin_covering_the_whole_notional_cannot_be_liquidated() -> None:
    """Unlevered spot has no liquidation price, and inventing one would be a lie."""
    assert liquidation_price(long("1", "50000", "60000"), SCHEDULE) is None


def test_a_short_always_has_a_liquidation_price_however_much_margin() -> None:
    """A short's loss is unbounded above, so no amount of margin removes the risk."""
    price = liquidation_price(short("1", "50000", "10000000"), SCHEDULE)

    assert price is not None and price > Decimal("50000")


def test_the_bracket_is_chosen_at_the_liquidation_price_not_at_entry() -> None:
    """A position entered inside one bracket can liquidate inside a higher one.

    Selecting the tier from the entry notional and stopping would use too low a
    maintenance ratio and report liquidation as further away than it is — an
    error in the dangerous direction.
    """
    position = short("1", "49000", "30000")  # entry notional 49k -> bracket 1

    price = liquidation_price(position, SCHEDULE)

    assert price is not None
    entry_bracket = tier_for(SCHEDULE, position.entry_notional).bracket
    settled_bracket = tier_for(SCHEDULE, position.quantity * price).bracket
    assert entry_bracket == 1
    assert settled_bracket == 2
    # And the answer is self-consistent under the bracket it actually lands in.
    assert margin_balance_at(position, price) == pytest.approx(
        maintenance_margin_at(position, price), rel=Decimal("1e-12")
    )


def test_a_size_above_the_top_bracket_is_refused_rather_than_sized() -> None:
    with pytest.raises(LiquidationError, match="top margin bracket"):
        liquidation_price(short("10000", "50000", "1000000"), SCHEDULE)


# --- distance ----------------------------------------------------------------


def test_distance_is_measured_in_the_direction_that_hurts() -> None:
    rising = liquidation_distance_fraction(short("1", "50000", "5000"), SCHEDULE)
    falling = liquidation_distance_fraction(long("1", "50000", "5000"), SCHEDULE)

    assert rising is not None and rising > 0
    assert falling is not None and falling > 0


def test_distance_is_measured_from_the_mark_not_the_entry_when_given() -> None:
    """Risk is about where the price is now, not where the position was opened."""
    position = short("1", "50000", "5000")

    at_entry = liquidation_distance_fraction(position, SCHEDULE)
    after_move = liquidation_distance_fraction(position, SCHEDULE, mark_price=Decimal("53000"))

    assert at_entry is not None and after_move is not None
    assert after_move < at_entry


def test_a_position_already_past_liquidation_reports_no_remaining_distance() -> None:
    position = short("1", "50000", "5000")

    distance = liquidation_distance_fraction(position, SCHEDULE, mark_price=Decimal("500000"))

    assert distance == Decimal(0)


# --- the property ------------------------------------------------------------


@settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(
    quantity=st.decimals(min_value=Decimal("0.001"), max_value=Decimal("50"), places=3),
    entry=st.decimals(min_value=Decimal("100"), max_value=Decimal("120000"), places=2),
    wallet=st.decimals(min_value=Decimal("1"), max_value=Decimal("2000000"), places=2),
    is_short=st.booleans(),
)
def test_margin_is_exactly_exhausted_at_the_liquidation_price(
    quantity: Decimal, entry: Decimal, wallet: Decimal, is_short: bool
) -> None:
    """The invariant, checked against the venue's own definition.

    At the computed price the margin balance must equal the maintenance
    requirement — the instant the venue closes the position. Checked
    independently of the derivation by evaluating both sides directly.
    """
    side = PositionSide.SHORT if is_short else PositionSide.LONG
    position = Position(side=side, quantity=quantity, entry_price=entry, wallet_balance=wallet)
    assume(position.entry_notional <= SCHEDULE.tiers[-1].notional_cap)

    try:
        price = liquidation_price(position, SCHEDULE)
    except LiquidationError:
        # Refusing is always an acceptable outcome; inventing a number is not.
        return
    if price is None:
        return
    assume(price * quantity <= SCHEDULE.tiers[-1].notional_cap)

    balance = margin_balance_at(position, price)
    requirement = maintenance_margin_at(position, price)

    assert balance == pytest.approx(requirement, abs=Decimal("1e-9"), rel=Decimal("1e-12"))


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(
    quantity=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("20"), places=2),
    entry=st.decimals(min_value=Decimal("1000"), max_value=Decimal("100000"), places=2),
    wallet=st.decimals(min_value=Decimal("10"), max_value=Decimal("500000"), places=2),
)
def test_a_short_survives_every_price_strictly_inside_its_liquidation_distance(
    quantity: Decimal, entry: Decimal, wallet: Decimal
) -> None:
    """The invariant as a price path: nothing short of the liquidation price
    liquidates. This is the shape the risk engine's stress band relies on."""
    position = Position(
        side=PositionSide.SHORT, quantity=quantity, entry_price=entry, wallet_balance=wallet
    )
    assume(position.entry_notional <= SCHEDULE.tiers[-1].notional_cap)

    try:
        price = liquidation_price(position, SCHEDULE)
    except LiquidationError:
        return
    assert price is not None
    assume(price * quantity <= SCHEDULE.tiers[-1].notional_cap)
    # A position opened at or beyond its maintenance threshold has zero
    # liquidation distance, so there is no interior to survive. That case is
    # pinned separately below — the risk engine must refuse it, not size it.
    assume(price > entry)

    for fraction in ("0.10", "0.50", "0.90", "0.99"):
        probe = entry + (price - entry) * Decimal(fraction)
        assume(probe * quantity <= SCHEDULE.tiers[-1].notional_cap)
        assert margin_balance_at(position, probe) > maintenance_margin_at(position, probe)


def test_a_position_opened_at_its_maintenance_threshold_has_zero_distance() -> None:
    """Hypothesis found this: 2.5 units at 1000 with exactly 10 of margin sits on
    the maintenance requirement (2500 * 0.004 = 10) the instant it opens.

    The arithmetic is right — liquidation price equals entry — and the safety
    consequence belongs to the risk engine: zero distance can never clear a
    stress band, so such a size must be refused rather than opened.
    """
    position = short("2.5", "1000", "10")

    price = liquidation_price(position, SCHEDULE)
    distance = liquidation_distance_fraction(position, SCHEDULE)

    assert price == Decimal("1000")
    assert distance == Decimal(0)
    assert margin_balance_at(position, Decimal("1000")) == maintenance_margin_at(
        position, Decimal("1000")
    )
