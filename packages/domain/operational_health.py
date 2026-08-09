"""Pure policy for scheduled-job and collected-artifact health.

Ported verbatim from the sibling ``automated-trading-system`` repo; only the
tolerances callers pass in are retuned. This layer is intentionally independent
from cron, PostgreSQL, and providers. It combines durable run history with the
timestamp of the data actually collected; neither a successful process with no
fresh artifact nor fresh legacy data with a new run failure can silently pass
forever.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from domain.safety import (
    AutomaticHalt,
    SafetyCheck,
    SafetyCheckStatus,
    SafetyGateStatus,
    SafetyScopeRef,
)


class OperationalJobStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class OperationalHealthSignal:
    """One scheduled producer and the artifact whose freshness proves its work."""

    name: str
    job_name: str
    scope_ref: SafetyScopeRef
    evaluated_at: datetime
    artifact_at: datetime | None
    maximum_age: timedelta
    last_terminal_at: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int
    failure_threshold: int
    stale_running_count: int
    maximum_runtime: timedelta

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("operational health signal name cannot be empty")
        if not self.job_name.strip():
            raise ValueError("operational health job name cannot be empty")
        if self.maximum_age <= timedelta(0):
            raise ValueError("operational maximum age must be positive")
        if self.maximum_runtime <= timedelta(0):
            raise ValueError("operational maximum runtime must be positive")
        if self.failure_threshold < 1:
            raise ValueError("operational failure threshold must be positive")
        if self.consecutive_failures < 0 or self.stale_running_count < 0:
            raise ValueError("operational failure counts cannot be negative")


@dataclass(frozen=True, slots=True)
class OperationalHealthEvaluation:
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


def _check(
    name: str,
    passed: bool,
    *,
    passed_detail: str,
    blocked_detail: str,
    observed: str | int | float | None = None,
    limit: str | int | float | None = None,
) -> SafetyCheck:
    return SafetyCheck(
        name=name,
        status=SafetyCheckStatus.PASS if passed else SafetyCheckStatus.BLOCK,
        detail=passed_detail if passed else blocked_detail,
        observed=observed,
        limit=limit,
    )


def evaluate_operational_health(
    signals: tuple[OperationalHealthSignal, ...],
    *,
    clock_drift_tolerance: timedelta = timedelta(minutes=5),
) -> OperationalHealthEvaluation:
    """Evaluate every signal and request at most one latched halt per signal."""
    if not signals:
        raise ValueError("at least one operational health signal is required")
    if clock_drift_tolerance < timedelta(0):
        raise ValueError("clock drift tolerance cannot be negative")

    checks: list[SafetyCheck] = []
    automatic_halts: list[AutomaticHalt] = []
    for signal in signals:
        prefix = signal.name.strip().upper().replace("-", "_")
        artifact_age = (
            signal.evaluated_at - signal.artifact_at if signal.artifact_at is not None else None
        )
        artifact_current = (
            artifact_age is not None
            and -clock_drift_tolerance <= artifact_age <= signal.maximum_age
        )
        artifact_check = _check(
            f"{prefix}_ARTIFACT_FRESHNESS",
            artifact_current,
            passed_detail="collected artifact is current",
            blocked_detail=(
                "collected artifact is missing"
                if artifact_age is None
                else "collected artifact is stale or future-dated"
            ),
            observed=(None if artifact_age is None else round(artifact_age.total_seconds(), 3)),
            limit=signal.maximum_age.total_seconds(),
        )
        checks.append(artifact_check)

        if signal.last_terminal_at is None:
            success_current = artifact_current
            success_detail = "no terminal run history; current artifact permits bootstrap"
            success_blocked = "no terminal run history and no current artifact"
            success_age: timedelta | None = None
        else:
            success_age = (
                signal.evaluated_at - signal.last_success_at
                if signal.last_success_at is not None
                else None
            )
            success_current = (
                success_age is not None
                and -clock_drift_tolerance <= success_age <= signal.maximum_age
            )
            success_detail = "latest successful scheduled run is current"
            success_blocked = (
                "scheduled job has no successful terminal run"
                if success_age is None
                else "latest successful scheduled run is stale or future-dated"
            )
        checks.append(
            _check(
                f"{prefix}_JOB_SUCCESS_FRESHNESS",
                success_current,
                passed_detail=success_detail,
                blocked_detail=success_blocked,
                observed=(None if success_age is None else round(success_age.total_seconds(), 3)),
                limit=signal.maximum_age.total_seconds(),
            )
        )

        checks.append(
            _check(
                f"{prefix}_FAILURE_STREAK",
                signal.consecutive_failures < signal.failure_threshold,
                passed_detail="consecutive job failures are below the halt threshold",
                blocked_detail="consecutive job failures reached the halt threshold",
                observed=signal.consecutive_failures,
                limit=signal.failure_threshold,
            )
        )
        checks.append(
            _check(
                f"{prefix}_STALE_RUNNING",
                signal.stale_running_count == 0,
                passed_detail="no crashed or overlong RUNNING invocation remains",
                blocked_detail="a RUNNING invocation exceeded maximum runtime",
                observed=signal.stale_running_count,
                limit=0,
            )
        )

        signal_blocked = any(check.status is SafetyCheckStatus.BLOCK for check in checks[-4:])
        if signal_blocked:
            failed_names = ", ".join(
                check.name for check in checks[-4:] if check.status is SafetyCheckStatus.BLOCK
            )
            automatic_halts.append(
                AutomaticHalt(
                    scope=signal.scope_ref.scope,
                    scope_key=signal.scope_ref.key,
                    reason=f"operational health blocked: {failed_names}",
                )
            )

    return OperationalHealthEvaluation(
        status=(
            SafetyGateStatus.BLOCKED
            if any(check.status is SafetyCheckStatus.BLOCK for check in checks)
            else SafetyGateStatus.PASS
        ),
        checks=tuple(checks),
        automatic_halts=tuple(automatic_halts),
    )
