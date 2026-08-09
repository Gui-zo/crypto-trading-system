"""Watchdog-policy tests.

The property under test throughout: neither a green process with no fresh data,
nor fresh data with a red process, may pass. Both halves are required, which is
what stops "the cron ran fine" from meaning "the data is current".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from domain.operational_health import (
    OperationalHealthEvaluation,
    OperationalHealthSignal,
    OperationalJobStatus,
    evaluate_operational_health,
)
from domain.safety import (
    SafetyCheckStatus,
    SafetyGateStatus,
    SafetyScope,
    SafetyScopeRef,
)

NOW = datetime(2026, 8, 9, 16, 0, tzinfo=UTC)
SCOPE = SafetyScopeRef(SafetyScope.DATA_PROVIDER, "BINANCE_FAPI")


def signal(**overrides: object) -> OperationalHealthSignal:
    defaults: dict[str, object] = {
        "name": "funding-history",
        "job_name": "record-funding",
        "scope_ref": SCOPE,
        "evaluated_at": NOW,
        "artifact_at": NOW - timedelta(minutes=5),
        "maximum_age": timedelta(hours=1),
        "last_terminal_at": NOW - timedelta(minutes=5),
        "last_success_at": NOW - timedelta(minutes=5),
        "consecutive_failures": 0,
        "failure_threshold": 3,
        "stale_running_count": 0,
        "maximum_runtime": timedelta(minutes=20),
    }
    defaults.update(overrides)
    return OperationalHealthSignal(**defaults)  # type: ignore[arg-type]


def status_of(evaluation: OperationalHealthEvaluation, name: str) -> SafetyCheckStatus:
    matching = [check for check in evaluation.checks if check.name == name]
    assert matching, f"no check named {name}"
    return matching[0].status


def test_a_healthy_producer_passes_without_halting() -> None:
    evaluation = evaluate_operational_health((signal(),))
    assert evaluation.status is SafetyGateStatus.PASS
    assert evaluation.automatic_halts == ()


def test_a_green_job_with_stale_data_still_blocks() -> None:
    """The half of the check a 'log on success' scheme cannot make."""
    evaluation = evaluate_operational_health((signal(artifact_at=NOW - timedelta(hours=3)),))
    assert status_of(evaluation, "FUNDING_HISTORY_ARTIFACT_FRESHNESS") is SafetyCheckStatus.BLOCK
    assert evaluation.status is SafetyGateStatus.BLOCKED


def test_fresh_data_with_a_failing_job_still_blocks() -> None:
    """The other half: legacy data must not mask a producer that stopped working."""
    evaluation = evaluate_operational_health(
        (
            signal(
                last_terminal_at=NOW - timedelta(minutes=1),
                last_success_at=NOW - timedelta(days=2),
            ),
        )
    )
    assert (
        status_of(evaluation, "FUNDING_HISTORY_JOB_SUCCESS_FRESHNESS") is SafetyCheckStatus.BLOCK
    )


def test_missing_data_is_reported_as_missing_not_merely_stale() -> None:
    evaluation = evaluate_operational_health((signal(artifact_at=None),))
    (check,) = [
        c for c in evaluation.checks if c.name == "FUNDING_HISTORY_ARTIFACT_FRESHNESS"
    ]
    assert "missing" in check.detail
    assert check.observed is None


def test_a_first_run_bootstraps_from_a_current_artifact() -> None:
    """No terminal history yet, but the data is there — this must not halt."""
    evaluation = evaluate_operational_health(
        (signal(last_terminal_at=None, last_success_at=None),)
    )
    assert evaluation.status is SafetyGateStatus.PASS


def test_a_first_run_with_no_artifact_blocks() -> None:
    evaluation = evaluate_operational_health(
        (signal(last_terminal_at=None, last_success_at=None, artifact_at=None),)
    )
    assert evaluation.status is SafetyGateStatus.BLOCKED


def test_a_failure_streak_blocks_at_the_threshold() -> None:
    at_threshold = evaluate_operational_health((signal(consecutive_failures=3),))
    assert status_of(at_threshold, "FUNDING_HISTORY_FAILURE_STREAK") is SafetyCheckStatus.BLOCK

    below = evaluate_operational_health((signal(consecutive_failures=2),))
    assert status_of(below, "FUNDING_HISTORY_FAILURE_STREAK") is SafetyCheckStatus.PASS


def test_a_crashed_running_invocation_blocks() -> None:
    evaluation = evaluate_operational_health((signal(stale_running_count=1),))
    assert status_of(evaluation, "FUNDING_HISTORY_STALE_RUNNING") is SafetyCheckStatus.BLOCK


def test_future_dated_data_blocks_rather_than_reading_as_very_fresh() -> None:
    evaluation = evaluate_operational_health((signal(artifact_at=NOW + timedelta(hours=1)),))
    assert status_of(evaluation, "FUNDING_HISTORY_ARTIFACT_FRESHNESS") is SafetyCheckStatus.BLOCK


def test_a_blocked_signal_latches_a_halt_on_its_own_scope() -> None:
    evaluation = evaluate_operational_health((signal(artifact_at=None),))
    (halt,) = evaluation.automatic_halts
    assert halt.scope is SafetyScope.DATA_PROVIDER
    assert halt.scope_key == "BINANCE_FAPI"
    assert "ARTIFACT_FRESHNESS" in halt.reason


def test_each_signal_latches_at_most_one_halt() -> None:
    """A producer failing four ways is one halt, not four log entries of noise."""
    evaluation = evaluate_operational_health(
        (
            signal(
                artifact_at=None,
                last_terminal_at=NOW,
                last_success_at=None,
                consecutive_failures=9,
                stale_running_count=2,
            ),
        )
    )
    assert len(evaluation.automatic_halts) == 1
    assert len(evaluation.blocked_reasons) == 4


def test_signals_are_evaluated_independently() -> None:
    healthy = signal(name="prices", job_name="record-prices")
    broken = signal(name="funding", job_name="record-funding", artifact_at=None)
    evaluation = evaluate_operational_health((healthy, broken))
    assert status_of(evaluation, "PRICES_ARTIFACT_FRESHNESS") is SafetyCheckStatus.PASS
    assert status_of(evaluation, "FUNDING_ARTIFACT_FRESHNESS") is SafetyCheckStatus.BLOCK
    assert len(evaluation.automatic_halts) == 1


def test_names_are_normalized_into_check_prefixes() -> None:
    evaluation = evaluate_operational_health((signal(name="mark-price feed"),))
    assert any(c.name.startswith("MARK_PRICE FEED_") for c in evaluation.checks)


def test_evaluating_no_signals_is_an_error_not_a_pass() -> None:
    with pytest.raises(ValueError, match="at least one"):
        evaluate_operational_health(())


def test_a_negative_drift_tolerance_is_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        evaluate_operational_health((signal(),), clock_drift_tolerance=timedelta(seconds=-1))


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("name", "  ", "name cannot be empty"),
        ("job_name", "", "job name cannot be empty"),
        ("maximum_age", timedelta(0), "maximum age must be positive"),
        ("maximum_runtime", timedelta(0), "maximum runtime must be positive"),
        ("failure_threshold", 0, "failure threshold must be positive"),
        ("consecutive_failures", -1, "counts cannot be negative"),
        ("stale_running_count", -1, "counts cannot be negative"),
    ],
)
def test_signal_construction_validates_its_inputs(
    field: str, value: object, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        signal(**{field: value})


def test_job_status_vocabulary_is_closed() -> None:
    assert {status.value for status in OperationalJobStatus} == {
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
    }
