"""Immutable Phase-3 market-data persistence and quality monitoring.

Archive and REST rows are stored independently because their schemas and
provenance differ. Canonical reads merge them only when every overlapping exact
fact agrees; disagreement raises instead of allowing source order to choose the
backtest's history.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from db.models import (
    BookTickerSnapshotRecord,
    FundingRateObservationRecord,
    KlineObservationRecord,
    MarketDataQualityAssessmentRecord,
    MarketDataSourceArtifactRecord,
    MarkPriceSnapshotRecord,
)
from domain.instrument import InstrumentRef, VenueEnvironment, VenueScope
from domain.market_data import (
    BookTicker,
    FundingRateObservation,
    Kline,
    MarketDataSource,
    MarkPriceSnapshot,
    kline_interval,
)


class DataQualityStatus(StrEnum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"


class MarketDataConflict(RuntimeError):
    """Two retained sources disagree about the same logical market fact."""


@dataclass(frozen=True, slots=True)
class MarketDataArtifact:
    source_type: MarketDataSource
    dataset: str
    market: str
    symbol: str
    source_url: str
    raw_key: str
    raw_sha256: str
    raw_size: int
    fetched_at: datetime
    interval: str | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    checksum_key: str | None = None
    checksum_sha256: str | None = None
    expected_payload_sha256: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("dataset", "market", "symbol", "source_url", "raw_key"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"market-data artifact {name} cannot be empty")
        if self.dataset not in {"funding_rate", "kline", "mark_price", "book_ticker"}:
            raise ValueError(f"unsupported market-data dataset {self.dataset!r}")
        if self.market not in {"spot", "usdm"}:
            raise ValueError(f"unsupported market {self.market!r}")
        if len(self.raw_sha256) != 64 or self.raw_size <= 0:
            raise ValueError("market-data artifact requires a SHA-256 and positive size")
        try:
            int(self.raw_sha256, 16)
        except ValueError as exc:
            raise ValueError("market-data raw SHA-256 is not hexadecimal") from exc
        _aware(self.fetched_at, "fetched_at")
        if self.dataset == "kline" and self.interval is None:
            raise ValueError("kline artifact requires an interval")
        if self.dataset != "kline" and self.interval is not None:
            raise ValueError("only a kline artifact can carry an interval")
        if self.source_type is MarketDataSource.ARCHIVE:
            required = (
                self.period_start,
                self.period_end,
                self.checksum_key,
                self.checksum_sha256,
                self.expected_payload_sha256,
            )
            if any(value is None for value in required):
                raise ValueError("archive artifact requires period and checksum provenance")
            assert self.checksum_key is not None and self.checksum_sha256 is not None
            if not self.checksum_key.strip():
                raise ValueError("archive checksum key cannot be empty")
            if len(self.checksum_sha256) != 64:
                raise ValueError("archive checksum object requires a SHA-256")
            try:
                int(self.checksum_sha256, 16)
            except ValueError as exc:
                raise ValueError("archive checksum object SHA-256 is not hexadecimal") from exc
            if self.expected_payload_sha256 != self.raw_sha256:
                raise ValueError("archive expected and retained payload digests differ")
            assert self.period_start is not None and self.period_end is not None
            _aware(self.period_start, "period_start")
            _aware(self.period_end, "period_end")
            if self.period_end <= self.period_start:
                raise ValueError("archive artifact period must be ordered")
        elif any(
            value is not None
            for value in (
                self.checksum_key,
                self.checksum_sha256,
                self.expected_payload_sha256,
                self.period_start,
                self.period_end,
            )
        ):
            raise ValueError("REST artifact cannot carry archive provenance fields")


@dataclass(frozen=True, slots=True)
class IngestionResult:
    artifact_id: uuid.UUID
    status: DataQualityStatus
    rows: int
    inserted: int
    existing: int
    duplicates: int
    gaps: int
    conflicts: int


@dataclass(frozen=True, slots=True)
class MarketDataStatus:
    artifacts: int
    funding_rows: int
    kline_rows: int
    mark_snapshots: int
    book_snapshots: int
    quality_assessments: int
    blocked_assessments: int
    superseded_blocked_assessments: int
    latest_funding_at: datetime | None
    latest_prices_at: datetime | None


class MarketDataRepository:
    def __init__(self, session: AsyncSession, *, environment: str) -> None:
        self._session = session
        self._environment = environment

    async def ingest_funding(
        self,
        observations: tuple[FundingRateObservation, ...],
        *,
        artifact: MarketDataArtifact,
        expected_interval_hours: int | None = None,
    ) -> IngestionResult:
        self._validate_artifact(artifact, dataset="funding_rate", market="usdm")
        symbol = artifact.symbol.strip().upper()
        for observation in observations:
            self._validate_instrument(observation.instrument, symbol=symbol, market="usdm")

        artifact_row = await self._artifact(artifact)
        unique, duplicates, input_conflicts, out_of_order = _unique_funding(observations)
        gaps, gap_details = _funding_gaps(unique, expected_interval_hours)
        boundary_gaps, boundary_details = await self._funding_boundary_gaps(
            symbol,
            unique,
            source_type=artifact.source_type,
            expected_interval_hours=expected_interval_hours,
        )
        gaps += boundary_gaps
        gap_details = (gap_details + boundary_details)[:20]
        existing_rows = await self._funding_existing(symbol, unique)
        existing_by_time = _group_funding(existing_rows)
        source_existing = {
            row.funding_time: row
            for row in existing_rows
            if row.source_type == artifact.source_type.value
        }
        conflicts = input_conflicts
        to_insert: list[FundingRateObservation] = []
        existing = 0
        for observation in unique:
            overlapping = existing_by_time.get(observation.funding_time, ())
            if any(row.funding_rate != observation.funding_rate for row in overlapping):
                conflicts += 1
                continue
            same_source = source_existing.get(observation.funding_time)
            if same_source is None:
                to_insert.append(observation)
            elif _same_funding(same_source, observation):
                existing += 1
            else:
                conflicts += 1

        status = (
            DataQualityStatus.BLOCKED
            if not unique or conflicts or gaps or duplicates or out_of_order
            else DataQualityStatus.PASS
        )
        inserted = 0
        if conflicts == 0:
            for observation in to_insert:
                self._session.add(
                    FundingRateObservationRecord(
                        environment=self._environment,
                        symbol=symbol,
                        funding_time=observation.funding_time,
                        funding_rate=observation.funding_rate,
                        mark_price=observation.mark_price,
                        interval_hours=observation.interval_hours,
                        rate_type=observation.rate_type,
                        source_type=artifact.source_type.value,
                        source_artifact_id=artifact_row.id,
                        collected_at=observation.collected_at,
                    )
                )
                inserted += 1

        assessment = self._assessment(
            artifact_row=artifact_row,
            artifact=artifact,
            status=status,
            rows=len(observations),
            inserted=inserted,
            existing=existing,
            duplicates=duplicates,
            gaps=gaps,
            conflicts=conflicts,
            range_start=unique[0].funding_time if unique else None,
            range_end=unique[-1].funding_time if unique else None,
            details={
                "empty": not unique,
                "out_of_order": out_of_order,
                "gap_examples": gap_details,
            },
        )
        self._session.add(assessment)
        await self._session.flush()
        return IngestionResult(
            artifact_id=artifact_row.id,
            status=status,
            rows=len(observations),
            inserted=inserted,
            existing=existing,
            duplicates=duplicates,
            gaps=gaps,
            conflicts=conflicts,
        )

    async def ingest_klines(
        self,
        klines: tuple[Kline, ...],
        *,
        interval: str,
        artifact: MarketDataArtifact,
    ) -> IngestionResult:
        self._validate_artifact(artifact, dataset="kline", market=artifact.market)
        if artifact.interval != interval:
            raise ValueError("kline artifact interval does not match ingestion interval")
        symbol = artifact.symbol.strip().upper()
        for kline in klines:
            self._validate_instrument(kline.instrument, symbol=symbol, market=artifact.market)
            if not kline.is_closed:
                raise ValueError("an open kline cannot be persisted as historical evidence")

        artifact_row = await self._artifact(artifact)
        unique, duplicates, input_conflicts, out_of_order = _unique_klines(klines)
        gaps, gap_details = _kline_gaps(unique, interval)
        boundary_gaps, boundary_details = await self._kline_boundary_gaps(
            symbol,
            artifact.market,
            interval,
            unique,
            source_type=artifact.source_type,
        )
        gaps += boundary_gaps
        gap_details = (gap_details + boundary_details)[:20]
        existing_rows = await self._kline_existing(symbol, artifact.market, interval, unique)
        existing_by_time = _group_klines(existing_rows)
        source_existing = {
            row.open_time: row
            for row in existing_rows
            if row.source_type == artifact.source_type.value
        }
        conflicts = input_conflicts
        to_insert: list[Kline] = []
        existing = 0
        for kline in unique:
            overlapping = existing_by_time.get(kline.open_time, ())
            if any(not _same_kline(row, kline) for row in overlapping):
                conflicts += 1
                continue
            same_source = source_existing.get(kline.open_time)
            if same_source is None:
                to_insert.append(kline)
            elif _same_kline(same_source, kline):
                existing += 1
            else:
                conflicts += 1

        status = (
            DataQualityStatus.BLOCKED
            if not unique or conflicts or gaps or duplicates or out_of_order
            else DataQualityStatus.PASS
        )
        inserted = 0
        if conflicts == 0:
            for kline in to_insert:
                self._session.add(
                    KlineObservationRecord(
                        environment=self._environment,
                        market=artifact.market,
                        symbol=symbol,
                        interval=interval,
                        open_time=kline.open_time,
                        close_time=kline.close_time,
                        open_price=kline.open,
                        high_price=kline.high,
                        low_price=kline.low,
                        close_price=kline.close,
                        volume=kline.volume,
                        quote_volume=kline.quote_volume,
                        trades=kline.trades,
                        source_type=artifact.source_type.value,
                        source_artifact_id=artifact_row.id,
                        collected_at=kline.collected_at,
                    )
                )
                inserted += 1

        assessment = self._assessment(
            artifact_row=artifact_row,
            artifact=artifact,
            status=status,
            rows=len(klines),
            inserted=inserted,
            existing=existing,
            duplicates=duplicates,
            gaps=gaps,
            conflicts=conflicts,
            range_start=unique[0].open_time if unique else None,
            range_end=unique[-1].close_time if unique else None,
            details={
                "empty": not unique,
                "out_of_order": out_of_order,
                "gap_examples": gap_details,
            },
        )
        self._session.add(assessment)
        await self._session.flush()
        return IngestionResult(
            artifact_id=artifact_row.id,
            status=status,
            rows=len(klines),
            inserted=inserted,
            existing=existing,
            duplicates=duplicates,
            gaps=gaps,
            conflicts=conflicts,
        )

    async def record_mark_price(
        self, snapshot: MarkPriceSnapshot, *, artifact: MarketDataArtifact
    ) -> bool:
        self._validate_artifact(artifact, dataset="mark_price", market="usdm")
        self._validate_instrument(snapshot.instrument, symbol=artifact.symbol, market="usdm")
        artifact_row = await self._artifact(artifact)
        existing = (
            await self._session.execute(
                select(MarkPriceSnapshotRecord).where(
                    MarkPriceSnapshotRecord.environment == self._environment,
                    MarkPriceSnapshotRecord.symbol == snapshot.instrument.symbol,
                    MarkPriceSnapshotRecord.venue_time == snapshot.venue_time,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if not _same_mark(existing, snapshot):
                raise MarketDataConflict("mark-price sources disagree at the same venue time")
            return False
        self._session.add(
            MarkPriceSnapshotRecord(
                environment=self._environment,
                symbol=snapshot.instrument.symbol,
                venue_time=snapshot.venue_time,
                mark_price=snapshot.mark_price,
                index_price=snapshot.index_price,
                last_funding_rate=snapshot.last_funding_rate,
                interest_rate=snapshot.interest_rate,
                next_funding_time=snapshot.next_funding_time,
                estimated_settle_price=snapshot.estimated_settle_price,
                source_artifact_id=artifact_row.id,
                collected_at=snapshot.collected_at,
            )
        )
        await self._session.flush()
        return True

    async def record_book_ticker(self, ticker: BookTicker, *, artifact: MarketDataArtifact) -> bool:
        self._validate_artifact(artifact, dataset="book_ticker", market=artifact.market)
        self._validate_instrument(ticker.instrument, symbol=artifact.symbol, market=artifact.market)
        if ticker.is_crossed:
            raise ValueError("crossed or locked book ticker cannot be persisted")
        artifact_row = await self._artifact(artifact)
        existing = (
            await self._session.execute(
                select(BookTickerSnapshotRecord).where(
                    BookTickerSnapshotRecord.source_artifact_id == artifact_row.id
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if not _same_book(existing, ticker):
                raise MarketDataConflict("one retained artifact maps to two book tickers")
            return False
        self._session.add(
            BookTickerSnapshotRecord(
                environment=self._environment,
                market=artifact.market,
                symbol=ticker.instrument.symbol,
                bid_price=ticker.bid_price,
                bid_quantity=ticker.bid_qty,
                ask_price=ticker.ask_price,
                ask_quantity=ticker.ask_qty,
                venue_time=ticker.venue_time,
                last_update_id=ticker.last_update_id,
                source_artifact_id=artifact_row.id,
                collected_at=ticker.collected_at,
            )
        )
        await self._session.flush()
        return True

    async def funding_series(
        self,
        symbol: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[FundingRateObservation, ...]:
        normalized = symbol.strip().upper()
        query = select(FundingRateObservationRecord).where(
            FundingRateObservationRecord.environment == self._environment,
            FundingRateObservationRecord.symbol == normalized,
        )
        if start is not None:
            query = query.where(FundingRateObservationRecord.funding_time >= start)
        if end is not None:
            query = query.where(FundingRateObservationRecord.funding_time < end)
        rows = (
            (
                await self._session.execute(
                    query.order_by(
                        FundingRateObservationRecord.funding_time,
                        FundingRateObservationRecord.source_type,
                    )
                )
            )
            .scalars()
            .all()
        )
        scope = VenueScope(venue="BINANCE", environment=VenueEnvironment(self._environment))
        instrument = InstrumentRef(scope=scope, symbol=normalized, market="usdm")
        canonical: list[FundingRateObservation] = []
        for funding_time, grouped in _group_funding(rows).items():
            rates = {row.funding_rate for row in grouped}
            if len(rates) != 1:
                raise MarketDataConflict(
                    f"{normalized}: funding sources disagree at {funding_time.isoformat()}"
                )
            rest = next(
                (row for row in grouped if row.source_type == MarketDataSource.REST.value), None
            )
            archive = next(
                (row for row in grouped if row.source_type == MarketDataSource.ARCHIVE.value),
                None,
            )
            preferred = rest or archive
            assert preferred is not None
            canonical.append(
                FundingRateObservation(
                    instrument=instrument,
                    funding_time=funding_time,
                    funding_rate=preferred.funding_rate,
                    collected_at=max(row.collected_at for row in grouped),
                    mark_price=rest.mark_price if rest is not None else None,
                    rate_type=rest.rate_type if rest is not None else None,
                    interval_hours=archive.interval_hours if archive is not None else None,
                )
            )
        return tuple(canonical)

    async def status(self) -> MarketDataStatus:
        # A BLOCKED assessment is point-in-time evidence about the rows visible when
        # it ran. Re-ingesting the same artifact once the surrounding range is
        # complete appends a PASS that supersedes it. The audit row is never edited,
        # so an operator count must exclude the superseded ones or a transient
        # ingest-order gap would read as a permanent data defect.
        later = aliased(MarketDataQualityAssessmentRecord)
        superseded = (
            select(1)
            .where(
                later.environment == MarketDataQualityAssessmentRecord.environment,
                later.source_artifact_id
                == MarketDataQualityAssessmentRecord.source_artifact_id,
                later.status == DataQualityStatus.PASS.value,
                later.evaluated_at > MarketDataQualityAssessmentRecord.evaluated_at,
            )
            .exists()
        )
        blocked_base = select(func.count(MarketDataQualityAssessmentRecord.id)).where(
            MarketDataQualityAssessmentRecord.environment == self._environment,
            MarketDataQualityAssessmentRecord.status == DataQualityStatus.BLOCKED.value,
        )
        blocked = int((await self._session.execute(blocked_base.where(~superseded))).scalar_one())
        superseded_blocked = int(
            (await self._session.execute(blocked_base.where(superseded))).scalar_one()
        )
        return MarketDataStatus(
            artifacts=await self._count_scoped(MarketDataSourceArtifactRecord),
            funding_rows=await self._count_scoped(FundingRateObservationRecord),
            kline_rows=await self._count_scoped(KlineObservationRecord),
            mark_snapshots=await self._count_scoped(MarkPriceSnapshotRecord),
            book_snapshots=await self._count_scoped(BookTickerSnapshotRecord),
            quality_assessments=await self._count_scoped(MarketDataQualityAssessmentRecord),
            blocked_assessments=blocked,
            superseded_blocked_assessments=superseded_blocked,
            latest_funding_at=await self.latest_funding_at(),
            latest_prices_at=await self.latest_prices_at(),
        )

    async def latest_funding_at(self) -> datetime | None:
        return (
            await self._session.execute(
                select(func.max(MarketDataSourceArtifactRecord.fetched_at)).where(
                    MarketDataSourceArtifactRecord.environment == self._environment,
                    MarketDataSourceArtifactRecord.dataset == "funding_rate",
                    MarketDataSourceArtifactRecord.source_type == MarketDataSource.REST.value,
                )
            )
        ).scalar_one_or_none()

    async def latest_prices_at(self) -> datetime | None:
        return (
            await self._session.execute(
                select(func.max(MarketDataSourceArtifactRecord.fetched_at)).where(
                    MarketDataSourceArtifactRecord.environment == self._environment,
                    MarketDataSourceArtifactRecord.dataset.in_(("mark_price", "book_ticker")),
                    MarketDataSourceArtifactRecord.source_type == MarketDataSource.REST.value,
                )
            )
        ).scalar_one_or_none()

    async def _count_scoped(self, model: Any) -> int:
        environment = model.environment
        return int(
            (
                await self._session.execute(
                    select(func.count()).select_from(model).where(environment == self._environment)
                )
            ).scalar_one()
        )

    async def _artifact(self, artifact: MarketDataArtifact) -> MarketDataSourceArtifactRecord:
        existing = (
            await self._session.execute(
                select(MarketDataSourceArtifactRecord).where(
                    MarketDataSourceArtifactRecord.environment == self._environment,
                    MarketDataSourceArtifactRecord.raw_key == artifact.raw_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if (
                existing.raw_sha256 != artifact.raw_sha256
                or existing.raw_size != artifact.raw_size
                or existing.dataset != artifact.dataset
                or existing.source_type != artifact.source_type.value
                or existing.symbol != artifact.symbol.strip().upper()
                or existing.market != artifact.market
                or existing.interval != artifact.interval
                or existing.source_url != artifact.source_url
                or existing.period_start != artifact.period_start
                or existing.period_end != artifact.period_end
                or existing.checksum_key != artifact.checksum_key
                or existing.checksum_sha256 != artifact.checksum_sha256
                or existing.expected_payload_sha256 != artifact.expected_payload_sha256
                or existing.metadata_json != artifact.metadata
            ):
                raise MarketDataConflict("retained raw key was reused for different provenance")
            return existing
        row = MarketDataSourceArtifactRecord(
            environment=self._environment,
            source_type=artifact.source_type.value,
            dataset=artifact.dataset,
            market=artifact.market,
            symbol=artifact.symbol.strip().upper(),
            interval=artifact.interval,
            source_url=artifact.source_url,
            period_start=artifact.period_start,
            period_end=artifact.period_end,
            raw_key=artifact.raw_key,
            raw_sha256=artifact.raw_sha256,
            raw_size=artifact.raw_size,
            checksum_key=artifact.checksum_key,
            checksum_sha256=artifact.checksum_sha256,
            expected_payload_sha256=artifact.expected_payload_sha256,
            fetched_at=artifact.fetched_at,
            metadata_json=artifact.metadata,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def _funding_existing(
        self, symbol: str, observations: tuple[FundingRateObservation, ...]
    ) -> list[FundingRateObservationRecord]:
        if not observations:
            return []
        return list(
            (
                await self._session.execute(
                    select(FundingRateObservationRecord).where(
                        FundingRateObservationRecord.environment == self._environment,
                        FundingRateObservationRecord.symbol == symbol,
                        FundingRateObservationRecord.funding_time >= observations[0].funding_time,
                        FundingRateObservationRecord.funding_time <= observations[-1].funding_time,
                    )
                )
            )
            .scalars()
            .all()
        )

    async def _funding_boundary_gaps(
        self,
        symbol: str,
        observations: tuple[FundingRateObservation, ...],
        *,
        source_type: MarketDataSource,
        expected_interval_hours: int | None,
    ) -> tuple[int, list[str]]:
        if not observations:
            return 0, []
        first, last = observations[0], observations[-1]
        previous = (
            (
                await self._session.execute(
                    select(FundingRateObservationRecord)
                    .where(
                        FundingRateObservationRecord.environment == self._environment,
                        FundingRateObservationRecord.symbol == symbol,
                        FundingRateObservationRecord.source_type == source_type.value,
                        FundingRateObservationRecord.funding_time < first.funding_time,
                    )
                    .order_by(FundingRateObservationRecord.funding_time.desc())
                    .limit(1)
                )
            )
            .scalars()
            .one_or_none()
        )
        following = (
            (
                await self._session.execute(
                    select(FundingRateObservationRecord)
                    .where(
                        FundingRateObservationRecord.environment == self._environment,
                        FundingRateObservationRecord.symbol == symbol,
                        FundingRateObservationRecord.source_type == source_type.value,
                        FundingRateObservationRecord.funding_time > last.funding_time,
                    )
                    .order_by(FundingRateObservationRecord.funding_time)
                    .limit(1)
                )
            )
            .scalars()
            .one_or_none()
        )
        gaps: list[str] = []
        if previous is not None and _funding_transition_has_gap(
            previous.funding_time,
            previous.interval_hours,
            first.funding_time,
            expected_interval_hours,
        ):
            gaps.append(
                f"boundary:{previous.funding_time.isoformat()}->{first.funding_time.isoformat()}"
            )
        if following is not None and _funding_transition_has_gap(
            last.funding_time,
            last.interval_hours,
            following.funding_time,
            expected_interval_hours,
        ):
            gaps.append(
                f"boundary:{last.funding_time.isoformat()}->{following.funding_time.isoformat()}"
            )
        return len(gaps), gaps

    async def _kline_existing(
        self,
        symbol: str,
        market: str,
        interval: str,
        klines: tuple[Kline, ...],
    ) -> list[KlineObservationRecord]:
        if not klines:
            return []
        return list(
            (
                await self._session.execute(
                    select(KlineObservationRecord).where(
                        KlineObservationRecord.environment == self._environment,
                        KlineObservationRecord.market == market,
                        KlineObservationRecord.symbol == symbol,
                        KlineObservationRecord.interval == interval,
                        KlineObservationRecord.open_time >= klines[0].open_time,
                        KlineObservationRecord.open_time <= klines[-1].open_time,
                    )
                )
            )
            .scalars()
            .all()
        )

    async def _kline_boundary_gaps(
        self,
        symbol: str,
        market: str,
        interval: str,
        klines: tuple[Kline, ...],
        *,
        source_type: MarketDataSource,
    ) -> tuple[int, list[str]]:
        if not klines:
            return 0, []
        first, last = klines[0], klines[-1]
        previous = (
            (
                await self._session.execute(
                    select(KlineObservationRecord)
                    .where(
                        KlineObservationRecord.environment == self._environment,
                        KlineObservationRecord.market == market,
                        KlineObservationRecord.symbol == symbol,
                        KlineObservationRecord.interval == interval,
                        KlineObservationRecord.source_type == source_type.value,
                        KlineObservationRecord.open_time < first.open_time,
                    )
                    .order_by(KlineObservationRecord.open_time.desc())
                    .limit(1)
                )
            )
            .scalars()
            .one_or_none()
        )
        following = (
            (
                await self._session.execute(
                    select(KlineObservationRecord)
                    .where(
                        KlineObservationRecord.environment == self._environment,
                        KlineObservationRecord.market == market,
                        KlineObservationRecord.symbol == symbol,
                        KlineObservationRecord.interval == interval,
                        KlineObservationRecord.source_type == source_type.value,
                        KlineObservationRecord.open_time > last.open_time,
                    )
                    .order_by(KlineObservationRecord.open_time)
                    .limit(1)
                )
            )
            .scalars()
            .one_or_none()
        )
        width = kline_interval(interval)
        gaps: list[str] = []
        if previous is not None and previous.open_time + width != first.open_time:
            gaps.append(f"boundary:{previous.open_time.isoformat()}->{first.open_time.isoformat()}")
        if following is not None and last.open_time + width != following.open_time:
            gaps.append(f"boundary:{last.open_time.isoformat()}->{following.open_time.isoformat()}")
        return len(gaps), gaps

    def _assessment(
        self,
        *,
        artifact_row: MarketDataSourceArtifactRecord,
        artifact: MarketDataArtifact,
        status: DataQualityStatus,
        rows: int,
        inserted: int,
        existing: int,
        duplicates: int,
        gaps: int,
        conflicts: int,
        range_start: datetime | None,
        range_end: datetime | None,
        details: dict[str, object],
    ) -> MarketDataQualityAssessmentRecord:
        return MarketDataQualityAssessmentRecord(
            environment=self._environment,
            source_artifact_id=artifact_row.id,
            source_type=artifact.source_type.value,
            dataset=artifact.dataset,
            market=artifact.market,
            symbol=artifact.symbol.strip().upper(),
            interval=artifact.interval,
            range_start=range_start,
            range_end=range_end,
            evaluated_at=datetime.now(UTC),
            status=status.value,
            row_count=rows,
            inserted_count=inserted,
            existing_count=existing,
            duplicate_count=duplicates,
            gap_count=gaps,
            conflict_count=conflicts,
            details_json=details,
        )

    def _validate_artifact(
        self, artifact: MarketDataArtifact, *, dataset: str, market: str
    ) -> None:
        if artifact.dataset != dataset or artifact.market != market:
            raise ValueError("market-data artifact does not match ingestion target")

    def _validate_instrument(self, instrument: InstrumentRef, *, symbol: str, market: str) -> None:
        if instrument.scope.environment.value != self._environment:
            raise ValueError("market-data observation environment does not match repository")
        if instrument.symbol != symbol.strip().upper() or instrument.market != market:
            raise ValueError("market-data observation instrument does not match artifact")


def _aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _funding_values(observation: FundingRateObservation) -> tuple[object, ...]:
    return (
        observation.funding_rate,
        observation.mark_price,
        observation.interval_hours,
        observation.rate_type,
    )


def _unique_funding(
    observations: tuple[FundingRateObservation, ...],
) -> tuple[tuple[FundingRateObservation, ...], int, int, int]:
    indexed: dict[datetime, FundingRateObservation] = {}
    duplicates = conflicts = 0
    out_of_order = sum(
        current.funding_time < previous.funding_time for previous, current in pairwise(observations)
    )
    for observation in observations:
        previous = indexed.get(observation.funding_time)
        if previous is None:
            indexed[observation.funding_time] = observation
        elif _funding_values(previous) == _funding_values(observation):
            duplicates += 1
        else:
            conflicts += 1
    return tuple(indexed[key] for key in sorted(indexed)), duplicates, conflicts, out_of_order


def _funding_gaps(
    observations: tuple[FundingRateObservation, ...], expected_interval_hours: int | None
) -> tuple[int, list[str]]:
    gaps: list[str] = []
    for previous, current in pairwise(observations):
        if _funding_transition_has_gap(
            previous.funding_time,
            previous.interval_hours,
            current.funding_time,
            expected_interval_hours,
        ):
            gaps.append(f"{previous.funding_time.isoformat()}->{current.funding_time.isoformat()}")
    return len(gaps), gaps[:20]


def _funding_transition_has_gap(
    previous_time: datetime,
    interval_hours: int | None,
    current_time: datetime,
    fallback_interval_hours: int | None,
) -> bool:
    cadence = interval_hours or fallback_interval_hours
    if cadence is None:
        return False
    expected = previous_time + timedelta(hours=cadence)
    tolerance = timedelta(minutes=1)
    return not expected - tolerance <= current_time <= expected + tolerance


def _same_funding(row: FundingRateObservationRecord, observation: FundingRateObservation) -> bool:
    return (
        row.funding_rate,
        row.mark_price,
        row.interval_hours,
        row.rate_type,
    ) == _funding_values(observation)


def _group_funding(
    rows: Sequence[FundingRateObservationRecord],
) -> dict[datetime, tuple[FundingRateObservationRecord, ...]]:
    grouped: dict[datetime, list[FundingRateObservationRecord]] = {}
    for row in rows:
        grouped.setdefault(row.funding_time, []).append(row)
    return {key: tuple(value) for key, value in sorted(grouped.items())}


def _kline_values(kline: Kline) -> tuple[object, ...]:
    return (
        kline.close_time,
        kline.open,
        kline.high,
        kline.low,
        kline.close,
        kline.volume,
        kline.quote_volume,
        kline.trades,
    )


def _unique_klines(
    klines: tuple[Kline, ...],
) -> tuple[tuple[Kline, ...], int, int, int]:
    indexed: dict[datetime, Kline] = {}
    duplicates = conflicts = 0
    out_of_order = sum(
        current.open_time < previous.open_time for previous, current in pairwise(klines)
    )
    for kline in klines:
        previous = indexed.get(kline.open_time)
        if previous is None:
            indexed[kline.open_time] = kline
        elif _kline_values(previous) == _kline_values(kline):
            duplicates += 1
        else:
            conflicts += 1
    return tuple(indexed[key] for key in sorted(indexed)), duplicates, conflicts, out_of_order


def _kline_gaps(klines: tuple[Kline, ...], interval: str) -> tuple[int, list[str]]:
    width = kline_interval(interval)
    gaps = [
        f"{previous.open_time.isoformat()}->{current.open_time.isoformat()}"
        for previous, current in pairwise(klines)
        if current.open_time != previous.open_time + width
    ]
    return len(gaps), gaps[:20]


def _same_kline(row: KlineObservationRecord, kline: Kline) -> bool:
    return (
        row.close_time,
        row.open_price,
        row.high_price,
        row.low_price,
        row.close_price,
        row.volume,
        row.quote_volume,
        row.trades,
    ) == _kline_values(kline)


def _group_klines(
    rows: Sequence[KlineObservationRecord],
) -> dict[datetime, tuple[KlineObservationRecord, ...]]:
    grouped: dict[datetime, list[KlineObservationRecord]] = {}
    for row in rows:
        grouped.setdefault(row.open_time, []).append(row)
    return {key: tuple(value) for key, value in sorted(grouped.items())}


def _same_mark(row: MarkPriceSnapshotRecord, snapshot: MarkPriceSnapshot) -> bool:
    return (
        row.mark_price,
        row.index_price,
        row.last_funding_rate,
        row.interest_rate,
        row.next_funding_time,
        row.estimated_settle_price,
    ) == (
        snapshot.mark_price,
        snapshot.index_price,
        snapshot.last_funding_rate,
        snapshot.interest_rate,
        snapshot.next_funding_time,
        snapshot.estimated_settle_price,
    )


def _same_book(row: BookTickerSnapshotRecord, ticker: BookTicker) -> bool:
    return (
        row.bid_price,
        row.bid_quantity,
        row.ask_price,
        row.ask_quantity,
        row.venue_time,
        row.last_update_id,
    ) == (
        ticker.bid_price,
        ticker.bid_qty,
        ticker.ask_price,
        ticker.ask_qty,
        ticker.venue_time,
        ticker.last_update_id,
    )
