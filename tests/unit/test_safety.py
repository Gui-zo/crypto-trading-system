"""Safety-gate tests.

The shape of every test here is: start from a context that passes, break exactly
one thing, and assert that the named check blocks. That isolation matters —
``evaluate_safety`` never short-circuits, so a test that breaks two things cannot
prove which check caught which.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from domain.modes import TradingMode
from domain.safety import (
    ReconciliationStatus,
    SafetyCheckStatus,
    SafetyContext,
    SafetyControlAction,
    SafetyControlState,
    SafetyEvaluation,
    SafetyGateStatus,
    SafetyScope,
    SafetyScopeRef,
    evaluate_safety,
    normalize_scope_key,
)

NOW = datetime(2026, 8, 9, 16, 0, tzinfo=UTC)


def healthy_context(**overrides: object) -> SafetyContext:
    defaults: dict[str, object] = {
        "evaluated_at": NOW,
        "mode": TradingMode.PAPER,
        "venue_enabled": True,
        "instrument_status": "TRADING",
        "instrument_spec_status": "APPROVED",
        "trading_environment": "testnet",
        "data_environment": "testnet",
        "mark_price_recorded_at": NOW - timedelta(seconds=5),
        "funding_recorded_at": NOW - timedelta(minutes=30),
        "account_state_recorded_at": NOW - timedelta(seconds=10),
        "provider_keys": ("binance-fapi",),
        "reconciliation_status": ReconciliationStatus.RECONCILED,
        "available_margin": Decimal("1000"),
        "daily_realized_pnl": Decimal("0"),
        "daily_loss_limit": Decimal("100"),
        "drawdown_fraction": Decimal("0.01"),
        "drawdown_limit_fraction": Decimal("0.10"),
        "account_key": "MAIN",
        "scope_refs": (),
    }
    defaults.update(overrides)
    return SafetyContext(**defaults)  # type: ignore[arg-type]


def status_of(evaluation: SafetyEvaluation, name: str) -> SafetyCheckStatus:
    matching = [check for check in evaluation.checks if check.name == name]
    assert matching, f"no check named {name}"
    return matching[0].status


def test_a_healthy_context_passes_every_check() -> None:
    evaluation = evaluate_safety(healthy_context())
    assert evaluation.status is SafetyGateStatus.PASS
    assert evaluation.blocked_reasons == ()
    assert evaluation.automatic_halts == ()


def test_every_check_is_evaluated_even_after_one_blocks() -> None:
    """A blocked gate must still report the full picture, not the first failure."""
    evaluation = evaluate_safety(
        healthy_context(
            venue_enabled=False,
            instrument_status="BREAK",
            reconciliation_status=ReconciliationStatus.UNKNOWN,
        )
    )
    assert evaluation.status is SafetyGateStatus.BLOCKED
    assert len(evaluation.blocked_reasons) == 3
    assert status_of(evaluation, "MODE_ALLOWED") is SafetyCheckStatus.PASS


@pytest.mark.parametrize(
    "mode",
    [TradingMode.RESEARCH, TradingMode.BACKTEST, TradingMode.HALTED],
)
def test_non_originating_modes_block(mode: TradingMode) -> None:
    evaluation = evaluate_safety(healthy_context(mode=mode))
    assert status_of(evaluation, "MODE_ALLOWED") is SafetyCheckStatus.BLOCK
    assert evaluation.status is SafetyGateStatus.BLOCKED


def test_environment_mismatch_blocks() -> None:
    """Testnet data must never be used for a production decision (ADR-0010)."""
    evaluation = evaluate_safety(
        healthy_context(trading_environment="production", data_environment="testnet")
    )
    assert status_of(evaluation, "VENUE_ENVIRONMENT_SCOPE") is SafetyCheckStatus.BLOCK


def test_matching_environments_pass_in_either_environment() -> None:
    for env in ("testnet", "production"):
        evaluation = evaluate_safety(
            healthy_context(trading_environment=env, data_environment=env)
        )
        assert status_of(evaluation, "VENUE_ENVIRONMENT_SCOPE") is SafetyCheckStatus.PASS


def test_a_blank_trading_environment_blocks_rather_than_matching_blank_data() -> None:
    evaluation = evaluate_safety(healthy_context(trading_environment="", data_environment=""))
    assert status_of(evaluation, "VENUE_ENVIRONMENT_SCOPE") is SafetyCheckStatus.BLOCK


@pytest.mark.parametrize("status", ["BREAK", "HALT", "SETTLING", "PENDING_TRADING", ""])
def test_any_instrument_status_other_than_trading_blocks(status: str) -> None:
    evaluation = evaluate_safety(healthy_context(instrument_status=status))
    assert status_of(evaluation, "INSTRUMENT_TRADING") is SafetyCheckStatus.BLOCK


def test_unapproved_instrument_specs_block() -> None:
    evaluation = evaluate_safety(healthy_context(instrument_spec_status="PENDING_REVIEW"))
    assert status_of(evaluation, "INSTRUMENT_SPEC_APPROVED") is SafetyCheckStatus.BLOCK


@pytest.mark.parametrize(
    ("field", "check", "age"),
    [
        ("mark_price_recorded_at", "MARK_PRICE_CURRENT", timedelta(minutes=5)),
        ("funding_recorded_at", "FUNDING_CURRENT", timedelta(hours=9)),
        ("account_state_recorded_at", "ACCOUNT_STATE_CURRENT", timedelta(minutes=5)),
    ],
)
def test_each_input_stales_independently(field: str, check: str, age: timedelta) -> None:
    evaluation = evaluate_safety(healthy_context(**{field: NOW - age}))
    assert status_of(evaluation, check) is SafetyCheckStatus.BLOCK


def test_mark_price_tolerance_is_far_tighter_than_funding_tolerance() -> None:
    """A 30-minute-old mark price is lethal; a 30-minute-old funding rate is fine."""
    context = healthy_context(
        mark_price_recorded_at=NOW - timedelta(minutes=30),
        funding_recorded_at=NOW - timedelta(minutes=30),
    )
    evaluation = evaluate_safety(context)
    assert status_of(evaluation, "MARK_PRICE_CURRENT") is SafetyCheckStatus.BLOCK
    assert status_of(evaluation, "FUNDING_CURRENT") is SafetyCheckStatus.PASS


def test_future_dated_input_blocks_on_drift_not_just_staleness() -> None:
    evaluation = evaluate_safety(
        healthy_context(mark_price_recorded_at=NOW + timedelta(minutes=10))
    )
    assert status_of(evaluation, "MARK_PRICE_CLOCK_DRIFT") is SafetyCheckStatus.BLOCK
    assert status_of(evaluation, "MARK_PRICE_CURRENT") is SafetyCheckStatus.BLOCK


def test_small_forward_drift_is_tolerated() -> None:
    evaluation = evaluate_safety(
        healthy_context(mark_price_recorded_at=NOW + timedelta(seconds=30))
    )
    assert status_of(evaluation, "MARK_PRICE_CLOCK_DRIFT") is SafetyCheckStatus.PASS
    assert status_of(evaluation, "MARK_PRICE_CURRENT") is SafetyCheckStatus.PASS


def test_missing_provider_lineage_blocks() -> None:
    evaluation = evaluate_safety(healthy_context(provider_keys=()))
    assert status_of(evaluation, "DATA_PROVIDER_LINEAGE") is SafetyCheckStatus.BLOCK


@pytest.mark.parametrize(
    "status", [ReconciliationStatus.UNKNOWN, ReconciliationStatus.DISCREPANCY]
)
def test_anything_short_of_reconciled_blocks(status: ReconciliationStatus) -> None:
    evaluation = evaluate_safety(healthy_context(reconciliation_status=status))
    assert status_of(evaluation, "LEDGER_RECONCILED") is SafetyCheckStatus.BLOCK


def test_only_a_real_discrepancy_latches_an_automatic_halt() -> None:
    """UNKNOWN blocks but does not halt; a proven discrepancy latches."""
    unknown = evaluate_safety(healthy_context(reconciliation_status=ReconciliationStatus.UNKNOWN))
    assert unknown.automatic_halts == ()

    discrepant = evaluate_safety(
        healthy_context(reconciliation_status=ReconciliationStatus.DISCREPANCY)
    )
    reasons = [halt.reason for halt in discrepant.automatic_halts]
    assert reasons == ["local ledger disagrees with venue account state"]
    assert discrepant.automatic_halts[0].scope is SafetyScope.ACCOUNT
    assert discrepant.automatic_halts[0].scope_key == "MAIN"


def test_negative_margin_blocks_and_halts() -> None:
    evaluation = evaluate_safety(healthy_context(available_margin=Decimal("-1")))
    assert status_of(evaluation, "AVAILABLE_MARGIN") is SafetyCheckStatus.BLOCK
    assert any("negative available margin" in halt.reason for halt in evaluation.automatic_halts)


def test_daily_loss_limit_blocks_at_the_limit_not_past_it() -> None:
    at_limit = evaluate_safety(
        healthy_context(daily_realized_pnl=Decimal("-100"), daily_loss_limit=Decimal("100"))
    )
    assert status_of(at_limit, "DAILY_LOSS") is SafetyCheckStatus.BLOCK

    below = evaluate_safety(
        healthy_context(daily_realized_pnl=Decimal("-99.99"), daily_loss_limit=Decimal("100"))
    )
    assert status_of(below, "DAILY_LOSS") is SafetyCheckStatus.PASS


def test_profit_never_counts_toward_the_daily_loss_limit() -> None:
    evaluation = evaluate_safety(healthy_context(daily_realized_pnl=Decimal("5000")))
    assert status_of(evaluation, "DAILY_LOSS") is SafetyCheckStatus.PASS


def test_drawdown_limit_blocks_at_the_limit() -> None:
    evaluation = evaluate_safety(
        healthy_context(
            drawdown_fraction=Decimal("0.10"), drawdown_limit_fraction=Decimal("0.10")
        )
    )
    assert status_of(evaluation, "ROLLING_DRAWDOWN") is SafetyCheckStatus.BLOCK
    assert any("drawdown" in halt.reason for halt in evaluation.automatic_halts)


def test_an_active_kill_switch_blocks_its_scope() -> None:
    scope_ref = SafetyScopeRef(SafetyScope.MARKET, "BTCUSDT")
    control = SafetyControlState(
        event_id=uuid.uuid4(),
        sequence_number=7,
        scope=SafetyScope.MARKET,
        scope_key="BTCUSDT",
        action=SafetyControlAction.ACTIVATE,
        reason="manual halt",
    )
    evaluation = evaluate_safety(
        healthy_context(scope_refs=(scope_ref,)), scoped_controls=(control,)
    )
    assert status_of(evaluation, "KILL_SWITCH_MARKET") is SafetyCheckStatus.BLOCK
    assert "manual halt" in evaluation.blocked_reasons[0]


def test_a_cleared_kill_switch_does_not_block() -> None:
    scope_ref = SafetyScopeRef(SafetyScope.MARKET, "BTCUSDT")
    control = SafetyControlState(
        event_id=uuid.uuid4(),
        sequence_number=8,
        scope=SafetyScope.MARKET,
        scope_key="BTCUSDT",
        action=SafetyControlAction.CLEAR,
        reason="resolved",
    )
    evaluation = evaluate_safety(
        healthy_context(scope_refs=(scope_ref,)), scoped_controls=(control,)
    )
    assert status_of(evaluation, "KILL_SWITCH_MARKET") is SafetyCheckStatus.PASS


def test_a_scope_with_no_event_at_all_passes() -> None:
    evaluation = evaluate_safety(
        healthy_context(scope_refs=(SafetyScopeRef(SafetyScope.VENUE, "BINANCE"),))
    )
    assert status_of(evaluation, "KILL_SWITCH_VENUE") is SafetyCheckStatus.PASS


def test_a_control_for_a_different_key_does_not_block_this_one() -> None:
    control = SafetyControlState(
        event_id=uuid.uuid4(),
        sequence_number=9,
        scope=SafetyScope.MARKET,
        scope_key="ETHUSDT",
        action=SafetyControlAction.ACTIVATE,
        reason="unrelated halt",
    )
    evaluation = evaluate_safety(
        healthy_context(scope_refs=(SafetyScopeRef(SafetyScope.MARKET, "BTCUSDT"),)),
        scoped_controls=(control,),
    )
    assert status_of(evaluation, "KILL_SWITCH_MARKET") is SafetyCheckStatus.PASS


def test_all_eight_scopes_are_evaluable() -> None:
    refs = tuple(
        SafetyScopeRef(scope, "*" if scope is SafetyScope.GLOBAL else f"KEY_{scope.value}")
        for scope in SafetyScope
    )
    evaluation = evaluate_safety(healthy_context(scope_refs=refs))
    names = {check.name for check in evaluation.checks}
    for scope in SafetyScope:
        assert f"KILL_SWITCH_{scope.value}" in names


def test_scope_key_normalization() -> None:
    assert normalize_scope_key(SafetyScope.GLOBAL, "") == "*"
    assert normalize_scope_key(SafetyScope.GLOBAL, "global") == "*"
    assert normalize_scope_key(SafetyScope.MARKET, " btcusdt ") == "BTCUSDT"
    with pytest.raises(ValueError, match="global safety scope key"):
        normalize_scope_key(SafetyScope.GLOBAL, "BTCUSDT")
    with pytest.raises(ValueError, match="requires a specific key"):
        normalize_scope_key(SafetyScope.MARKET, "*")


@pytest.mark.parametrize(
    "field",
    ["max_mark_price_age", "max_funding_age", "max_account_state_age"],
)
def test_a_nonpositive_freshness_tolerance_is_rejected(field: str) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        evaluate_safety(healthy_context(**{field: timedelta(0)}))


def test_a_negative_daily_loss_limit_is_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        evaluate_safety(healthy_context(daily_loss_limit=Decimal("-1")))


def test_a_negative_drift_tolerance_is_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        evaluate_safety(healthy_context(), clock_drift_tolerance=timedelta(seconds=-1))
