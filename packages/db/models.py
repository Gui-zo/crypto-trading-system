"""ORM models for the append-only audit spine and immutable venue artifacts.

Only the tables the ported safety spine needs to be *durable* are defined here:
the append-only kill-switch event log, durable scheduler invocations, and
watchdog assessments. Market data, instruments, funding history, and the decision
chain arrive with their phases.

Two conventions apply to every table added here later, and both are cheaper to
start with than to retrofit:

* **Timestamps are timezone-aware UTC.** Funding settles on UTC boundaries.
* **Every market-keyed row carries ``environment``** (ADR-0010). Binance reuses
  symbols across testnet and production, so an unscoped row is ambiguous and a
  mixed series is silently corrupt. That includes safety controls: a testnet halt
  must not silence production, and vice versa.

Raw payloads live in the object store; rows keep only the object key.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

#: Every environment-scoped table shares this constraint text, so a typo'd
#: environment value fails at write time rather than at analysis time.
_VALID_ENVIRONMENT = "environment IN ('testnet', 'production')"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


class SafetyControlEventRecord(Base):
    """One append-only kill-switch transition.

    Never updated and never deleted. Current state is the highest
    ``sequence_number`` per ``(environment, scope_type, scope_key)``; clearing a
    halt appends a CLEAR event that points at the event it supersedes. That is
    what makes "who halted this, when, and why" answerable months later.
    """

    __tablename__ = "safety_control_events"
    __table_args__ = (
        UniqueConstraint("sequence_number"),
        CheckConstraint(_VALID_ENVIRONMENT, name="valid_environment"),
        CheckConstraint(
            "scope_type IN ('GLOBAL', 'VENUE', 'STRATEGY', 'CATEGORY', "
            "'MARKET', 'DATA_PROVIDER', 'MODEL_VERSION', 'ACCOUNT')",
            name="valid_scope_type",
        ),
        CheckConstraint("action IN ('ACTIVATE', 'CLEAR')", name="valid_action"),
        CheckConstraint("length(scope_key) > 0", name="nonempty_scope_key"),
        CheckConstraint("length(reason) > 0", name="nonempty_reason"),
        CheckConstraint("length(actor) > 0", name="nonempty_actor"),
        Index(
            "ix_safety_control_events_scope_sequence",
            "environment",
            "scope_type",
            "scope_key",
            "sequence_number",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    sequence_number: Mapped[int] = mapped_column(BigInteger, Identity())
    environment: Mapped[str] = mapped_column(String(32))
    scope_type: Mapped[str] = mapped_column(String(32))
    scope_key: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(64))
    automatic: Mapped[bool] = mapped_column(Boolean)
    supersedes_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("safety_control_events.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OperationalJobRunRecord(Base):
    """One durable CLI/scheduler invocation, including crashed RUNNING state.

    A run row is written *before* the work starts, so a process that dies without
    finishing leaves a RUNNING row behind. That residue is the point: the watchdog
    reads it as evidence of a crash, which a "log on success" scheme cannot see.
    """

    __tablename__ = "operational_job_runs"
    __table_args__ = (
        CheckConstraint("status IN ('RUNNING', 'SUCCEEDED', 'FAILED')", name="valid_status"),
        CheckConstraint("length(job_name) > 0", name="nonempty_job_name"),
        CheckConstraint("length(source) > 0", name="nonempty_source"),
        CheckConstraint(
            "(status = 'RUNNING' AND finished_at IS NULL AND exit_code IS NULL) OR "
            "(status = 'SUCCEEDED' AND finished_at IS NOT NULL AND exit_code = 0) OR "
            "(status = 'FAILED' AND finished_at IS NOT NULL AND exit_code <> 0)",
            name="consistent_terminal_state",
        ),
        Index("ix_operational_job_runs_name_started", "job_name", "started_at"),
        Index("ix_operational_job_runs_status_started", "status", "started_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_name: Mapped[str] = mapped_column(String(64))
    command_json: Mapped[list[str]] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OperationalHealthAssessmentRecord(Base):
    """One watchdog evaluation, persisted whether it passed or blocked.

    Persisting the passing ones matters as much as the blocking ones: a gap in
    this table is itself the finding, and without it "the watchdog was fine"
    is indistinguishable from "the watchdog never ran".
    """

    __tablename__ = "operational_health_assessments"
    __table_args__ = (
        CheckConstraint(_VALID_ENVIRONMENT, name="valid_environment"),
        CheckConstraint("status IN ('PASS', 'BLOCKED')", name="valid_status"),
        Index("ix_operational_health_assessments_status_time", "status", "evaluated_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    trigger_job_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("operational_job_runs.id"), nullable=True
    )
    environment: Mapped[str] = mapped_column(String(32))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    policy_version: Mapped[str] = mapped_column(String(128))
    checks_json: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    automatic_control_event_ids_json: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InstrumentCatalogVersionRecord(Base):
    """One immutable, content-addressed normalized instrument catalog."""

    __tablename__ = "instrument_catalog_versions"
    __table_args__ = (
        UniqueConstraint("environment", "content_sha256"),
        UniqueConstraint("id", "environment"),
        CheckConstraint(_VALID_ENVIRONMENT, name="valid_environment"),
        CheckConstraint("length(content_sha256) = 64", name="valid_content_sha256"),
        CheckConstraint("total_symbols > 0", name="positive_total_symbols"),
        CheckConstraint("candidate_symbols > 0", name="positive_candidate_symbols"),
        CheckConstraint("instrument_count > 0", name="positive_instrument_count"),
        CheckConstraint("excluded_count >= 0", name="nonnegative_excluded_count"),
        CheckConstraint(
            "candidate_symbols = instrument_count + excluded_count",
            name="complete_candidate_accounting",
        ),
        Index(
            "ix_instrument_catalog_versions_environment_created",
            "environment",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    environment: Mapped[str] = mapped_column(String(32))
    content_sha256: Mapped[str] = mapped_column(String(64))
    total_symbols: Mapped[int] = mapped_column(Integer)
    candidate_symbols: Mapped[int] = mapped_column(Integer)
    instrument_count: Mapped[int] = mapped_column(Integer)
    excluded_count: Mapped[int] = mapped_column(Integer)
    catalog_json: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InstrumentCatalogObservationRecord(Base):
    """One sync observation pointing at immutable raw and normalized evidence."""

    __tablename__ = "instrument_catalog_observations"
    __table_args__ = (
        CheckConstraint(_VALID_ENVIRONMENT, name="valid_environment"),
        ForeignKeyConstraint(
            ("catalog_version_id", "environment"),
            (
                "instrument_catalog_versions.id",
                "instrument_catalog_versions.environment",
            ),
        ),
        Index(
            "ix_instrument_catalog_observations_environment_time",
            "environment",
            "observed_at",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    environment: Mapped[str] = mapped_column(String(32))
    catalog_version_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_artifacts_json: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InstrumentCatalogReviewEventRecord(Base):
    """Append-only human approval/rejection of one exact catalog hash."""

    __tablename__ = "instrument_catalog_review_events"
    __table_args__ = (
        UniqueConstraint("sequence_number"),
        CheckConstraint(_VALID_ENVIRONMENT, name="valid_environment"),
        ForeignKeyConstraint(
            ("catalog_version_id", "environment"),
            (
                "instrument_catalog_versions.id",
                "instrument_catalog_versions.environment",
            ),
        ),
        CheckConstraint("action IN ('APPROVE', 'REJECT')", name="valid_action"),
        CheckConstraint("length(actor) > 0", name="nonempty_actor"),
        CheckConstraint("length(reason) > 0", name="nonempty_reason"),
        Index(
            "ix_instrument_catalog_review_events_version_sequence",
            "catalog_version_id",
            "sequence_number",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    sequence_number: Mapped[int] = mapped_column(BigInteger, Identity())
    environment: Mapped[str] = mapped_column(String(32))
    catalog_version_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
