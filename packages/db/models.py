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
from decimal import Decimal

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
    Numeric,
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


class MarketDataSourceArtifactRecord(Base):
    """One immutable retained REST response or checksum-verified archive object."""

    __tablename__ = "market_data_source_artifacts"
    __table_args__ = (
        UniqueConstraint("environment", "raw_key"),
        UniqueConstraint("id", "environment"),
        CheckConstraint(_VALID_ENVIRONMENT, name="valid_environment"),
        CheckConstraint("source_type IN ('ARCHIVE', 'REST')", name="valid_source_type"),
        CheckConstraint(
            "dataset IN ('funding_rate', 'kline', 'mark_price', 'book_ticker')",
            name="valid_dataset",
        ),
        CheckConstraint("market IN ('spot', 'usdm')", name="valid_market"),
        CheckConstraint("length(symbol) > 0", name="nonempty_symbol"),
        CheckConstraint("length(source_url) > 0", name="nonempty_source_url"),
        CheckConstraint("length(raw_key) > 0", name="nonempty_raw_key"),
        CheckConstraint("length(raw_sha256) = 64", name="valid_raw_sha256"),
        CheckConstraint("raw_size > 0", name="positive_raw_size"),
        CheckConstraint(
            "(dataset = 'kline' AND interval IS NOT NULL) OR "
            "(dataset <> 'kline' AND interval IS NULL)",
            name="interval_only_for_klines",
        ),
        CheckConstraint(
            "(source_type = 'ARCHIVE' AND checksum_key IS NOT NULL AND "
            "checksum_sha256 IS NOT NULL AND expected_payload_sha256 = raw_sha256 AND "
            "period_start IS NOT NULL AND period_end IS NOT NULL AND period_end > period_start) OR "
            "(source_type = 'REST' AND checksum_key IS NULL AND checksum_sha256 IS NULL AND "
            "expected_payload_sha256 IS NULL AND period_start IS NULL AND period_end IS NULL)",
            name="source_specific_provenance",
        ),
        CheckConstraint(
            "checksum_sha256 IS NULL OR length(checksum_sha256) = 64",
            name="valid_checksum_sha256",
        ),
        CheckConstraint(
            "expected_payload_sha256 IS NULL OR length(expected_payload_sha256) = 64",
            name="valid_expected_payload_sha256",
        ),
        Index(
            "ix_market_data_source_artifacts_lookup",
            "environment",
            "dataset",
            "market",
            "symbol",
            "fetched_at",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    environment: Mapped[str] = mapped_column(String(32))
    source_type: Mapped[str] = mapped_column(String(16))
    dataset: Mapped[str] = mapped_column(String(32))
    market: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(64))
    interval: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source_url: Mapped[str] = mapped_column(Text)
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_key: Mapped[str] = mapped_column(Text)
    raw_sha256: Mapped[str] = mapped_column(String(64))
    raw_size: Mapped[int] = mapped_column(BigInteger)
    checksum_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expected_payload_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FundingRateObservationRecord(Base):
    """One immutable settled funding fact from one independently retained source."""

    __tablename__ = "funding_rate_observations"
    __table_args__ = (
        UniqueConstraint("environment", "symbol", "funding_time", "source_type"),
        CheckConstraint(_VALID_ENVIRONMENT, name="valid_environment"),
        CheckConstraint("source_type IN ('ARCHIVE', 'REST')", name="valid_source_type"),
        CheckConstraint("length(symbol) > 0", name="nonempty_symbol"),
        CheckConstraint("mark_price IS NULL OR mark_price > 0", name="positive_mark_price"),
        CheckConstraint(
            "interval_hours IS NULL OR (interval_hours > 0 AND MOD(24, interval_hours) = 0)",
            name="valid_interval_hours",
        ),
        ForeignKeyConstraint(
            ("source_artifact_id", "environment"),
            ("market_data_source_artifacts.id", "market_data_source_artifacts.environment"),
        ),
        Index(
            "ix_funding_rate_observations_series",
            "environment",
            "symbol",
            "funding_time",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    environment: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(64))
    funding_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    funding_rate: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    mark_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    interval_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rate_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_type: Mapped[str] = mapped_column(String(16))
    source_artifact_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KlineObservationRecord(Base):
    """One closed OHLCV candle from archive or REST."""

    __tablename__ = "kline_observations"
    __table_args__ = (
        UniqueConstraint("environment", "market", "symbol", "interval", "open_time", "source_type"),
        CheckConstraint(_VALID_ENVIRONMENT, name="valid_environment"),
        CheckConstraint("market IN ('spot', 'usdm')", name="valid_market"),
        CheckConstraint("source_type IN ('ARCHIVE', 'REST')", name="valid_source_type"),
        CheckConstraint("length(symbol) > 0", name="nonempty_symbol"),
        CheckConstraint("length(interval) > 0", name="nonempty_interval"),
        CheckConstraint("close_time > open_time", name="ordered_times"),
        CheckConstraint(
            "open_price > 0 AND high_price > 0 AND low_price > 0 AND close_price > 0",
            name="positive_prices",
        ),
        CheckConstraint(
            "high_price >= open_price AND high_price >= close_price AND high_price >= low_price",
            name="valid_high",
        ),
        CheckConstraint(
            "low_price <= open_price AND low_price <= close_price AND low_price <= high_price",
            name="valid_low",
        ),
        CheckConstraint("volume >= 0 AND quote_volume >= 0", name="nonnegative_volumes"),
        CheckConstraint("trades >= 0", name="nonnegative_trades"),
        ForeignKeyConstraint(
            ("source_artifact_id", "environment"),
            ("market_data_source_artifacts.id", "market_data_source_artifacts.environment"),
        ),
        Index(
            "ix_kline_observations_series",
            "environment",
            "market",
            "symbol",
            "interval",
            "open_time",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    environment: Mapped[str] = mapped_column(String(32))
    market: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(64))
    interval: Mapped[str] = mapped_column(String(16))
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    close_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    open_price: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    high_price: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    low_price: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    close_price: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    volume: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    quote_volume: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    trades: Mapped[int] = mapped_column(BigInteger)
    source_type: Mapped[str] = mapped_column(String(16))
    source_artifact_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MarkPriceSnapshotRecord(Base):
    """One point-in-time mark/index/current-funding observation."""

    __tablename__ = "mark_price_snapshots"
    __table_args__ = (
        UniqueConstraint("environment", "symbol", "venue_time"),
        CheckConstraint(_VALID_ENVIRONMENT, name="valid_environment"),
        CheckConstraint("length(symbol) > 0", name="nonempty_symbol"),
        CheckConstraint("mark_price > 0 AND index_price > 0", name="positive_prices"),
        CheckConstraint(
            "estimated_settle_price IS NULL OR estimated_settle_price > 0",
            name="positive_estimated_settle_price",
        ),
        ForeignKeyConstraint(
            ("source_artifact_id", "environment"),
            ("market_data_source_artifacts.id", "market_data_source_artifacts.environment"),
        ),
        Index("ix_mark_price_snapshots_series", "environment", "symbol", "venue_time"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    environment: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(64))
    venue_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    mark_price: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    index_price: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    last_funding_rate: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    interest_rate: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    next_funding_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    estimated_settle_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    source_artifact_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BookTickerSnapshotRecord(Base):
    """One retained spot or USD-M best-bid/ask observation."""

    __tablename__ = "book_ticker_snapshots"
    __table_args__ = (
        UniqueConstraint("source_artifact_id"),
        CheckConstraint(_VALID_ENVIRONMENT, name="valid_environment"),
        CheckConstraint("market IN ('spot', 'usdm')", name="valid_market"),
        CheckConstraint("length(symbol) > 0", name="nonempty_symbol"),
        CheckConstraint("bid_price > 0 AND ask_price > 0", name="positive_prices"),
        CheckConstraint("bid_quantity >= 0 AND ask_quantity >= 0", name="nonnegative_quantities"),
        CheckConstraint("bid_price < ask_price", name="uncrossed_book"),
        ForeignKeyConstraint(
            ("source_artifact_id", "environment"),
            ("market_data_source_artifacts.id", "market_data_source_artifacts.environment"),
        ),
        Index(
            "ix_book_ticker_snapshots_series",
            "environment",
            "market",
            "symbol",
            "collected_at",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    environment: Mapped[str] = mapped_column(String(32))
    market: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(64))
    bid_price: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    bid_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    ask_price: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    ask_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    venue_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_update_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_artifact_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MarketDataQualityAssessmentRecord(Base):
    """Append-only coverage and integrity verdict for one retained source object."""

    __tablename__ = "market_data_quality_assessments"
    __table_args__ = (
        CheckConstraint(_VALID_ENVIRONMENT, name="valid_environment"),
        CheckConstraint("status IN ('PASS', 'BLOCKED')", name="valid_status"),
        CheckConstraint("source_type IN ('ARCHIVE', 'REST')", name="valid_source_type"),
        CheckConstraint("dataset IN ('funding_rate', 'kline')", name="valid_dataset"),
        CheckConstraint("market IN ('spot', 'usdm')", name="valid_market"),
        CheckConstraint(
            "row_count >= 0 AND inserted_count >= 0 AND existing_count >= 0 AND "
            "duplicate_count >= 0 AND gap_count >= 0 AND conflict_count >= 0",
            name="nonnegative_counts",
        ),
        ForeignKeyConstraint(
            ("source_artifact_id", "environment"),
            ("market_data_source_artifacts.id", "market_data_source_artifacts.environment"),
        ),
        Index(
            "ix_market_data_quality_assessments_lookup",
            "environment",
            "dataset",
            "market",
            "symbol",
            "evaluated_at",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    environment: Mapped[str] = mapped_column(String(32))
    source_artifact_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    source_type: Mapped[str] = mapped_column(String(16))
    dataset: Mapped[str] = mapped_column(String(32))
    market: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(64))
    interval: Mapped[str | None] = mapped_column(String(16), nullable=True)
    range_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    range_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    row_count: Mapped[int] = mapped_column(Integer)
    inserted_count: Mapped[int] = mapped_column(Integer)
    existing_count: Mapped[int] = mapped_column(Integer)
    duplicate_count: Mapped[int] = mapped_column(Integer)
    gap_count: Mapped[int] = mapped_column(Integer)
    conflict_count: Mapped[int] = mapped_column(Integer)
    details_json: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Phase 4 — funding-persistence model provenance, predictions, and evidence.
# ---------------------------------------------------------------------------


class ModelVersionRecord(Base):
    """Immutable, content-addressed identity for one model version (ADR-0021).

    Not environment-scoped: this is code identity, not market data. What the
    version *did* — predictions and evaluations — is scoped, because those are
    keyed by symbol and Binance reuses symbols across environments (ADR-0010).

    The provenance columns are ``NOT NULL`` together on purpose. A schema that
    permits a null artifact beside a populated commit cannot distinguish "this
    model has no trained artifact" from "somebody forgot".
    """

    __tablename__ = "model_versions"
    __table_args__ = (
        UniqueConstraint("content_sha256", name="uq_model_versions_content_sha256"),
        CheckConstraint("length(content_sha256) = 64", name="valid_content_sha256"),
        CheckConstraint("length(source_sha256) = 64", name="valid_source_sha256"),
        CheckConstraint("length(code_commit) = 40", name="valid_code_commit"),
        CheckConstraint("data_row_count > 0 AND data_symbol_count > 0", name="positive_data_scope"),
        CheckConstraint(
            "(training_start IS NULL) = (training_end IS NULL)", name="training_bounds_together"
        ),
        Index("ix_model_versions_semantic_created", "semantic_version", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    content_sha256: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(32))
    semantic_version: Mapped[str] = mapped_column(String(64))
    artifact_uri: Mapped[str] = mapped_column(Text)
    source_sha256: Mapped[str] = mapped_column(String(64))
    source_files_json: Mapped[list[str]] = mapped_column(JSON)
    code_commit: Mapped[str] = mapped_column(String(40))
    data_snapshot_id: Mapped[str] = mapped_column(Text)
    data_row_count: Mapped[int] = mapped_column(Integer)
    data_symbol_count: Mapped[int] = mapped_column(Integer)
    data_range_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data_range_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    parameters_json: Mapped[dict[str, str]] = mapped_column(JSON)
    training_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    training_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    untrained: Mapped[bool] = mapped_column(Boolean)
    provenance_json: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FundingPredictionRecord(Base):
    """One immutable forecast for one decision point under one exact target.

    Identity is (environment, model version, symbol, decision time, target). The
    target is part of the key because a probability under a 0 bps threshold and
    one under 1 bps answer different questions and must never be pooled.

    ``resolved_at`` is when the outcome became knowable — the last settlement of
    the target window. It is stored because the leakage rule is expressed in
    terms of it, so an auditor can re-derive which predictions were legitimately
    available to any later one.
    """

    __tablename__ = "funding_predictions"
    __table_args__ = (
        CheckConstraint(_VALID_ENVIRONMENT, name="valid_environment"),
        CheckConstraint(
            "model_probability >= 0 AND model_probability <= 1",
            name="model_probability_is_a_probability",
        ),
        CheckConstraint(
            "naive_probability >= 0 AND naive_probability <= 1",
            name="naive_probability_is_a_probability",
        ),
        CheckConstraint(
            "climatology_probability >= 0 AND climatology_probability <= 1",
            name="climatology_probability_is_a_probability",
        ),
        CheckConstraint("horizon >= 1", name="positive_horizon"),
        CheckConstraint("prior_cases >= 0 AND matched_cases >= 0", name="nonnegative_evidence"),
        CheckConstraint("resolved_at >= decision_time", name="resolution_follows_decision"),
        UniqueConstraint(
            "environment",
            "model_version_id",
            "symbol",
            "decision_time",
            "threshold_bps",
            "horizon",
            name="uq_funding_predictions_identity",
        ),
        ForeignKeyConstraint(("model_version_id",), ("model_versions.id",)),
        Index("ix_funding_predictions_lookup", "environment", "symbol", "decision_time"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    environment: Mapped[str] = mapped_column(String(32))
    model_version_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    symbol: Mapped[str] = mapped_column(String(64))
    decision_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    threshold_bps: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    horizon: Mapped[int] = mapped_column(Integer)
    model_probability: Mapped[float] = mapped_column(Numeric(9, 8))
    naive_probability: Mapped[float] = mapped_column(Numeric(9, 8))
    climatology_probability: Mapped[float] = mapped_column(Numeric(9, 8))
    outcome: Mapped[bool] = mapped_column(Boolean)
    previous_above: Mapped[bool] = mapped_column(Boolean)
    interval_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_step_hours: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    prior_cases: Mapped[int] = mapped_column(Integer)
    matched_cases: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModelEvaluationRecord(Base):
    """Immutable calibration and skill evidence for one model version.

    ``eligible_status`` is deliberately not a boolean. Archive replay can produce
    a strong number and still be worth nothing toward promotion (ADR-0012), so
    the row says which it is rather than leaving a reader to infer it.
    """

    __tablename__ = "model_evaluations"
    __table_args__ = (
        CheckConstraint(_VALID_ENVIRONMENT, name="valid_environment"),
        CheckConstraint(
            "evidence_source IN ('PAPER_PROSPECTIVE', 'BACKTEST', 'TESTNET', 'SYNTHETIC')",
            name="valid_evidence_source",
        ),
        CheckConstraint(
            "eligible_status IN ('RESEARCH_ONLY', 'PROMOTION_ELIGIBLE')",
            name="valid_eligible_status",
        ),
        CheckConstraint("scored_cases >= 0 AND skipped_cases >= 0", name="nonnegative_case_counts"),
        CheckConstraint("horizon >= 1", name="positive_horizon"),
        UniqueConstraint(
            "environment",
            "model_version_id",
            "threshold_bps",
            "horizon",
            "evidence_source",
            "data_snapshot_id",
            name="uq_model_evaluations_identity",
        ),
        ForeignKeyConstraint(("model_version_id",), ("model_versions.id",)),
        Index("ix_model_evaluations_lookup", "environment", "model_version_id", "evaluated_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    environment: Mapped[str] = mapped_column(String(32))
    model_version_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence_source: Mapped[str] = mapped_column(String(32))
    eligible_status: Mapped[str] = mapped_column(String(32))
    data_snapshot_id: Mapped[str] = mapped_column(Text)
    threshold_bps: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    horizon: Mapped[int] = mapped_column(Integer)
    scored_cases: Mapped[int] = mapped_column(Integer)
    skipped_cases: Mapped[int] = mapped_column(Integer)
    model_brier: Mapped[float] = mapped_column(Numeric(12, 10))
    naive_brier: Mapped[float] = mapped_column(Numeric(12, 10))
    climatology_brier: Mapped[float] = mapped_column(Numeric(12, 10))
    model_ece: Mapped[float] = mapped_column(Numeric(12, 10))
    brier_skill_vs_naive: Mapped[float] = mapped_column(Numeric(14, 10))
    brier_skill_vs_climatology: Mapped[float] = mapped_column(Numeric(14, 10))
    positive_rate: Mapped[float] = mapped_column(Numeric(12, 10))
    details_json: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModelEvaluationSliceRecord(Base):
    """Per-symbol and per-interval skill, so a pooled average cannot hide a hole.

    ADR-0021 gates on the pooled number and records these. The hourly regime
    carries a fraction of the eight-hourly one's sample, and gating on the
    thinnest slice would block promotion on noise — but not looking at all would
    be worse.
    """

    __tablename__ = "model_evaluation_slices"
    __table_args__ = (
        CheckConstraint("dimension IN ('symbol', 'interval')", name="valid_dimension"),
        CheckConstraint("n >= 0", name="nonnegative_n"),
        UniqueConstraint("evaluation_id", "dimension", "label", name="uq_model_evaluation_slices"),
        ForeignKeyConstraint(("evaluation_id",), ("model_evaluations.id",)),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    evaluation_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    dimension: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(String(64))
    n: Mapped[int] = mapped_column(Integer)
    model_brier: Mapped[float] = mapped_column(Numeric(12, 10))
    naive_brier: Mapped[float] = mapped_column(Numeric(12, 10))
    climatology_brier: Mapped[float] = mapped_column(Numeric(12, 10))
    brier_skill_vs_naive: Mapped[float] = mapped_column(Numeric(14, 10))
    brier_skill_vs_climatology: Mapped[float] = mapped_column(Numeric(14, 10))
    positive_rate: Mapped[float] = mapped_column(Numeric(12, 10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModelChampionEventRecord(Base):
    """Append-only champion registry. The current champion is the latest event.

    Same shape as the instrument-catalog review chain (ADR-0017): an explicit
    human decision, never inferred, never edited. A RETIRE leaves no champion
    rather than silently falling back to an earlier one — an older model that was
    superseded for a reason is not a safe default.
    """

    __tablename__ = "model_champion_events"
    __table_args__ = (
        CheckConstraint(_VALID_ENVIRONMENT, name="valid_environment"),
        CheckConstraint("action IN ('PROMOTE', 'RETIRE')", name="valid_action"),
        CheckConstraint("length(trim(actor)) > 0", name="actor_is_present"),
        CheckConstraint("length(trim(reason)) > 0", name="reason_is_present"),
        ForeignKeyConstraint(("model_version_id",), ("model_versions.id",)),
        ForeignKeyConstraint(("evaluation_id",), ("model_evaluations.id",)),
        Index("ix_model_champion_events_current", "environment", "sequence"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    sequence: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    environment: Mapped[str] = mapped_column(String(32))
    model_version_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    evaluation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    action: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CarryProposalRecord(Base):
    """One immutable, explainable sizing decision — approved or refused.

    Refusals are stored, not just approvals. A book that only records what it
    did cannot answer "why were we flat through that window", and the refusal
    reasons are the evidence that the risk engine was working rather than idle.

    ``outcomes_json`` carries every limit's permitted quantity, so a decision can
    be re-argued later without re-running the engine against inputs that have
    since moved.
    """

    __tablename__ = "carry_proposals"
    __table_args__ = (
        CheckConstraint(_VALID_ENVIRONMENT, name="valid_environment"),
        CheckConstraint("quantity >= 0 AND notional >= 0", name="nonnegative_size"),
        CheckConstraint("stress_band > 0", name="positive_stress_band"),
        CheckConstraint(
            "(approved AND quantity > 0) OR (NOT approved AND quantity = 0)",
            name="approval_matches_size",
        ),
        Index("ix_carry_proposals_lookup", "environment", "symbol", "evaluated_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    environment: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(64))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    catalog_sha256: Mapped[str] = mapped_column(String(64))
    mark_price: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    forecast_volatility: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    expected_funding_rate: Mapped[Decimal] = mapped_column(Numeric(18, 12))
    settlements: Mapped[int] = mapped_column(Integer)
    gross_funding_bps: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    total_cost_bps: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    net_carry_bps_on_capital: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    breakeven_funding_bps: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    approved: Mapped[bool] = mapped_column(Boolean)
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    notional: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    perp_margin: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    margin_buffer: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    capital_required: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    stress_band: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    binding_constraint: Mapped[str] = mapped_column(String(32))
    explanation: Mapped[str] = mapped_column(Text)
    limits_json: Mapped[dict[str, str]] = mapped_column(JSON)
    outcomes_json: Mapped[list[dict[str, str]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
