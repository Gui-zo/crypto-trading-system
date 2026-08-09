"""Deterministic kill-switch and decision-input health evaluation.

Adapted from the sibling ``automated-trading-system`` repo. The *machinery* is
ported unchanged — the eight kill-switch scopes, the append-only monotonic control
event log, the never-short-circuit check list, and the automatic-halt latching.
:class:`SafetyContext` is rewritten, because the predecessor's context is built
from prediction-market facts (contract review status, forecast issue time, a
single quote) that have no analogue here. See docs/adr/0014.

What changed, and why
---------------------
* **Freshness is tiered, not single.** The predecessor has one 90-minute quote
  tolerance. Here a decision reads three independently-aged inputs — mark price,
  funding rate, and account/margin state — each with its own tolerance, because
  they refresh on completely different cadences and a stale one fails differently
  (ADR-0007).
* **Venue environment is a gate, not a convention.** Binance reuses symbols across
  testnet and production, so a context whose data came from a different
  environment than the one being traded is rejected outright (ADR-0010).
* **Reconciliation is a precondition.** Two legs and real margin mean local ledger
  drift is the account-ending failure mode, so an unreconciled or discrepant
  account blocks and latches a halt (ADR-0013).
* **Money is :class:`~decimal.Decimal`.** Never float (ADR-0011).

This module does *not* size positions and does not know what a liquidation price
is. It answers "may a decision be made from these inputs at all"; the risk engine
(Phase 5) answers "how big". Keeping them apart is what lets the safety gate stay
exhaustively testable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from domain.modes import TradingMode, permits_new_orders

# Default freshness tolerances (ADR-0007). Every one is a starting point to be
# validated against recorded latency, and every one is overridable per call so a
# backtest can state its own. They are orders of magnitude tighter than the
# predecessor's 90 minutes: an 8-hour *decision* cadence does not excuse a stale
# price at the moment the decision is made.
DEFAULT_MAX_MARK_PRICE_AGE = timedelta(seconds=60)
DEFAULT_MAX_ACCOUNT_STATE_AGE = timedelta(seconds=60)
# One funding interval. Binance's usual USDS-M cadence is 8h, but some symbols
# settle every 4h — the interval is read per symbol from the venue and passed in,
# never assumed (ADR-0003).
DEFAULT_MAX_FUNDING_AGE = timedelta(hours=8)

DEFAULT_CLOCK_DRIFT_TOLERANCE = timedelta(minutes=5)


class SafetyScope(StrEnum):
    GLOBAL = "GLOBAL"
    VENUE = "VENUE"
    STRATEGY = "STRATEGY"
    CATEGORY = "CATEGORY"
    MARKET = "MARKET"
    DATA_PROVIDER = "DATA_PROVIDER"
    MODEL_VERSION = "MODEL_VERSION"
    ACCOUNT = "ACCOUNT"


class SafetyControlAction(StrEnum):
    ACTIVATE = "ACTIVATE"
    CLEAR = "CLEAR"


class SafetyCheckStatus(StrEnum):
    PASS = "PASS"
    BLOCK = "BLOCK"


class SafetyGateStatus(StrEnum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"


class ReconciliationStatus(StrEnum):
    """Whether the local ledger has been proven to match venue account state.

    ``UNKNOWN`` is not a neutral value — it means nobody has checked, which is
    exactly the state in which a drifting ledger goes unnoticed. It blocks.
    """

    RECONCILED = "RECONCILED"
    DISCREPANCY = "DISCREPANCY"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SafetyScopeRef:
    scope: SafetyScope
    key: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", normalize_scope_key(self.scope, self.key))


@dataclass(frozen=True, slots=True)
class SafetyControlState:
    event_id: uuid.UUID
    sequence_number: int
    scope: SafetyScope
    scope_key: str
    action: SafetyControlAction
    reason: str

    @property
    def active(self) -> bool:
        return self.action is SafetyControlAction.ACTIVATE


@dataclass(frozen=True, slots=True)
class AutomaticHalt:
    scope: SafetyScope
    scope_key: str
    reason: str


@dataclass(frozen=True, slots=True)
class SafetyCheck:
    name: str
    status: SafetyCheckStatus
    detail: str
    observed: str | int | float | bool | None = None
    limit: str | int | float | bool | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "name": self.name,
            "status": self.status.value,
            "detail": self.detail,
        }
        if self.observed is not None:
            result["observed"] = self.observed
        if self.limit is not None:
            result["limit"] = self.limit
        return result


@dataclass(frozen=True, slots=True)
class SafetyContext:
    """Every fact a decision-time safety evaluation reads.

    Amounts are quote-currency (USDT) :class:`~decimal.Decimal`, never float.
    """

    evaluated_at: datetime
    mode: TradingMode

    # Venue and instrument
    venue_enabled: bool
    instrument_status: str  # Binance exchangeInfo symbol status, e.g. "TRADING"
    instrument_spec_status: str  # our own review of filters + margin tiers
    trading_environment: str  # the environment we intend to trade
    data_environment: str  # the environment the inputs below came from

    # Tiered input freshness (ADR-0007)
    mark_price_recorded_at: datetime
    funding_recorded_at: datetime
    account_state_recorded_at: datetime
    max_mark_price_age: timedelta = DEFAULT_MAX_MARK_PRICE_AGE
    max_funding_age: timedelta = DEFAULT_MAX_FUNDING_AGE
    max_account_state_age: timedelta = DEFAULT_MAX_ACCOUNT_STATE_AGE

    # Lineage and ledger integrity
    provider_keys: tuple[str, ...] = ()
    reconciliation_status: ReconciliationStatus = ReconciliationStatus.UNKNOWN

    # Account limits
    available_margin: Decimal = Decimal("0")
    daily_realized_pnl: Decimal = Decimal("0")
    daily_loss_limit: Decimal = Decimal("0")
    drawdown_fraction: Decimal = Decimal("0")
    drawdown_limit_fraction: Decimal = Decimal("1")

    account_key: str = "DEFAULT"
    scope_refs: tuple[SafetyScopeRef, ...] = ()


@dataclass(frozen=True, slots=True)
class SafetyEvaluation:
    status: SafetyGateStatus
    checks: tuple[SafetyCheck, ...]
    automatic_halts: tuple[AutomaticHalt, ...]

    @property
    def blocked_reasons(self) -> tuple[str, ...]:
        return tuple(
            f"{check.name}: {check.detail}"
            for check in self.checks
            if check.status is SafetyCheckStatus.BLOCK
        )


def normalize_scope_key(scope: SafetyScope, key: str) -> str:
    normalized = key.strip().upper()
    if scope is SafetyScope.GLOBAL:
        if normalized not in {"", "*", "GLOBAL"}:
            raise ValueError("global safety scope key must be '*'")
        return "*"
    if not normalized or normalized == "*":
        raise ValueError(f"{scope.value} safety scope requires a specific key")
    return normalized


def _check(
    name: str,
    passed: bool,
    *,
    passed_detail: str,
    blocked_detail: str,
    observed: str | int | float | bool | None = None,
    limit: str | int | float | bool | None = None,
) -> SafetyCheck:
    return SafetyCheck(
        name=name,
        status=SafetyCheckStatus.PASS if passed else SafetyCheckStatus.BLOCK,
        detail=passed_detail if passed else blocked_detail,
        observed=observed,
        limit=limit,
    )


def _freshness_checks(
    name: str,
    *,
    age: timedelta,
    maximum_age: timedelta,
    clock_drift_tolerance: timedelta,
    subject: str,
) -> list[SafetyCheck]:
    """A drift check and a staleness check for one timestamped input.

    Two checks, not one, so the report distinguishes "this input is old" from
    "this input is dated in the future", which are different bugs with different
    fixes — the first is a collection failure, the second a clock failure.
    """
    return [
        _check(
            f"{name}_CLOCK_DRIFT",
            age >= -clock_drift_tolerance,
            passed_detail=f"{subject} timestamp is not materially in the future",
            blocked_detail=f"{subject} timestamp exceeds clock-drift tolerance",
            observed=round(age.total_seconds(), 3),
            limit=-clock_drift_tolerance.total_seconds(),
        ),
        _check(
            f"{name}_CURRENT",
            -clock_drift_tolerance <= age <= maximum_age,
            passed_detail=f"{subject} is current",
            blocked_detail=f"{subject} is stale or future-dated",
            observed=round(age.total_seconds(), 3),
            limit=maximum_age.total_seconds(),
        ),
    ]


def evaluate_safety(
    context: SafetyContext,
    *,
    scoped_controls: tuple[SafetyControlState, ...] = (),
    clock_drift_tolerance: timedelta = DEFAULT_CLOCK_DRIFT_TOLERANCE,
) -> SafetyEvaluation:
    """Return all mandatory decision-stage checks; never short-circuit failures.

    Every check is always evaluated so the operator sees the complete picture of
    what is wrong, not just the first thing that failed. The gate blocks if any
    single check blocks.
    """
    if context.max_mark_price_age <= timedelta(0):
        raise ValueError("maximum mark-price age must be positive")
    if context.max_funding_age <= timedelta(0):
        raise ValueError("maximum funding age must be positive")
    if context.max_account_state_age <= timedelta(0):
        raise ValueError("maximum account-state age must be positive")
    if clock_drift_tolerance < timedelta(0):
        raise ValueError("clock drift tolerance cannot be negative")
    if context.daily_loss_limit < 0:
        raise ValueError("daily loss limit cannot be negative")

    daily_loss = max(Decimal("0"), -context.daily_realized_pnl)

    checks = [
        _check(
            "MODE_ALLOWED",
            permits_new_orders(context.mode),
            passed_detail=f"{context.mode.value} permits order origination",
            blocked_detail=f"{context.mode.value} forbids order origination",
            observed=context.mode.value,
        ),
        _check(
            "VENUE_ENABLED",
            context.venue_enabled,
            passed_detail="venue is enabled",
            blocked_detail="venue is disabled",
            observed=context.venue_enabled,
        ),
        # Binance reuses symbols across testnet and production. Interleaving the
        # two silently corrupts every downstream series, so a mismatch is not a
        # warning (ADR-0010).
        _check(
            "VENUE_ENVIRONMENT_SCOPE",
            (
                bool(context.trading_environment)
                and context.trading_environment == context.data_environment
            ),
            passed_detail=f"inputs are scoped to {context.trading_environment}",
            blocked_detail=(
                f"inputs came from {context.data_environment!r} but trading "
                f"{context.trading_environment!r}"
            ),
            observed=context.data_environment,
            limit=context.trading_environment,
        ),
        _check(
            "INSTRUMENT_TRADING",
            context.instrument_status.upper() == "TRADING",
            passed_detail="instrument is trading",
            blocked_detail=f"instrument status is {context.instrument_status}",
            observed=context.instrument_status,
            limit="TRADING",
        ),
        # The analogue of the predecessor's CONTRACT_APPROVED: filters, tick/step
        # sizes, and maintenance-margin tiers must be the reviewed, versioned set.
        # This is where a wrong assumption silently produces a wrong size.
        _check(
            "INSTRUMENT_SPEC_APPROVED",
            context.instrument_spec_status.upper() == "APPROVED",
            passed_detail="instrument filters and margin tiers are approved",
            blocked_detail=(
                "instrument filters/margin tiers are not approved: "
                f"{context.instrument_spec_status}"
            ),
            observed=context.instrument_spec_status,
            limit="APPROVED",
        ),
        *_freshness_checks(
            "MARK_PRICE",
            age=context.evaluated_at - context.mark_price_recorded_at,
            maximum_age=context.max_mark_price_age,
            clock_drift_tolerance=clock_drift_tolerance,
            subject="mark price",
        ),
        *_freshness_checks(
            "FUNDING",
            age=context.evaluated_at - context.funding_recorded_at,
            maximum_age=context.max_funding_age,
            clock_drift_tolerance=clock_drift_tolerance,
            subject="funding rate",
        ),
        *_freshness_checks(
            "ACCOUNT_STATE",
            age=context.evaluated_at - context.account_state_recorded_at,
            maximum_age=context.max_account_state_age,
            clock_drift_tolerance=clock_drift_tolerance,
            subject="account/margin state",
        ),
        _check(
            "DATA_PROVIDER_LINEAGE",
            bool(context.provider_keys),
            passed_detail="decision retains exact provider lineage",
            blocked_detail="decision has no exact data-provider lineage",
            observed=len(context.provider_keys),
            limit=1,
        ),
        # Two legs plus real margin means ledger drift ends accounts. UNKNOWN
        # blocks: "nobody checked" is the state in which drift goes unnoticed.
        _check(
            "LEDGER_RECONCILED",
            context.reconciliation_status is ReconciliationStatus.RECONCILED,
            passed_detail="local ledger matches venue account state",
            blocked_detail=f"ledger reconciliation is {context.reconciliation_status.value}",
            observed=context.reconciliation_status.value,
            limit=ReconciliationStatus.RECONCILED.value,
        ),
        _check(
            "AVAILABLE_MARGIN",
            context.available_margin >= 0,
            passed_detail="available margin is nonnegative",
            blocked_detail=f"available margin is {context.available_margin}",
            observed=str(context.available_margin),
            limit=0,
        ),
        _check(
            "DAILY_LOSS",
            daily_loss < context.daily_loss_limit,
            passed_detail="daily realized loss is below the limit",
            blocked_detail="daily realized loss reached the limit",
            observed=str(daily_loss),
            limit=str(context.daily_loss_limit),
        ),
        _check(
            "ROLLING_DRAWDOWN",
            context.drawdown_fraction < context.drawdown_limit_fraction,
            passed_detail="rolling drawdown is below the limit",
            blocked_detail="rolling drawdown reached the limit",
            observed=str(context.drawdown_fraction),
            limit=str(context.drawdown_limit_fraction),
        ),
    ]

    controls_by_scope = {(control.scope, control.scope_key): control for control in scoped_controls}
    for scope_ref in sorted(context.scope_refs, key=lambda ref: (ref.scope.value, ref.key)):
        control = controls_by_scope.get((scope_ref.scope, scope_ref.key))
        active = control is not None and control.active
        action = control.action.value if control is not None else "NO_EVENT"
        detail = (
            f"{scope_ref.scope.value}:{scope_ref.key} has no active halt"
            if control is None
            else f"{scope_ref.scope.value}:{scope_ref.key} is clear"
        )
        checks.append(
            _check(
                f"KILL_SWITCH_{scope_ref.scope.value}",
                not active,
                passed_detail=detail,
                blocked_detail=(
                    f"{scope_ref.scope.value}:{scope_ref.key} is halted: "
                    f"{control.reason if control is not None else 'unknown'}"
                ),
                observed=action,
                limit=SafetyControlAction.CLEAR.value,
            )
        )

    automatic_halts: list[AutomaticHalt] = []
    account_scope_key = normalize_scope_key(SafetyScope.ACCOUNT, context.account_key)
    if context.available_margin < 0:
        automatic_halts.append(
            AutomaticHalt(
                scope=SafetyScope.ACCOUNT,
                scope_key=account_scope_key,
                reason="unexpected negative available margin",
            )
        )
    if daily_loss >= context.daily_loss_limit:
        automatic_halts.append(
            AutomaticHalt(
                scope=SafetyScope.ACCOUNT,
                scope_key=account_scope_key,
                reason="daily realized loss limit reached",
            )
        )
    if context.drawdown_fraction >= context.drawdown_limit_fraction:
        automatic_halts.append(
            AutomaticHalt(
                scope=SafetyScope.ACCOUNT,
                scope_key=account_scope_key,
                reason="rolling drawdown limit reached",
            )
        )
    if context.reconciliation_status is ReconciliationStatus.DISCREPANCY:
        automatic_halts.append(
            AutomaticHalt(
                scope=SafetyScope.ACCOUNT,
                scope_key=account_scope_key,
                reason="local ledger disagrees with venue account state",
            )
        )

    status = (
        SafetyGateStatus.BLOCKED
        if any(check.status is SafetyCheckStatus.BLOCK for check in checks)
        else SafetyGateStatus.PASS
    )
    return SafetyEvaluation(
        status=status,
        checks=tuple(checks),
        automatic_halts=tuple(automatic_halts),
    )
