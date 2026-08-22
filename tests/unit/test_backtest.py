"""Phase-6 replay: leakage, real settlements, measured basis, mid-hold liquidation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest

from domain.backtest import BacktestError, Bar, MarginMode, Settlement, replay
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
from domain.risk import RiskLimits

START = datetime(2025, 1, 1, tzinfo=UTC)

TIERS = (
    MaintenanceMarginTier(1, 125, Decimal(0), Decimal("300000"), Decimal("0.004"), Decimal(0)),
    MaintenanceMarginTier(
        2, 100, Decimal("300000"), Decimal("800000"), Decimal("0.005"), Decimal("300")
    ),
)


def specification(symbol: str = "BTCUSDT") -> InstrumentSpecification:
    scope = VenueScope(venue="BINANCE", environment=VenueEnvironment.PRODUCTION)
    return InstrumentSpecification(
        instrument=InstrumentRef(scope=scope, symbol=symbol, market="usdm"),
        status=InstrumentStatus.TRADING,
        contract_type=ContractType.PERPETUAL,
        base_asset="BTC",
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


def bars(count: int, *, price: str = "50000", high_bump: str = "0", drift: str = "0") -> list[Bar]:
    out = []
    value = Decimal(price)
    for index in range(count):
        value = value + Decimal(drift)
        out.append(
            Bar(
                open_time=START + timedelta(hours=index),
                high=value + Decimal(high_bump),
                low=value - Decimal("1"),
                close=value,
            )
        )
    return out


def settlements(count: int, rate: str = "0.0005", every: int = 8) -> list[Settlement]:
    return [
        Settlement(funding_time=START + timedelta(hours=index * every), funding_rate=Decimal(rate))
        for index in range(count)
    ]


def run(**over: object):  # type: ignore[no-untyped-def]
    kwargs: dict[str, object] = {
        "limits": RiskLimits(),
        "capital": Decimal("100000"),
        "hold_hours": 24,
        "entry_every_hours": 24,
        "fee_bps_per_leg": Decimal("2"),
        "slippage_bps_per_leg": Decimal("1"),
        "margin_mode": MarginMode.ISOLATED_LEGS,
        "topup_reserve_multiple": Decimal(0),
    }
    kwargs.update(over)
    return replay(
        over.pop("specification", specification()),  # type: ignore[arg-type]
        over.pop("perp_bars", bars(900)),  # type: ignore[arg-type]
        over.pop("spot_bars", bars(900)),  # type: ignore[arg-type]
        over.pop("settlements", settlements(120)),  # type: ignore[arg-type]
        **{
            k: v
            for k, v in kwargs.items()
            if k not in {"specification", "perp_bars", "spot_bars", "settlements"}
        },  # type: ignore[arg-type]
    )


# --- guardrails --------------------------------------------------------------


def test_a_zero_hold_is_refused() -> None:
    with pytest.raises(BacktestError, match="at least one hour"):
        run(hold_hours=0)


def test_non_positive_capital_is_refused() -> None:
    with pytest.raises(BacktestError, match="capital must be positive"):
        run(capital=Decimal("0"))


# --- leakage -----------------------------------------------------------------


def test_the_warm_up_is_refused_for_want_of_history_not_sized_on_nothing() -> None:
    """Early decisions have no prior bars, so they must be refused rather than
    sized from a volatility estimate that does not exist yet."""
    result = run(perp_bars=bars(200), spot_bars=bars(200), settlements=settlements(30))

    assert result.refusal_reasons.get("INSUFFICIENT_HISTORY", 0) > 0


def test_no_trade_opens_before_enough_history_has_accumulated() -> None:
    result = run()

    assert result.trades
    first = min(trade.entry_time for trade in result.trades)
    assert first >= START + timedelta(hours=30)


# --- funding accrual ---------------------------------------------------------


def test_only_settlements_inside_the_window_are_paid() -> None:
    """ADR-0020: the venue skips settlements. Paying for every scheduled
    boundary invents income."""
    result = run(hold_hours=24, entry_every_hours=24)

    for trade in result.trades:
        assert trade.settlements_paid <= 3


def test_a_window_whose_settlements_were_skipped_earns_nothing() -> None:
    """No settlements recorded inside the window means no funding, even though
    an 8-hour schedule implies three.

    The series carries plenty of *prior* settlements so the forecast still has
    history to work from — otherwise the engine would refuse for want of it,
    which is a different behaviour and is covered separately.
    """
    stops_after_two_weeks = [
        Settlement(funding_time=START + timedelta(hours=index * 8), funding_rate=Decimal("0.0005"))
        for index in range(42)
    ]
    result = run(settlements=stops_after_two_weeks)

    late = [trade for trade in result.trades if trade.entry_time > START + timedelta(days=20)]
    assert late
    assert all(trade.funding_collected == 0 for trade in late)
    assert all(trade.settlements_paid == 0 for trade in late)


def test_negative_funding_is_a_cost_not_an_absolute_value() -> None:
    result = run(settlements=settlements(120, rate="-0.0005"))

    assert all(trade.funding_collected <= 0 for trade in result.trades)


# --- basis -------------------------------------------------------------------


def test_a_constant_basis_contributes_nothing() -> None:
    """Only the *change* in the spread survives the hedge."""
    perp = bars(900, price="50100")
    spot = bars(900, price="50000")

    result = run(perp_bars=perp, spot_bars=spot)

    assert result.trades
    assert all(trade.basis_pnl == 0 for trade in result.trades)


def test_a_widening_basis_is_a_loss_for_the_short_perp() -> None:
    """Perp richening against spot means the short leg loses more than the long
    spot leg gains — the risk the trade actually carries."""
    perp = bars(900, price="50000", drift="1")
    spot = bars(900, price="50000")

    result = run(perp_bars=perp, spot_bars=spot)

    assert result.trades
    assert all(trade.basis_pnl < 0 for trade in result.trades)


# --- liquidation -------------------------------------------------------------


def test_a_spike_inside_the_hold_is_recorded_as_a_liquidation() -> None:
    """A position safe at entry can still die mid-hold. The window's highest
    high decides, not its close."""
    perp = bars(900)
    # One violent wick well beyond any plausible liquidation price.
    perp[400] = Bar(
        open_time=perp[400].open_time,
        high=Decimal("200000"),
        low=Decimal("49000"),
        close=Decimal("50000"),
    )
    result = run(perp_bars=perp, spot_bars=bars(900))

    assert result.liquidations >= 1


def test_a_calm_path_records_no_liquidation() -> None:
    result = run()

    assert result.liquidations == 0


# --- accounting --------------------------------------------------------------


def test_positions_do_not_overlap_so_capital_is_never_double_committed() -> None:
    result = run(hold_hours=48, entry_every_hours=24)

    ordered = sorted(result.trades, key=lambda trade: trade.entry_time)
    for earlier, later in pairwise(ordered):
        assert later.entry_time >= earlier.exit_time


def test_net_pnl_is_funding_plus_basis_less_costs() -> None:
    result = run()

    for trade in result.trades:
        assert trade.net_pnl == (
            trade.funding_collected + trade.basis_pnl - trade.fee_cost - trade.slippage_cost
        )


def test_the_benchmark_is_holding_usdt_not_zero_by_accident() -> None:
    """Kill criterion 1 compares against the benchmark; matching it is failing."""
    result = run(settlements=settlements(120, rate="0"))

    assert result.benchmark_pnl == 0
    assert not result.beats_benchmark


def test_costs_are_charged_on_four_fills() -> None:
    result = run()

    for trade in result.trades:
        expected = trade.notional * (Decimal("2") / Decimal(10000)) * Decimal(4)
        assert trade.fee_cost == expected


# --- liquidation accounting (ADR-0024) ---------------------------------------


def spiking_bars(count: int, at: int, high: str) -> list[Bar]:
    out = bars(count)
    out[at] = Bar(
        open_time=out[at].open_time,
        high=Decimal(high),
        low=Decimal("49000"),
        close=Decimal("50000"),
    )
    return out


def test_a_liquidated_trade_forfeits_its_margin_and_the_venue_fee() -> None:
    """Scoring a liquidated trade as though it ran to exit flatters exactly the
    configurations that liquidate. ADR-0022's first table did that."""
    result = run(perp_bars=spiking_bars(900, 400, "200000"), spot_bars=bars(900))

    dead = [trade for trade in result.trades if trade.liquidated]
    assert dead
    for trade in dead:
        assert trade.liquidation_loss == trade.perp_margin + trade.notional * trade.liquidation_fee
        assert trade.net_pnl < 0


def test_a_surviving_trade_carries_no_liquidation_loss() -> None:
    result = run()

    assert result.liquidation_losses == 0
    assert all(trade.liquidation_loss == 0 for trade in result.trades)


def test_funding_stops_at_liquidation_rather_than_running_to_exit() -> None:
    """A position the venue closed cannot keep collecting funding through the
    window it never survived."""
    early = run(perp_bars=spiking_bars(900, 100, "200000"), spot_bars=bars(900))
    alive = run()

    dead = [trade for trade in early.trades if trade.liquidated]
    assert dead
    matching = [t for t in alive.trades if t.entry_time == dead[0].entry_time]
    if matching:
        assert dead[0].settlements_paid <= matching[0].settlements_paid


def test_topup_capital_counts_toward_the_capital_the_trade_consumed() -> None:
    """Return on capital measured against the base alone would hide the drag."""
    rescued = run(
        perp_bars=spiking_bars(900, 400, "200000"),
        spot_bars=bars(900),
        topup_reserve_multiple=Decimal("2"),
    )

    topped = [trade for trade in rescued.trades if trade.topups > 0]
    assert topped
    for trade in topped:
        assert trade.capital_committed > trade.notional


def test_unified_collateral_lets_the_spot_leg_defend_the_perp() -> None:
    """ADR-0009's missing mechanism, modelled. With one wallet behind both legs
    the spot gain offsets the perp loss and the pair stops being fragile."""
    perp = spiking_bars(900, 400, "200000")
    spot = spiking_bars(900, 400, "200000")

    isolated = run(perp_bars=perp, spot_bars=spot, margin_mode=MarginMode.ISOLATED_LEGS)
    unified = run(perp_bars=perp, spot_bars=spot, margin_mode=MarginMode.UNIFIED_COLLATERAL)

    assert isolated.liquidations > 0
    assert unified.liquidations == 0
