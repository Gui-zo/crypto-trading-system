"""Durable scheduler invocations and operational-data watchdog persistence.

Adapted from the sibling repo with one deliberate decoupling: the predecessor's
``current_signals`` queries its own weather and quote tables directly, which
hard-wires the watchdog to one phase's data model. Here the repository owns
*run history* — the part that is genuinely generic — and the caller supplies the
artifact timestamps for the producers that exist in its phase. Phase 0 has no
market-data tables yet; Phase 1 will add them without editing this file.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    OperationalHealthAssessmentRecord,
    OperationalJobRunRecord,
)
from db.safety_repo import SafetyControlRepository
from domain.operational_health import (
    OperationalHealthSignal,
    OperationalJobStatus,
    evaluate_operational_health,
)
from domain.safety import (
    SafetyCheck,
    SafetyCheckStatus,
    SafetyControlAction,
    SafetyGateStatus,
    SafetyScopeRef,
)

POLICY_VERSION = "operational-health-v1"


@dataclass(frozen=True, slots=True)
class OperationalJobRun:
    run_id: uuid.UUID
    job_name: str
    source: str
    status: OperationalJobStatus
    started_at: datetime
    finished_at: datetime | None
    exit_code: int | None
    error_type: str | None
    error_message: str | None


@dataclass(frozen=True, slots=True)
class OperationalHealthAssessment:
    assessment_id: uuid.UUID
    evaluated_at: datetime
    status: SafetyGateStatus
    checks: tuple[SafetyCheck, ...]
    automatic_control_event_ids: tuple[uuid.UUID, ...]

    @property
    def blocked_reasons(self) -> tuple[str, ...]:
        return tuple(
            f"{check.name}: {check.detail}"
            for check in self.checks
            if check.status is SafetyCheckStatus.BLOCK
        )


@dataclass(frozen=True, slots=True)
class OperationalHealthSummary:
    job_runs: int
    running: int
    succeeded: int
    failed: int
    assessments: int
    passed_assessments: int
    blocked_assessments: int
    latest_assessed_at: datetime | None
    latest_status: SafetyGateStatus | None


@dataclass(frozen=True, slots=True)
class JobStats:
    """Run history for one scheduled producer, as the watchdog policy needs it."""

    last_terminal_at: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int
    stale_running_count: int


class OperationalHealthRepository:
    def __init__(self, session: AsyncSession, *, environment: str) -> None:
        self._session = session
        self._environment = environment

    @staticmethod
    def _run(row: OperationalJobRunRecord) -> OperationalJobRun:
        return OperationalJobRun(
            run_id=row.id,
            job_name=row.job_name,
            source=row.source,
            status=OperationalJobStatus(row.status),
            started_at=row.started_at,
            finished_at=row.finished_at,
            exit_code=row.exit_code,
            error_type=row.error_type,
            error_message=row.error_message,
        )

    async def start_run(
        self,
        *,
        job_name: str,
        command: list[str],
        source: str,
        started_at: datetime,
    ) -> OperationalJobRun:
        normalized_name = job_name.strip().lower()
        normalized_source = source.strip().upper()
        if not normalized_name:
            raise ValueError("operational job name cannot be empty")
        if not normalized_source:
            raise ValueError("operational job source cannot be empty")
        row = OperationalJobRunRecord(
            job_name=normalized_name,
            command_json=list(command),
            source=normalized_source,
            status=OperationalJobStatus.RUNNING.value,
            started_at=started_at,
            finished_at=None,
            exit_code=None,
            error_type=None,
            error_message=None,
        )
        self._session.add(row)
        await self._session.flush()
        return self._run(row)

    async def finish_run(
        self,
        run_id: uuid.UUID,
        *,
        status: OperationalJobStatus,
        exit_code: int,
        finished_at: datetime,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> OperationalJobRun:
        if status is OperationalJobStatus.RUNNING:
            raise ValueError("an operational run cannot finish as RUNNING")
        if status is OperationalJobStatus.SUCCEEDED and exit_code != 0:
            raise ValueError("a successful operational run requires exit code 0")
        if status is OperationalJobStatus.FAILED and exit_code == 0:
            raise ValueError("a failed operational run requires a nonzero exit code")
        row = await self._session.get(OperationalJobRunRecord, run_id)
        if row is None:
            raise RuntimeError(f"unknown operational job run: {run_id}")
        if row.status != OperationalJobStatus.RUNNING.value:
            if row.status == status.value and row.exit_code == exit_code:
                return self._run(row)
            raise RuntimeError(f"operational job run is already terminal: {run_id}")
        row.status = status.value
        row.finished_at = finished_at
        row.exit_code = exit_code
        row.error_type = error_type
        row.error_message = error_message
        await self._session.flush()
        return self._run(row)

    async def job_stats(
        self,
        *,
        job_name: str,
        evaluated_at: datetime,
        maximum_runtime: timedelta,
    ) -> JobStats:
        terminal_rows = (
            (
                await self._session.execute(
                    select(OperationalJobRunRecord)
                    .where(
                        OperationalJobRunRecord.job_name == job_name,
                        OperationalJobRunRecord.status.in_(
                            (
                                OperationalJobStatus.SUCCEEDED.value,
                                OperationalJobStatus.FAILED.value,
                            )
                        ),
                    )
                    .order_by(OperationalJobRunRecord.finished_at.desc())
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        consecutive_failures = 0
        for row in terminal_rows:
            if row.status == OperationalJobStatus.SUCCEEDED.value:
                break
            consecutive_failures += 1
        last_success_at = (
            await self._session.execute(
                select(func.max(OperationalJobRunRecord.finished_at)).where(
                    OperationalJobRunRecord.job_name == job_name,
                    OperationalJobRunRecord.status == OperationalJobStatus.SUCCEEDED.value,
                )
            )
        ).scalar_one()
        stale_running_filters = [
            OperationalJobRunRecord.job_name == job_name,
            OperationalJobRunRecord.status == OperationalJobStatus.RUNNING.value,
            OperationalJobRunRecord.started_at < evaluated_at - maximum_runtime,
        ]
        if last_success_at is not None:
            # A later success proves that an older crashed invocation no longer
            # represents current producer health; its RUNNING audit row remains.
            stale_running_filters.append(OperationalJobRunRecord.started_at > last_success_at)
        stale_running_count = int(
            (
                await self._session.execute(
                    select(func.count(OperationalJobRunRecord.id)).where(*stale_running_filters)
                )
            ).scalar_one()
        )
        return JobStats(
            last_terminal_at=(terminal_rows[0].finished_at if terminal_rows else None),
            last_success_at=last_success_at,
            consecutive_failures=consecutive_failures,
            stale_running_count=stale_running_count,
        )

    async def build_signal(
        self,
        *,
        name: str,
        job_name: str,
        scope_ref: SafetyScopeRef,
        evaluated_at: datetime,
        artifact_at: datetime | None,
        maximum_age: timedelta,
        failure_threshold: int,
        maximum_runtime: timedelta,
    ) -> OperationalHealthSignal:
        """Combine this job's durable run history with its artifact's freshness."""
        stats = await self.job_stats(
            job_name=job_name,
            evaluated_at=evaluated_at,
            maximum_runtime=maximum_runtime,
        )
        return OperationalHealthSignal(
            name=name,
            job_name=job_name,
            scope_ref=scope_ref,
            evaluated_at=evaluated_at,
            artifact_at=artifact_at,
            maximum_age=maximum_age,
            last_terminal_at=stats.last_terminal_at,
            last_success_at=stats.last_success_at,
            consecutive_failures=stats.consecutive_failures,
            failure_threshold=failure_threshold,
            stale_running_count=stats.stale_running_count,
            maximum_runtime=maximum_runtime,
        )

    async def evaluate_and_record(
        self,
        *,
        signals: tuple[OperationalHealthSignal, ...],
        evaluated_at: datetime,
        trigger_job_run_id: uuid.UUID | None,
    ) -> OperationalHealthAssessment:
        evaluation = evaluate_operational_health(signals)
        controls = SafetyControlRepository(self._session, environment=self._environment)
        automatic_event_ids: list[uuid.UUID] = []
        for halt in evaluation.automatic_halts:
            state = await controls.set_control(
                scope=halt.scope,
                scope_key=halt.scope_key,
                action=SafetyControlAction.ACTIVATE,
                reason=halt.reason,
                actor="operational-health-watchdog",
                source="OPERATIONAL_HEALTH",
                automatic=True,
            )
            automatic_event_ids.append(state.event_id)
        row = OperationalHealthAssessmentRecord(
            trigger_job_run_id=trigger_job_run_id,
            environment=self._environment,
            evaluated_at=evaluated_at,
            status=evaluation.status.value,
            policy_version=POLICY_VERSION,
            checks_json=[check.as_dict() for check in evaluation.checks],
            automatic_control_event_ids_json=[str(event_id) for event_id in automatic_event_ids],
        )
        self._session.add(row)
        await self._session.flush()
        return self._assessment(row)

    @staticmethod
    def _assessment(row: OperationalHealthAssessmentRecord) -> OperationalHealthAssessment:
        return OperationalHealthAssessment(
            assessment_id=row.id,
            evaluated_at=row.evaluated_at,
            status=SafetyGateStatus(row.status),
            checks=tuple(
                SafetyCheck(
                    name=str(item["name"]),
                    status=SafetyCheckStatus(str(item["status"])),
                    detail=str(item["detail"]),
                    observed=cast(str | int | float | bool | None, item.get("observed")),
                    limit=cast(str | int | float | bool | None, item.get("limit")),
                )
                for item in row.checks_json
            ),
            automatic_control_event_ids=tuple(
                uuid.UUID(value) for value in row.automatic_control_event_ids_json
            ),
        )

    async def latest_assessment(self) -> OperationalHealthAssessment | None:
        row = (
            await self._session.execute(
                select(OperationalHealthAssessmentRecord)
                .where(OperationalHealthAssessmentRecord.environment == self._environment)
                .order_by(
                    OperationalHealthAssessmentRecord.evaluated_at.desc(),
                    OperationalHealthAssessmentRecord.created_at.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return self._assessment(row) if row is not None else None

    async def recent_runs(self, *, limit: int = 20) -> list[OperationalJobRun]:
        rows = (
            (
                await self._session.execute(
                    select(OperationalJobRunRecord)
                    .order_by(OperationalJobRunRecord.started_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [self._run(row) for row in rows]

    async def summary(self) -> OperationalHealthSummary:
        runs = (
            await self._session.execute(
                select(
                    func.count(OperationalJobRunRecord.id),
                    func.count(OperationalJobRunRecord.id).filter(
                        OperationalJobRunRecord.status == OperationalJobStatus.RUNNING.value
                    ),
                    func.count(OperationalJobRunRecord.id).filter(
                        OperationalJobRunRecord.status == OperationalJobStatus.SUCCEEDED.value
                    ),
                    func.count(OperationalJobRunRecord.id).filter(
                        OperationalJobRunRecord.status == OperationalJobStatus.FAILED.value
                    ),
                )
            )
        ).one()
        assessments = (
            await self._session.execute(
                select(
                    func.count(OperationalHealthAssessmentRecord.id),
                    func.count(OperationalHealthAssessmentRecord.id).filter(
                        OperationalHealthAssessmentRecord.status == SafetyGateStatus.PASS.value
                    ),
                    func.count(OperationalHealthAssessmentRecord.id).filter(
                        OperationalHealthAssessmentRecord.status == SafetyGateStatus.BLOCKED.value
                    ),
                    func.max(OperationalHealthAssessmentRecord.evaluated_at),
                ).where(OperationalHealthAssessmentRecord.environment == self._environment)
            )
        ).one()
        latest = await self.latest_assessment()
        return OperationalHealthSummary(
            job_runs=int(runs[0]),
            running=int(runs[1]),
            succeeded=int(runs[2]),
            failed=int(runs[3]),
            assessments=int(assessments[0]),
            passed_assessments=int(assessments[1]),
            blocked_assessments=int(assessments[2]),
            latest_assessed_at=assessments[3],
            latest_status=latest.status if latest is not None else None,
        )
