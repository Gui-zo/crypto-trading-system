"""Leakage-free replay of the carry trade over recorded history.

Phase 6. The question this answers is narrow and worth stating exactly: *if the
risk engine had been running over the research history, what would it have done,
and what would that have earned?* Not "what is the best carry strategy" — this
replays the engine we actually have.

Four things make a carry backtest lie, and each is handled explicitly:

* **Leakage.** Volatility and the expected funding rate are computed from bars
  and settlements strictly *before* the decision. The replay hands each decision
  a prefix of history, never the whole series.
* **Assumed funding.** Accrual reads the settlements that actually happened
  inside the holding window (ADR-0020). The venue skips settlements, so paying
  a position for every scheduled boundary invents income.
* **Assumed basis.** Spot and USD-M closes are both recorded, so the basis at
  entry and at exit is *measured*. Carry that ignores basis drift is the single
  easiest way to make this trade look profitable when it is not.
* **Survivorship inside the hold.** A short perp can be liquidated mid-hold even
  though the position was safe at entry. The replay checks the highest perp
  price reached during the window against the liquidation price, so a
  liquidation is recorded rather than quietly earning funding through it.

Everything is ``Decimal``. The result carries a benchmark — holding USDT earns
nothing — because ADR-0004 kill criterion 1 compares against that, not zero.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from domain.errors import DomainError
from domain.instrument import InstrumentSpecification
from domain.liquidation import Position, PositionSide, liquidation_price, tier_for
from domain.precision import to_bps
from domain.regime import (
    PricePoint,
    RegimeError,
    extension_above_trailing,
    worst_drawup,
)
from domain.risk import RiskLimits, SizingRequest, size_position
from domain.volatility import VolatilityError, mean_funding_rate, realized_volatility


class BacktestError(DomainError):
    """History that cannot support an honest replay. Always fails closed."""


class MarginMode(StrEnum):
    """How the two legs share collateral. ADR-0022's whole problem lives here.

    ``ISOLATED_LEGS`` is what the venue does today under this account's
    configuration: the spot leg sits in the spot wallet and cannot post margin to
    the futures wallet, so it can be profitable and useless while the perp dies.

    ``UNIFIED_COLLATERAL`` models a portfolio-margin style account where both
    legs back one maintenance requirement. **This is a modelled hypothesis, not a
    verified venue behaviour** — nothing in `tests/fixtures/binance/recorded/`
    confirms the mechanics, and under ADR-0003 that means it is unverified.
    """

    ISOLATED_LEGS = "ISOLATED_LEGS"
    UNIFIED_COLLATERAL = "UNIFIED_COLLATERAL"


@dataclass(frozen=True, slots=True)
class HoldOutcome:
    """What happened between entry and exit."""

    liquidated: bool
    topups: int
    topup_capital: Decimal
    worst_distance: Decimal
    #: When the venue closed the position, and at what price. ``None`` if it
    #: survived. Funding stops here and the margin is gone — a liquidated trade
    #: scored as though it ran to exit would flatter every configuration that
    #: liquidates, which is exactly the comparison ADR-0024 needs to be honest.
    liquidated_at: datetime | None = None
    liquidation_price_hit: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Bar:
    """One closed candle. ``high`` is carried because liquidation needs it."""

    open_time: datetime
    high: Decimal
    low: Decimal
    close: Decimal

    def __post_init__(self) -> None:
        if self.open_time.tzinfo is None:
            raise BacktestError("bar open_time must be timezone-aware")
        if min(self.high, self.low, self.close) <= 0:
            raise BacktestError("bar prices must be positive")
        if self.high < self.low:
            raise BacktestError("bar high is below its low")


@dataclass(frozen=True, slots=True)
class Settlement:
    funding_time: datetime
    funding_rate: Decimal


@dataclass(frozen=True, slots=True)
class Trade:
    """One completed round trip, with every component of its P&L."""

    symbol: str
    entry_time: datetime
    exit_time: datetime
    quantity: Decimal
    notional: Decimal
    capital_committed: Decimal
    perp_entry: Decimal
    perp_exit: Decimal
    spot_entry: Decimal
    spot_exit: Decimal
    funding_collected: Decimal
    settlements_paid: int
    fee_cost: Decimal
    slippage_cost: Decimal
    basis_pnl: Decimal
    liquidated: bool
    perp_margin: Decimal = Decimal(0)
    liquidation_fee: Decimal = Decimal(0)
    topups: int = 0
    topup_capital: Decimal = Decimal(0)
    worst_distance: Decimal = Decimal(0)

    @property
    def net_pnl(self) -> Decimal:
        """Funding plus basis drift, less costs — and less the margin if it died.

        The delta-neutral legs cancel by construction: the short perp loses what
        the long spot gains on a price move. What does *not* cancel is the change
        in the spot-perp spread, which is ``basis_pnl``, and that is the risk the
        trade is actually taking.

        A liquidation forfeits the perp margin and the venue's liquidation fee.
        Scoring a liquidated trade as though it ran to exit would flatter exactly
        the configurations that liquidate, which is the comparison that has to be
        honest (ADR-0024).
        """
        base = self.funding_collected + self.basis_pnl - self.fee_cost - self.slippage_cost
        return base - self.liquidation_loss

    @property
    def liquidation_loss(self) -> Decimal:
        """Margin forfeited plus the venue's liquidation fee. Zero if it lived."""
        if not self.liquidated:
            return Decimal(0)
        return self.perp_margin + self.notional * self.liquidation_fee

    @property
    def return_on_capital_bps(self) -> Decimal:
        if self.capital_committed <= 0:
            return Decimal(0)
        return to_bps(self.net_pnl / self.capital_committed)


@dataclass(frozen=True, slots=True)
class BacktestResult:
    symbol: str
    trades: tuple[Trade, ...]
    considered: int
    refused: int
    refusal_reasons: dict[str, int]

    @property
    def net_pnl(self) -> Decimal:
        return sum((trade.net_pnl for trade in self.trades), Decimal(0))

    @property
    def funding_collected(self) -> Decimal:
        return sum((trade.funding_collected for trade in self.trades), Decimal(0))

    @property
    def basis_pnl(self) -> Decimal:
        return sum((trade.basis_pnl for trade in self.trades), Decimal(0))

    @property
    def liquidation_losses(self) -> Decimal:
        return sum((trade.liquidation_loss for trade in self.trades), Decimal(0))

    @property
    def peak_topup_capital(self) -> Decimal:
        return sum((trade.topup_capital for trade in self.trades), Decimal(0))

    @property
    def costs(self) -> Decimal:
        return sum((trade.fee_cost + trade.slippage_cost for trade in self.trades), Decimal(0))

    @property
    def liquidations(self) -> int:
        """ADR-0009 ceiling evidence. One is a permanent promotion failure."""
        return sum(1 for trade in self.trades if trade.liquidated)

    @property
    def benchmark_pnl(self) -> Decimal:
        """Holding USDT over the same windows. Cash earns nothing here."""
        return Decimal(0)

    @property
    def beats_benchmark(self) -> bool:
        return self.net_pnl > self.benchmark_pnl


def _bars_before(bars: Sequence[Bar], moment: datetime) -> tuple[Bar, ...]:
    return tuple(bar for bar in bars if bar.open_time < moment)


def replay(
    specification: InstrumentSpecification,
    perp_bars: Sequence[Bar],
    spot_bars: Sequence[Bar],
    settlements: Sequence[Settlement],
    *,
    limits: RiskLimits,
    capital: Decimal,
    hold_hours: int,
    entry_every_hours: int,
    fee_bps_per_leg: Decimal,
    slippage_bps_per_leg: Decimal,
    volatility_lookback: int = 720,
    funding_lookback: int = 90,
    tail_aware: bool = False,
    extension_lookback: int = 720,
    margin_mode: MarginMode = MarginMode.ISOLATED_LEGS,
    topup_reserve_multiple: Decimal = Decimal(0),
    topup_trigger_distance: Decimal = Decimal("0.10"),
) -> BacktestResult:
    """Replay the risk engine across ``perp_bars``, opening at fixed intervals.

    Entries are attempted on a fixed cadence rather than on a signal, because the
    engine under test is the *risk* engine: the question is what it permits and
    what that earns, not whether some timing rule adds value.

    ``margin_mode`` and ``topup_reserve_multiple`` model the two remedies left
    open by ADR-0022. Both are hypotheses about account configuration and
    operator behaviour rather than verified venue mechanics.

    ``tail_aware`` swaps the parametric stress band for the larger of it and the
    worst rise actually observed over a window of the holding length, and feeds
    the symbol's extension above its trailing median to the regime filter
    (ADR-0022). Both are measured from the decision's own prefix of history.
    """
    if hold_hours < 1 or entry_every_hours < 1:
        raise BacktestError("hold and entry cadence must be at least one hour")
    if capital <= 0:
        raise BacktestError("capital must be positive")

    perp = sorted(perp_bars, key=lambda bar: bar.open_time)
    spot_by_time = {bar.open_time: bar for bar in spot_bars}
    ordered_settlements = sorted(settlements, key=lambda item: item.funding_time)

    trades: list[Trade] = []
    considered = 0
    refused = 0
    reasons: dict[str, int] = {}
    index = 0
    while index < len(perp):
        entry_bar = perp[index]
        exit_time = entry_bar.open_time + timedelta(hours=hold_hours)
        window = [bar for bar in perp[index:] if bar.open_time <= exit_time]
        if not window or window[-1].open_time < exit_time:
            break  # not enough future history to close the position honestly
        spot_entry_bar = spot_by_time.get(entry_bar.open_time)
        spot_exit_bar = spot_by_time.get(window[-1].open_time)
        if spot_entry_bar is None or spot_exit_bar is None:
            index += entry_every_hours
            continue

        considered += 1

        # --- everything below uses only data strictly before the decision ---
        prior_bars = _bars_before(perp, entry_bar.open_time)
        prior_settlements = [
            item for item in ordered_settlements if item.funding_time < entry_bar.open_time
        ]
        try:
            volatility = realized_volatility(
                [bar.close for bar in prior_bars[-volatility_lookback:]],
                periods_ahead=hold_hours,
            )
            expected_rate = mean_funding_rate(
                [item.funding_rate for item in prior_settlements[-funding_lookback:]]
            )
        except VolatilityError:
            refused += 1
            reasons["INSUFFICIENT_HISTORY"] = reasons.get("INSUFFICIENT_HISTORY", 0) + 1
            index += entry_every_hours
            continue

        empirical_tail: Decimal | None = None
        extension: Decimal | None = None
        if tail_aware:
            try:
                empirical_tail = worst_drawup(
                    [PricePoint(close=bar.close, high=bar.high) for bar in prior_bars],
                    horizon_periods=hold_hours,
                )
                extension = extension_above_trailing(
                    [bar.close for bar in prior_bars], lookback_periods=extension_lookback
                )
            except RegimeError:
                refused += 1
                reasons["INSUFFICIENT_HISTORY"] = reasons.get("INSUFFICIENT_HISTORY", 0) + 1
                index += entry_every_hours
                continue

        interval = specification.funding_schedule.interval_hours
        expected_settlements = hold_hours // interval
        margin_fraction = Decimal(1) / limits.max_effective_leverage
        round_trip_bps = (fee_bps_per_leg + slippage_bps_per_leg) * Decimal(4)
        gross_bps = to_bps(expected_rate) * Decimal(expected_settlements)
        capital_multiple = Decimal(1) + margin_fraction + limits.margin_buffer_fraction
        net_bps_on_capital = (gross_bps - round_trip_bps) / capital_multiple

        decision = size_position(
            SizingRequest(
                specification=specification,
                mark_price=entry_bar.close,
                forecast_volatility=volatility,
                available_capital=capital,
                net_carry_bps=net_bps_on_capital,
                empirical_tail=empirical_tail,
                extension=extension,
            ),
            limits,
        )
        if not decision.approved:
            refused += 1
            reasons[decision.binding.value] = reasons.get(decision.binding.value, 0) + 1
            index += entry_every_hours
            continue

        # --- the position is open; from here we may look forward ---
        exit_bar = window[-1]
        quantity = decision.quantity
        perp_entry = entry_bar.close
        perp_exit = exit_bar.close
        spot_entry = spot_entry_bar.close
        spot_exit = spot_exit_bar.close

        # Walk the hold first: whether and when the venue closed the position
        # decides how long funding accrues and where the legs are marked.
        outcome = _simulate_hold(
            specification,
            window,
            spot_by_time,
            quantity=quantity,
            perp_entry=perp_entry,
            spot_entry=spot_entry,
            perp_margin=decision.perp_margin,
            margin_buffer=decision.margin_buffer,
            margin_mode=margin_mode,
            topup_reserve=capital * topup_reserve_multiple,
            topup_trigger_distance=topup_trigger_distance,
        )
        liquidated = outcome.liquidated

        # Funding actually settled inside the window, never the schedule's guess,
        # and never past the moment the position stopped existing.
        accrual_end = outcome.liquidated_at or exit_bar.open_time
        paid = [
            item
            for item in ordered_settlements
            if entry_bar.open_time < item.funding_time <= accrual_end
        ]
        funding_collected = sum(
            (item.funding_rate * quantity * perp_entry for item in paid), Decimal(0)
        )

        # The legs cancel on price; what survives is the change in the spread.
        # A liquidated position is closed by the venue at the liquidation price,
        # so mark it there rather than at the exit it never reached.
        if outcome.liquidated and outcome.liquidation_price_hit is not None:
            perp_exit = outcome.liquidation_price_hit
            spot_at_kill = spot_by_time.get(accrual_end)
            spot_exit = spot_at_kill.high if spot_at_kill is not None else perp_exit
        entry_basis = perp_entry - spot_entry
        exit_basis = perp_exit - spot_exit
        basis_pnl = (entry_basis - exit_basis) * quantity

        notional = quantity * perp_entry
        fee_cost = notional * (fee_bps_per_leg / Decimal(10000)) * Decimal(4)
        slippage_cost = notional * (slippage_bps_per_leg / Decimal(10000)) * Decimal(4)

        trades.append(
            Trade(
                symbol=specification.instrument.symbol,
                entry_time=entry_bar.open_time,
                exit_time=exit_bar.open_time,
                quantity=quantity,
                notional=notional,
                # Top-up capital is capital the trade consumed, so return on
                # capital must be measured against the total actually at risk.
                capital_committed=decision.capital_required + outcome.topup_capital,
                perp_entry=perp_entry,
                perp_exit=perp_exit,
                spot_entry=spot_entry,
                spot_exit=spot_exit,
                funding_collected=funding_collected,
                settlements_paid=len(paid),
                fee_cost=fee_cost,
                slippage_cost=slippage_cost,
                basis_pnl=basis_pnl,
                liquidated=liquidated,
                perp_margin=decision.perp_margin,
                liquidation_fee=specification.liquidation_fee,
                topups=outcome.topups,
                topup_capital=outcome.topup_capital,
                worst_distance=outcome.worst_distance,
            )
        )
        # Non-overlapping: one position at a time, so capital is never
        # double-committed and the P&L series is not an average of overlaps.
        index += max(hold_hours, entry_every_hours)

    return BacktestResult(
        symbol=specification.instrument.symbol,
        trades=tuple(trades),
        considered=considered,
        refused=refused,
        refusal_reasons=reasons,
    )


def _simulate_hold(
    specification: InstrumentSpecification,
    window: Sequence[Bar],
    spot_by_time: dict[datetime, Bar],
    *,
    quantity: Decimal,
    perp_entry: Decimal,
    spot_entry: Decimal,
    perp_margin: Decimal,
    margin_buffer: Decimal,
    margin_mode: MarginMode,
    topup_reserve: Decimal,
    topup_trigger_distance: Decimal,
) -> HoldOutcome:
    """Walk the hold bar by bar, topping up and dying where the venue would.

    Bar by bar rather than against the window's highest high, because a top-up
    changes the liquidation price partway through and a single maximum cannot
    represent that. The cost is one pass per hold, which is cheap.

    Under ``UNIFIED_COLLATERAL`` the spot leg is marked to market and counted as
    collateral. For a delta-neutral pair that makes equity nearly constant in
    price — the spot gain offsets the perp loss — which is precisely the
    mechanism ADR-0009 said was missing when the legs sit in separate wallets.
    """
    schedule = specification.margin_schedule
    wallet = perp_margin + margin_buffer
    reserve = topup_reserve
    topups = 0
    injected = Decimal(0)
    worst_distance = Decimal(1)

    for bar in window:
        price = bar.high  # the worst point of the bar for a short
        notional = quantity * price
        try:
            tier = tier_for(schedule, notional)
        except Exception:
            return HoldOutcome(True, topups, injected, Decimal(0))
        maintenance = notional * tier.maintenance_margin_ratio - tier.cumulative

        perp_pnl = quantity * (perp_entry - price)
        equity = wallet + perp_pnl
        if margin_mode is MarginMode.UNIFIED_COLLATERAL:
            spot_bar = spot_by_time.get(bar.open_time)
            spot_price = spot_bar.high if spot_bar is not None else price
            # The spot leg is an asset backing the same requirement.
            equity += quantity * spot_price

        current = liquidation_price(
            Position(
                side=PositionSide.SHORT,
                quantity=quantity,
                entry_price=perp_entry,
                wallet_balance=wallet,
            ),
            schedule,
        )
        if current is not None and price > 0:
            distance = max(Decimal(0), (current - price) / price)
            worst_distance = min(worst_distance, distance)

        if equity >= maintenance:
            # Solvent. Top up early if the position is getting close, so the
            # operator acts on a margin call rather than on a liquidation.
            if (
                reserve > 0
                and current is not None
                and (current - price) / price < topup_trigger_distance
            ):
                injection = min(reserve, perp_margin)
                wallet += injection
                reserve -= injection
                injected += injection
                topups += 1
            continue

        # Insolvent at this bar: the reserve is the last thing between the
        # position and the venue closing it.
        shortfall = maintenance - equity
        if reserve >= shortfall:
            wallet += shortfall
            reserve -= shortfall
            injected += shortfall
            topups += 1
            continue
        return HoldOutcome(True, topups, injected, Decimal(0), bar.open_time, price)

    return HoldOutcome(False, topups, injected, worst_distance)
