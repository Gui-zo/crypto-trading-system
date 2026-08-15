"""Operational-health repository tests against the migrated schema.

Job names are made unique per test so the run-history queries never see rows
from a real cron run committed on this machine.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import OperationalJobRunRecord
from db.operational_health_repo import OperationalHealthRepository
from db.safety_repo import SafetyControlRepository
from domain.operational_health import OperationalJobStatus
from domain.safety import SafetyControlAction, SafetyGateStatus, SafetyScope, SafetyScopeRef

NOW = datetime(2026, 8, 9, 16, 0, tzinfo=UTC)


def unique_job() -> str:
    return f"test-job-{uuid.uuid4().hex[:12]}"


def repo(session: AsyncSession, *, environment: str = "testnet") -> OperationalHealthRepository:
    return OperationalHealthRepository(session, environment=environment)


async def test_a_run_starts_as_running_and_finishes_terminal(db_session: AsyncSession) -> None:
    health = repo(db_session)
    job = unique_job()
    run = await health.start_run(
        job_name=job, command=[job, "--json"], source="CRON", started_at=NOW
    )
    assert run.status is OperationalJobStatus.RUNNING
    assert run.finished_at is None

    finished = await health.finish_run(
        run.run_id,
        status=OperationalJobStatus.SUCCEEDED,
        exit_code=0,
        finished_at=NOW + timedelta(seconds=4),
    )
    assert finished.status is OperationalJobStatus.SUCCEEDED
    assert finished.exit_code == 0


async def test_a_failed_run_retains_its_error(db_session: AsyncSession) -> None:
    health = repo(db_session)
    run = await health.start_run(
        job_name=unique_job(), command=["x"], source="CRON", started_at=NOW
    )
    finished = await health.finish_run(
        run.run_id,
        status=OperationalJobStatus.FAILED,
        exit_code=1,
        finished_at=NOW + timedelta(seconds=1),
        error_type="HTTPError",
        error_message="418 I'm a teapot",
    )
    assert finished.error_type == "HTTPError"
    assert finished.error_message == "418 I'm a teapot"


@pytest.mark.parametrize(
    ("status", "exit_code", "match"),
    [
        (OperationalJobStatus.RUNNING, 0, "cannot finish as RUNNING"),
        (OperationalJobStatus.SUCCEEDED, 1, "requires exit code 0"),
        (OperationalJobStatus.FAILED, 0, "requires a nonzero exit code"),
    ],
)
async def test_inconsistent_terminal_states_are_refused(
    db_session: AsyncSession,
    status: OperationalJobStatus,
    exit_code: int,
    match: str,
) -> None:
    health = repo(db_session)
    run = await health.start_run(
        job_name=unique_job(), command=["x"], source="CRON", started_at=NOW
    )
    with pytest.raises(ValueError, match=match):
        await health.finish_run(run.run_id, status=status, exit_code=exit_code, finished_at=NOW)


async def test_finishing_twice_identically_is_idempotent(db_session: AsyncSession) -> None:
    health = repo(db_session)
    run = await health.start_run(
        job_name=unique_job(), command=["x"], source="CRON", started_at=NOW
    )
    first = await health.finish_run(
        run.run_id, status=OperationalJobStatus.SUCCEEDED, exit_code=0, finished_at=NOW
    )
    second = await health.finish_run(
        run.run_id, status=OperationalJobStatus.SUCCEEDED, exit_code=0, finished_at=NOW
    )
    assert first.run_id == second.run_id


async def test_contradicting_a_terminal_run_is_refused(db_session: AsyncSession) -> None:
    """A finished run's verdict is audit history; it is not re-writable."""
    health = repo(db_session)
    run = await health.start_run(
        job_name=unique_job(), command=["x"], source="CRON", started_at=NOW
    )
    await health.finish_run(
        run.run_id, status=OperationalJobStatus.SUCCEEDED, exit_code=0, finished_at=NOW
    )
    with pytest.raises(RuntimeError, match="already terminal"):
        await health.finish_run(
            run.run_id, status=OperationalJobStatus.FAILED, exit_code=1, finished_at=NOW
        )


async def test_finishing_an_unknown_run_is_refused(db_session: AsyncSession) -> None:
    with pytest.raises(RuntimeError, match="unknown operational job run"):
        await repo(db_session).finish_run(
            uuid.uuid4(), status=OperationalJobStatus.SUCCEEDED, exit_code=0, finished_at=NOW
        )


async def test_consecutive_failures_are_counted_back_to_the_last_success(
    db_session: AsyncSession,
) -> None:
    health = repo(db_session)
    job = unique_job()
    for index, status in enumerate(
        [
            OperationalJobStatus.SUCCEEDED,
            OperationalJobStatus.FAILED,
            OperationalJobStatus.FAILED,
        ]
    ):
        run = await health.start_run(
            job_name=job,
            command=["x"],
            source="CRON",
            started_at=NOW + timedelta(minutes=index),
        )
        await health.finish_run(
            run.run_id,
            status=status,
            exit_code=0 if status is OperationalJobStatus.SUCCEEDED else 1,
            finished_at=NOW + timedelta(minutes=index, seconds=30),
        )

    stats = await health.job_stats(
        job_name=job, evaluated_at=NOW + timedelta(hours=1), maximum_runtime=timedelta(minutes=20)
    )
    assert stats.consecutive_failures == 2
    assert stats.last_success_at is not None


async def test_a_crashed_run_is_detected_as_stale_running(db_session: AsyncSession) -> None:
    """The RUNNING row a dead process leaves behind is the evidence of the crash."""
    health = repo(db_session)
    job = unique_job()
    await health.start_run(job_name=job, command=["x"], source="CRON", started_at=NOW)
    stats = await health.job_stats(
        job_name=job,
        evaluated_at=NOW + timedelta(hours=2),
        maximum_runtime=timedelta(minutes=20),
    )
    assert stats.stale_running_count == 1


async def test_a_later_success_retires_an_older_crash(db_session: AsyncSession) -> None:
    health = repo(db_session)
    job = unique_job()
    await health.start_run(job_name=job, command=["x"], source="CRON", started_at=NOW)
    later = await health.start_run(
        job_name=job, command=["x"], source="CRON", started_at=NOW + timedelta(minutes=30)
    )
    await health.finish_run(
        later.run_id,
        status=OperationalJobStatus.SUCCEEDED,
        exit_code=0,
        finished_at=NOW + timedelta(minutes=31),
    )
    stats = await health.job_stats(
        job_name=job,
        evaluated_at=NOW + timedelta(hours=2),
        maximum_runtime=timedelta(minutes=20),
    )
    assert stats.stale_running_count == 0


async def test_a_blocked_assessment_latches_a_real_kill_switch(
    db_session: AsyncSession,
) -> None:
    """End to end: watchdog verdict -> append-only halt readable by safety-status."""
    health = repo(db_session)
    job = unique_job()
    scope_key = f"PROVIDER_{uuid.uuid4().hex[:10].upper()}"
    signal = await health.build_signal(
        name="funding",
        job_name=job,
        scope_ref=SafetyScopeRef(SafetyScope.DATA_PROVIDER, scope_key),
        evaluated_at=NOW,
        artifact_at=None,  # nothing collected -> blocked
        maximum_age=timedelta(hours=1),
        failure_threshold=3,
        maximum_runtime=timedelta(minutes=20),
    )
    assessment = await health.evaluate_and_record(
        signals=(signal,), evaluated_at=NOW, trigger_job_run_id=None
    )

    assert assessment.status is SafetyGateStatus.BLOCKED
    assert len(assessment.automatic_control_event_ids) == 1

    controls = SafetyControlRepository(db_session, environment="testnet")
    (state,) = await controls.states_for((SafetyScopeRef(SafetyScope.DATA_PROVIDER, scope_key),))
    assert state.action is SafetyControlAction.ACTIVATE


async def test_a_passing_assessment_is_persisted_too(db_session: AsyncSession) -> None:
    """A gap in this table is the finding; only recording failures would hide it."""
    health = repo(db_session)
    signal = await health.build_signal(
        name="funding",
        job_name=unique_job(),
        scope_ref=SafetyScopeRef(SafetyScope.DATA_PROVIDER, "BINANCE_FAPI"),
        evaluated_at=NOW,
        artifact_at=NOW - timedelta(minutes=1),
        maximum_age=timedelta(hours=1),
        failure_threshold=3,
        maximum_runtime=timedelta(minutes=20),
    )
    assessment = await health.evaluate_and_record(
        signals=(signal,), evaluated_at=NOW, trigger_job_run_id=None
    )
    assert assessment.status is SafetyGateStatus.PASS
    assert assessment.automatic_control_event_ids == ()
    assert assessment.checks  # the full check list survives the JSON round trip


async def test_checks_survive_the_json_round_trip_with_their_values(
    db_session: AsyncSession,
) -> None:
    health = repo(db_session)
    signal = await health.build_signal(
        name="funding",
        job_name=unique_job(),
        scope_ref=SafetyScopeRef(SafetyScope.DATA_PROVIDER, "BINANCE_FAPI"),
        evaluated_at=NOW,
        artifact_at=NOW - timedelta(minutes=1),
        maximum_age=timedelta(hours=1),
        failure_threshold=3,
        maximum_runtime=timedelta(minutes=20),
    )
    assessment = await health.evaluate_and_record(
        signals=(signal,), evaluated_at=NOW, trigger_job_run_id=None
    )
    freshness = [c for c in assessment.checks if c.name == "FUNDING_ARTIFACT_FRESHNESS"]
    assert freshness and freshness[0].limit == 3600.0


async def test_blank_job_metadata_is_refused(db_session: AsyncSession) -> None:
    health = repo(db_session)
    with pytest.raises(ValueError, match="job name cannot be empty"):
        await health.start_run(job_name="  ", command=[], source="CRON", started_at=NOW)
    with pytest.raises(ValueError, match="job source cannot be empty"):
        await health.start_run(job_name=unique_job(), command=[], source="", started_at=NOW)


async def test_the_database_refuses_an_inconsistent_terminal_row(
    db_session: AsyncSession,
) -> None:
    """Defence in depth: the constraint holds even if the repository is bypassed."""
    db_session.add(
        OperationalJobRunRecord(
            job_name="bypass",
            command_json=[],
            source="TEST",
            status="SUCCEEDED",
            started_at=NOW,
            finished_at=None,  # SUCCEEDED requires a finish time
            exit_code=0,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
