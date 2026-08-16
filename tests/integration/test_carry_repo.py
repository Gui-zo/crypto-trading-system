"""Phase-5 proposal persistence: refusals are evidence, not silence."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from db.carry_repo import CarryProposalRepository
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
from domain.risk import RiskLimits, SizingRequest, size_position

TIERS = (
    MaintenanceMarginTier(1, 125, Decimal(0), Decimal("300000"), Decimal("0.004"), Decimal(0)),
    MaintenanceMarginTier(
        2, 100, Decimal("300000"), Decimal("800000"), Decimal("0.005"), Decimal("300")
    ),
    MaintenanceMarginTier(
        3, 75, Decimal("800000"), Decimal("3000000"), Decimal("0.0065"), Decimal("1500")
    ),
)


def specification(symbol: str, base: str) -> InstrumentSpecification:
    scope = VenueScope(venue="BINANCE", environment=VenueEnvironment.TESTNET)
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
            interval_hours=8, rate_cap=Decimal("0.003"), rate_floor=Decimal("-0.003")
        ),
        margin_schedule=MarginSchedule(symbol=symbol, tiers=TIERS),
        liquidation_fee=Decimal("0.015"),
    )


def inputs(rate: str, settlements: int) -> CarryInputs:
    return CarryInputs(
        expected_funding_rate=Decimal(rate),
        settlements=settlements,
        slippage_bps=Decimal("3"),
        basis_cost_bps=Decimal("5"),
        capital_cost_bps=Decimal("0"),
        fees=FeeSchedule(
            perp_entry_bps=Decimal("2"),
            perp_exit_bps=Decimal("2"),
            spot_entry_bps=Decimal("10"),
            spot_exit_bps=Decimal("10"),
        ),
        perp_margin_fraction=Decimal("0.5"),
        margin_buffer_fraction=Decimal("0.25"),
    )


async def record_one(
    session: AsyncSession, *, symbol: str, rate: str, settlements: int, capital: str
) -> object:
    repository = CarryProposalRepository(session, environment="testnet")
    limits = RiskLimits()
    carry = inputs(rate, settlements)
    estimate = estimate_carry(carry)
    decision = size_position(
        SizingRequest(
            specification=specification(symbol, symbol[:3]),
            mark_price=Decimal("50000"),
            forecast_volatility=Decimal("0.03"),
            available_capital=Decimal(capital),
            net_carry_bps=estimate.net_bps_on_capital,
        ),
        limits,
    )
    return await repository.record(
        symbol=symbol,
        catalog_sha256="a" * 64,
        mark_price=Decimal("50000"),
        forecast_volatility=Decimal("0.03"),
        expected_funding_rate=Decimal(rate),
        settlements=settlements,
        estimate=estimate,
        breakeven_funding_bps=breakeven_funding_rate(carry),
        decision=decision,
        limits=limits,
    )


async def test_a_refusal_is_stored_with_its_reason(db_session: AsyncSession) -> None:
    """A book that only records what it did cannot explain why it was flat."""
    symbol = f"T{uuid.uuid4().hex[:8].upper()}USDT"

    row = await record_one(
        db_session, symbol=symbol, rate="0.00001", settlements=3, capital="100000"
    )

    assert row.approved is False  # type: ignore[attr-defined]
    assert row.quantity == 0  # type: ignore[attr-defined]
    assert row.binding_constraint == "NEGATIVE_CARRY"  # type: ignore[attr-defined]
    assert "does not pay" in row.explanation  # type: ignore[attr-defined]


async def test_an_approval_records_the_size_and_the_capital_it_consumes(
    db_session: AsyncSession,
) -> None:
    symbol = f"T{uuid.uuid4().hex[:8].upper()}USDT"

    row = await record_one(
        db_session, symbol=symbol, rate="0.0001", settlements=90, capital="100000"
    )

    assert row.approved is True  # type: ignore[attr-defined]
    assert row.quantity > 0  # type: ignore[attr-defined]
    assert row.capital_required <= Decimal("100000")  # type: ignore[attr-defined]


async def test_every_limit_is_retained_so_a_decision_can_be_re_argued(
    db_session: AsyncSession,
) -> None:
    symbol = f"T{uuid.uuid4().hex[:8].upper()}USDT"

    row = await record_one(
        db_session, symbol=symbol, rate="0.0001", settlements=90, capital="100000"
    )

    constraints = {item["constraint"] for item in row.outcomes_json}  # type: ignore[attr-defined]
    assert "STRESS_BAND" in constraints
    assert "AVAILABLE_CAPITAL" in constraints
    assert row.limits_json["max_effective_leverage"] == "2"  # type: ignore[attr-defined]


async def test_counts_and_binding_reasons_are_reported_as_deltas(
    db_session: AsyncSession,
) -> None:
    """`counts()` is a global query, so assert on before/after (limitation 18)."""
    repository = CarryProposalRepository(db_session, environment="testnet")
    before = await repository.counts()
    symbol = f"T{uuid.uuid4().hex[:8].upper()}USDT"

    await record_one(db_session, symbol=symbol, rate="0.0001", settlements=90, capital="100000")
    await record_one(db_session, symbol=symbol, rate="0.00001", settlements=3, capital="100000")
    after = await repository.counts()

    assert after["proposals"] - before["proposals"] == 2
    assert after["approved"] - before["approved"] == 1
    assert dict(await repository.binding_constraint_counts())


async def test_proposals_are_environment_scoped(db_session: AsyncSession) -> None:
    symbol = f"T{uuid.uuid4().hex[:8].upper()}USDT"
    await record_one(db_session, symbol=symbol, rate="0.0001", settlements=90, capital="100000")

    other = CarryProposalRepository(db_session, environment="production")
    visible = [item for item in await other.latest(limit=200) if item.symbol == symbol]

    assert visible == []
