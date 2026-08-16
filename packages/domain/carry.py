"""Carry economics: what a delta-neutral funding position actually earns.

ADR-0004 kill criterion 1 is that net harvested funding — after fees, slippage,
basis drift, and the cost of capital on **both** legs — must beat simply holding
USDT. This module computes that number so the criterion is measurable rather
than rhetorical.

Two things here are easy to get wrong and are deliberately made hard:

**Carry is quoted on capital, not on notional.** A delta-neutral position is
long spot *and* short perp, so it consumes the full spot notional plus the perp's
margin plus the untouchable buffer. Quoting the funding yield against the perp
notional alone would roughly halve the denominator and make a marginal trade look
like a good one. :attr:`CarryEstimate.net_bps_on_capital` is the honest figure
and is what the promotion gate should read.

**A round trip is four fills, not two.** Entering costs a spot buy and a perp
sell; exiting costs a spot sell and a perp buy. Costing only the entry
understates the hurdle by half.

Everything is in **basis points** (ADR-0011) and every arithmetic step is
``Decimal``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from domain.errors import DomainError
from domain.precision import to_bps


class CarryError(DomainError):
    """Carry inputs that cannot produce an honest estimate. Always fails closed."""


def _require_finite(value: Decimal, name: str) -> None:
    if not value.is_finite():
        raise CarryError(f"{name} must be finite")


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Round-trip trading costs, per leg, in basis points of that leg's notional.

    Defaults are deliberately absent. A fee schedule guessed from memory is how
    a backtest earns money the venue would have taken.
    """

    perp_entry_bps: Decimal
    perp_exit_bps: Decimal
    spot_entry_bps: Decimal
    spot_exit_bps: Decimal

    def __post_init__(self) -> None:
        for name in ("perp_entry_bps", "perp_exit_bps", "spot_entry_bps", "spot_exit_bps"):
            value: Decimal = getattr(self, name)
            _require_finite(value, name)
            if value < 0:
                raise CarryError(f"{name} cannot be negative; a rebate is not modelled here")

    @property
    def round_trip_bps(self) -> Decimal:
        """All four fills. Both legs, in and out."""
        return self.perp_entry_bps + self.perp_exit_bps + self.spot_entry_bps + self.spot_exit_bps


@dataclass(frozen=True, slots=True)
class CarryInputs:
    """One symbol's carry case over an intended holding horizon.

    ``expected_funding_rate`` is the **per-settlement** rate a short perp
    receives, as a fraction — the model's forecast, not a realised value.
    ``settlements`` is how many settlements the horizon actually contains, which
    the caller derives from observed cadence rather than from a schedule
    (ADR-0020): the venue skips settlements, and assuming one exists inflates
    the estimate.
    """

    expected_funding_rate: Decimal
    settlements: int
    slippage_bps: Decimal
    basis_cost_bps: Decimal
    capital_cost_bps: Decimal
    fees: FeeSchedule
    #: Perp margin as a fraction of perp notional (1 / effective leverage).
    perp_margin_fraction: Decimal
    #: Unencumbered buffer held on the futures wallet, as a fraction of notional.
    margin_buffer_fraction: Decimal

    def __post_init__(self) -> None:
        if self.settlements < 0:
            raise CarryError("settlements cannot be negative")
        for name in (
            "expected_funding_rate",
            "slippage_bps",
            "basis_cost_bps",
            "capital_cost_bps",
            "perp_margin_fraction",
            "margin_buffer_fraction",
        ):
            _require_finite(getattr(self, name), name)
        for name in ("slippage_bps", "basis_cost_bps", "capital_cost_bps"):
            if getattr(self, name) < 0:
                raise CarryError(f"{name} cannot be negative; costs are costs")
        if self.perp_margin_fraction <= 0:
            raise CarryError("perp margin fraction must be positive; infinite leverage is not real")
        if self.margin_buffer_fraction < 0:
            raise CarryError("margin buffer fraction cannot be negative")


@dataclass(frozen=True, slots=True)
class CarryEstimate:
    """A decomposed carry estimate. Every component is reported, not just the total.

    An opaque net number cannot be argued with, and ADR-0004's kill criterion
    needs to be arguable — if carry compresses, the reader should be able to see
    whether it was the funding that fell or the costs that rose.
    """

    gross_funding_bps: Decimal
    fee_bps: Decimal
    slippage_bps: Decimal
    basis_cost_bps: Decimal
    capital_cost_bps: Decimal
    net_bps_on_notional: Decimal
    capital_multiple: Decimal
    net_bps_on_capital: Decimal

    @property
    def total_cost_bps(self) -> Decimal:
        return self.fee_bps + self.slippage_bps + self.basis_cost_bps + self.capital_cost_bps

    def beats(self, benchmark_bps: Decimal) -> bool:
        """Strictly beats the benchmark on capital. Matching it is not beating it."""
        return self.net_bps_on_capital > benchmark_bps


def estimate_carry(inputs: CarryInputs) -> CarryEstimate:
    """Net carry over the horizon, on notional and on capital deployed.

    The capital multiple is ``spot notional + perp margin + buffer`` per unit of
    perp notional. For a fully-funded spot leg at 2x on the perp with a 25%
    buffer that is ``1 + 0.5 + 0.25 = 1.75``, so a 10 bps carry on notional is
    5.7 bps on the money actually tied up.
    """
    gross = to_bps(inputs.expected_funding_rate) * Decimal(inputs.settlements)
    fee = inputs.fees.round_trip_bps
    net_on_notional = (
        gross - fee - inputs.slippage_bps - inputs.basis_cost_bps - inputs.capital_cost_bps
    )
    # Spot leg is fully funded (1), perp posts margin, buffer sits untouched.
    capital_multiple = Decimal(1) + inputs.perp_margin_fraction + inputs.margin_buffer_fraction
    return CarryEstimate(
        gross_funding_bps=gross,
        fee_bps=fee,
        slippage_bps=inputs.slippage_bps,
        basis_cost_bps=inputs.basis_cost_bps,
        capital_cost_bps=inputs.capital_cost_bps,
        net_bps_on_notional=net_on_notional,
        capital_multiple=capital_multiple,
        net_bps_on_capital=net_on_notional / capital_multiple,
    )


def breakeven_funding_rate(inputs: CarryInputs) -> Decimal | None:
    """The per-settlement funding rate at which this case exactly breaks even.

    Useful as a refusal explanation: "this needs 1.4 bps a settlement and the
    model forecasts 0.9". ``None`` when the horizon contains no settlements, in
    which case no funding rate can pay for the round trip.
    """
    if inputs.settlements == 0:
        return None
    costs = (
        inputs.fees.round_trip_bps
        + inputs.slippage_bps
        + inputs.basis_cost_bps
        + inputs.capital_cost_bps
    )
    return costs / Decimal(inputs.settlements)
