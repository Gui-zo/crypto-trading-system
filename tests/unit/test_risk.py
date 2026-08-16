"""Phase-5 carry economics and the sizing engine.

The property at the bottom is the one ADR-0009 calls the most important test in
this repository, in its final form: **anything the engine approves survives the
whole stress band without liquidation**. It is checked by walking the price path
and evaluating the venue's own maintenance requirement, not by re-deriving the
sizing formula.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from domain.carry import CarryInputs, FeeSchedule, breakeven_funding_rate, estimate_carry
from domain.instrument import (
    ContractType,
    FundingSchedule,
    InstrumentRef,
    InstrumentSpecification,
    InstrumentStatus,
    MaintenanceMarginTier,
    MarginSchedule,
    PriceFilter,
    QuantityFilter,
    VenueEnvironment,
    VenueScope,
)
from domain.liquidation import Position, PositionSide, liquidation_distance_fraction
from domain.risk import (
    BookExposure,
    Constraint,
    RiskError,
    RiskLimits,
    SizingRequest,
    group_key,
    max_quantity_for_stress_band,
    size_position,
)

SCHEDULE = MarginSchedule(
    symbol="BTCUSDT",
    tiers=(
        MaintenanceMarginTier(1, 125, Decimal(0), Decimal("300000"), Decimal("0.004"), Decimal(0)),
        MaintenanceMarginTier(
            2, 100, Decimal("300000"), Decimal("800000"), Decimal("0.005"), Decimal("300")
        ),
        MaintenanceMarginTier(
            3, 75, Decimal("800000"), Decimal("3000000"), Decimal("0.0065"), Decimal("1500")
        ),
        MaintenanceMarginTier(
            4, 50, Decimal("3000000"), Decimal("12000000"), Decimal("0.01"), Decimal("12000")
        ),
    ),
)


def schedule_for(symbol: str) -> MarginSchedule:
    return MarginSchedule(symbol=symbol, tiers=SCHEDULE.tiers)


def specification(symbol: str = "BTCUSDT", base: str = "BTC") -> InstrumentSpecification:
    scope = VenueScope(venue="BINANCE", environment=VenueEnvironment.PRODUCTION)
    return InstrumentSpecification(
        instrument=InstrumentRef(scope=scope, symbol=symbol, market="usdm"),
        status=InstrumentStatus.TRADING,
        contract_type=ContractType.PERPETUAL,
        base_asset=base,
        quote_asset="USDT",
        margin_asset="USDT",
        price_filter=PriceFilter(
            tick_size=Decimal("0.10"), min_price=Decimal("0.10"), max_price=Decimal("1000000")
        ),
        quantity_filter=QuantityFilter(
            step_size=Decimal("0.001"),
            min_quantity=Decimal("0.001"),
            max_quantity=Decimal("1000"),
        ),
        minimum_notional=Decimal("100"),
        funding_schedule=FundingSchedule(
            interval_hours=8,
            rate_cap=Decimal("0.003"),
            rate_floor=Decimal("-0.003"),
        ),
        margin_schedule=schedule_for(symbol),
        liquidation_fee=Decimal("0.015"),
    )


def fees(bps: str = "4") -> FeeSchedule:
    value = Decimal(bps)
    return FeeSchedule(
        perp_entry_bps=value, perp_exit_bps=value, spot_entry_bps=value, spot_exit_bps=value
    )


def carry_inputs(rate: str = "0.0001", settlements: int = 3, **over: object) -> CarryInputs:
    base: dict[str, object] = {
        "expected_funding_rate": Decimal(rate),
        "settlements": settlements,
        "slippage_bps": Decimal("2"),
        "basis_cost_bps": Decimal("1"),
        "capital_cost_bps": Decimal("0"),
        "fees": fees(),
        "perp_margin_fraction": Decimal("0.5"),
        "margin_buffer_fraction": Decimal("0.25"),
    }
    base.update(over)
    return CarryInputs(**base)  # type: ignore[arg-type]


def request(**over: object) -> SizingRequest:
    base: dict[str, object] = {
        "specification": specification(),
        "mark_price": Decimal("50000"),
        "forecast_volatility": Decimal("0.03"),
        "available_capital": Decimal("100000"),
        "net_carry_bps": Decimal("5"),
    }
    base.update(over)
    return SizingRequest(**base)  # type: ignore[arg-type]


# --- carry economics ---------------------------------------------------------


def test_a_round_trip_costs_four_fills_not_two() -> None:
    """Costing only the entry understates the hurdle by half."""
    assert fees("4").round_trip_bps == Decimal("16")


def test_carry_on_capital_is_lower_than_carry_on_notional() -> None:
    """The delta-neutral position funds spot *and* margins the perp, so the
    denominator is bigger than the notional. Quoting on notional flatters it.

    Checked on a profitable case, because for a *loss* the bigger denominator
    works the other way and would make the assertion accidentally pass."""
    estimate = estimate_carry(carry_inputs(settlements=60))

    assert estimate.net_bps_on_notional > 0
    assert estimate.capital_multiple == Decimal("1.75")
    assert estimate.net_bps_on_capital < estimate.net_bps_on_notional


def test_every_cost_component_is_reported_not_just_the_total() -> None:
    estimate = estimate_carry(carry_inputs())

    assert estimate.gross_funding_bps == Decimal("3")  # 1 bp * 3 settlements
    assert estimate.total_cost_bps == Decimal("19")  # 16 fees + 2 slippage + 1 basis
    assert estimate.net_bps_on_notional == Decimal("-16")


def test_a_realistic_case_can_be_negative_and_says_so() -> None:
    """Three settlements at 1 bp does not pay a 16 bp round trip. This is the
    kill-criterion arithmetic working, not a bug."""
    estimate = estimate_carry(carry_inputs())

    assert not estimate.beats(Decimal(0))


def test_holding_longer_earns_more_funding_against_one_round_trip() -> None:
    brief = estimate_carry(carry_inputs(settlements=3))
    patient = estimate_carry(carry_inputs(settlements=60))

    assert brief.net_bps_on_capital < 0 < patient.net_bps_on_capital


def test_matching_the_benchmark_is_not_beating_it() -> None:
    estimate = estimate_carry(carry_inputs(settlements=19))  # 19 bps gross vs 19 bps cost

    assert estimate.net_bps_on_notional == Decimal(0)
    assert not estimate.beats(Decimal(0))


def test_breakeven_says_what_the_forecast_would_have_to_be() -> None:
    """A refusal is more useful as 'needs 6.33 bps, forecast 1' than as 'no'."""
    assert breakeven_funding_rate(carry_inputs()) == Decimal("19") / Decimal(3)


def test_a_horizon_with_no_settlements_has_no_breakeven() -> None:
    assert breakeven_funding_rate(carry_inputs(settlements=0)) is None


def test_negative_costs_are_refused() -> None:
    with pytest.raises(Exception, match="costs are costs"):
        carry_inputs(slippage_bps=Decimal("-1"))


# --- the stress band ---------------------------------------------------------


def test_the_floor_wins_when_the_vol_model_is_optimistic() -> None:
    """A vol model that underestimates is exactly what a floor exists to survive."""
    limits = RiskLimits()

    assert limits.stress_band(Decimal("0.001")) == Decimal("0.15")
    assert limits.stress_band(Decimal("0.10")) == Decimal("0.30")


def test_an_unfloored_band_is_refused_at_construction() -> None:
    with pytest.raises(RiskError, match="unfloored band is not a band"):
        RiskLimits(stress_band_floor=Decimal(0))


def test_a_richer_margin_fraction_permits_more_quantity_at_the_same_band() -> None:
    thin = max_quantity_for_stress_band(
        SCHEDULE,
        entry_price=Decimal("50000"),
        margin_fraction=Decimal("0.25"),
        required_distance=Decimal("0.30"),
    )
    thick = max_quantity_for_stress_band(
        SCHEDULE,
        entry_price=Decimal("50000"),
        margin_fraction=Decimal("0.50"),
        required_distance=Decimal("0.30"),
    )

    assert thick > thin


def test_a_band_wider_than_the_margin_fraction_permits_nothing_in_the_first_tier() -> None:
    """The margin fraction alone decides survivability. The first tier gives no
    maintenance-amount credit, so nothing buys room there."""
    survivable = max_quantity_for_stress_band(
        SCHEDULE,
        entry_price=Decimal("50000"),
        margin_fraction=Decimal("0.50"),
        required_distance=Decimal("0.40"),
    )
    beyond = max_quantity_for_stress_band(
        SCHEDULE,
        entry_price=Decimal("50000"),
        margin_fraction=Decimal("0.10"),
        required_distance=Decimal("0.60"),
    )

    assert survivable > 0
    assert beyond == 0


# --- sizing and refusal ------------------------------------------------------


def test_an_approved_size_reports_its_full_working() -> None:
    decision = size_position(request(), RiskLimits())

    assert decision.approved
    assert decision.quantity > 0
    assert decision.capital_required <= Decimal("100000")
    assert len(decision.outcomes) >= 5
    assert "bound by" in decision.explanation


def test_non_positive_carry_is_refused_however_safe_the_size() -> None:
    """The risk engine can always refuse. It exists to say no."""
    decision = size_position(request(net_carry_bps=Decimal("-1")), RiskLimits())

    assert not decision.approved
    assert decision.binding is Constraint.NEGATIVE_CARRY
    assert decision.quantity == 0


def test_the_group_cap_binds_on_the_asset_not_the_instrument() -> None:
    """A BTC perp short and a BTC spot long are one ASSET:BTC exposure."""
    decision = size_position(
        request(),
        RiskLimits(max_group_notional=Decimal("60000")),
        BookExposure(group_notional=Decimal("55000")),
    )

    assert decision.binding is Constraint.GROUP_NOTIONAL
    assert "ASSET:BTC" in decision.explanation


def test_group_key_is_the_underlying() -> None:
    assert group_key(specification("BTCUSDT", "BTC")) == "ASSET:BTC"
    assert group_key(specification("ETHUSDT", "ETH")) == "ASSET:ETH"


def test_a_full_book_refuses_rather_than_sizing_to_zero_silently() -> None:
    decision = size_position(
        request(),
        RiskLimits(max_total_notional=Decimal("10000")),
        BookExposure(total_notional=Decimal("10000")),
    )

    assert not decision.approved
    assert decision.quantity == 0


def test_a_size_below_the_venue_minimum_is_refused_not_rounded_up() -> None:
    decision = size_position(request(available_capital=Decimal("100")), RiskLimits())

    assert not decision.approved
    assert decision.binding in {Constraint.MINIMUM_NOTIONAL, Constraint.LOT_SIZE}


def test_quantity_is_quantized_down_to_the_lot_size() -> None:
    """Rounding a cap up defeats the cap (ADR-0011)."""
    decision = size_position(request(), RiskLimits())

    step = specification().quantity_filter.step_size
    assert decision.quantity % step == 0


def test_a_tighter_leverage_cap_reduces_the_size() -> None:
    loose = size_position(request(), RiskLimits(max_effective_leverage=Decimal("2")))
    tight = size_position(request(), RiskLimits(max_effective_leverage=Decimal("1")))

    assert tight.quantity < loose.quantity


def test_the_buffer_is_reserved_and_never_allocated() -> None:
    limits = RiskLimits(margin_buffer_fraction=Decimal("0.25"))
    decision = size_position(request(), limits)

    assert decision.margin_buffer == decision.notional * Decimal("0.25")
    assert decision.capital_required == decision.notional * Decimal("1.75")


# --- the invariant -----------------------------------------------------------


@settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(
    price=st.decimals(min_value=Decimal("100"), max_value=Decimal("120000"), places=2),
    volatility=st.decimals(min_value=Decimal("0"), max_value=Decimal("0.40"), places=4),
    capital=st.decimals(min_value=Decimal("500"), max_value=Decimal("5000000"), places=2),
    leverage=st.sampled_from([Decimal("1"), Decimal("1.5"), Decimal("2")]),
)
def test_an_approved_position_survives_its_entire_stress_band(
    price: Decimal, volatility: Decimal, capital: Decimal, leverage: Decimal
) -> None:
    """ADR-0009's invariant, end to end.

    Whatever the engine approves must still be alive at every price inside the
    stress band — checked against the liquidation distance computed from the
    venue's own tier table, not from the sizing arithmetic that produced it.
    """
    limits = RiskLimits(max_effective_leverage=leverage)
    decision = size_position(
        request(
            mark_price=price,
            forecast_volatility=volatility,
            available_capital=capital,
            net_carry_bps=Decimal("5"),
        ),
        limits,
    )
    assume(decision.approved)

    position = Position(
        side=PositionSide.SHORT,
        quantity=decision.quantity,
        entry_price=price,
        wallet_balance=decision.perp_margin,
    )
    distance = liquidation_distance_fraction(position, SCHEDULE)

    assert distance is not None
    # The whole band must fit inside the distance to liquidation.
    assert distance >= decision.stress_band


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(
    capital=st.decimals(min_value=Decimal("500"), max_value=Decimal("2000000"), places=2),
    volatility=st.decimals(min_value=Decimal("0"), max_value=Decimal("0.30"), places=4),
)
def test_effective_leverage_never_exceeds_the_cap(capital: Decimal, volatility: Decimal) -> None:
    limits = RiskLimits(max_effective_leverage=Decimal("2"))
    decision = size_position(
        request(available_capital=capital, forecast_volatility=volatility), limits
    )
    assume(decision.approved)

    effective = decision.notional / decision.perp_margin

    assert effective <= Decimal("2")


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(
    capital=st.decimals(min_value=Decimal("500"), max_value=Decimal("2000000"), places=2),
    used=st.decimals(min_value=Decimal("0"), max_value=Decimal("400000"), places=2),
)
def test_an_approved_size_never_breaches_a_cap_it_was_given(
    capital: Decimal, used: Decimal
) -> None:
    cap = Decimal("500000")
    decision = size_position(
        request(available_capital=capital),
        RiskLimits(max_total_notional=cap),
        BookExposure(total_notional=used),
    )
    assume(decision.approved)

    assert used + decision.notional <= cap
    assert decision.capital_required <= capital


def test_a_satisfied_gate_is_never_reported_as_the_binding_constraint() -> None:
    """Carry decides *whether* to trade, never *how much*.

    Letting it into the minimum made a profitable trade report "bound by
    NEGATIVE_CARRY", which is the opposite of an actionable explanation.
    """
    decision = size_position(request(net_carry_bps=Decimal("40")), RiskLimits())

    assert decision.approved
    assert decision.binding is not Constraint.NEGATIVE_CARRY
    assert "NEGATIVE_CARRY" not in decision.explanation
