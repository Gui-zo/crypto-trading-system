"""Persistence for immutable, content-addressed instrument catalogs.

The latest *observation* is authoritative. If it points at a new catalog hash,
that version is pending review even when an older version was approved. There is
deliberately no fallback to stale approved specifications: venue change is the
condition under which sizing must stop, not the condition under which it should
quietly keep using yesterday's tiers.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    InstrumentCatalogObservationRecord,
    InstrumentCatalogReviewEventRecord,
    InstrumentCatalogVersionRecord,
)
from domain.instrument import (
    InstrumentCatalog,
    InstrumentReviewAction,
    InstrumentSpecReviewStatus,
)


@dataclass(frozen=True, slots=True)
class CatalogSourceArtifact:
    endpoint: str
    key: str
    sha256: str
    size: int
    fetched_at: datetime

    def __post_init__(self) -> None:
        if not self.endpoint.strip() or not self.key.strip():
            raise ValueError("catalog source endpoint and key cannot be empty")
        if len(self.sha256) != 64:
            raise ValueError("catalog source SHA-256 must contain 64 hexadecimal characters")
        try:
            int(self.sha256, 16)
        except ValueError as exc:
            raise ValueError("catalog source SHA-256 is not hexadecimal") from exc
        if self.size <= 0:
            raise ValueError("catalog source artifact cannot be empty")
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise ValueError("catalog source timestamp must be timezone-aware")

    def as_dict(self) -> dict[str, object]:
        return {
            "endpoint": self.endpoint,
            "key": self.key,
            "sha256": self.sha256,
            "size": self.size,
            "fetched_at": self.fetched_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class InstrumentCatalogStatus:
    version_id: uuid.UUID
    content_sha256: str
    review_status: InstrumentSpecReviewStatus
    total_symbols: int
    candidate_symbols: int
    instrument_count: int
    excluded_count: int
    observed_at: datetime
    catalog: dict[str, object]


@dataclass(frozen=True, slots=True)
class InstrumentCatalogSyncResult:
    status: InstrumentCatalogStatus
    created_new_version: bool
    changed_from_previous: bool


class InstrumentCatalogRepository:
    def __init__(self, session: AsyncSession, *, environment: str) -> None:
        self._session = session
        self._environment = environment

    async def _review_status(self, version_id: uuid.UUID) -> InstrumentSpecReviewStatus:
        action = (
            await self._session.execute(
                select(InstrumentCatalogReviewEventRecord.action)
                .where(InstrumentCatalogReviewEventRecord.catalog_version_id == version_id)
                .order_by(InstrumentCatalogReviewEventRecord.sequence_number.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if action is None:
            return InstrumentSpecReviewStatus.PENDING_REVIEW
        if action == InstrumentReviewAction.APPROVE.value:
            return InstrumentSpecReviewStatus.APPROVED
        return InstrumentSpecReviewStatus.REJECTED

    async def _status(
        self,
        version: InstrumentCatalogVersionRecord,
        *,
        observed_at: datetime,
    ) -> InstrumentCatalogStatus:
        return InstrumentCatalogStatus(
            version_id=version.id,
            content_sha256=version.content_sha256,
            review_status=await self._review_status(version.id),
            total_symbols=version.total_symbols,
            candidate_symbols=version.candidate_symbols,
            instrument_count=version.instrument_count,
            excluded_count=version.excluded_count,
            observed_at=observed_at,
            catalog=version.catalog_json,
        )

    async def current_status(self) -> InstrumentCatalogStatus | None:
        row = (
            await self._session.execute(
                select(InstrumentCatalogObservationRecord, InstrumentCatalogVersionRecord)
                .join(
                    InstrumentCatalogVersionRecord,
                    InstrumentCatalogVersionRecord.id
                    == InstrumentCatalogObservationRecord.catalog_version_id,
                )
                .where(InstrumentCatalogObservationRecord.environment == self._environment)
                .order_by(
                    InstrumentCatalogObservationRecord.observed_at.desc(),
                    InstrumentCatalogObservationRecord.created_at.desc(),
                )
                .limit(1)
            )
        ).one_or_none()
        if row is None:
            return None
        observation, version = row
        return await self._status(version, observed_at=observation.observed_at)

    async def record_catalog(
        self,
        catalog: InstrumentCatalog,
        *,
        sources: tuple[CatalogSourceArtifact, ...],
        observed_at: datetime,
    ) -> InstrumentCatalogSyncResult:
        if catalog.scope.environment.value != self._environment:
            raise ValueError("instrument catalog environment does not match repository scope")
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("catalog observation timestamp must be timezone-aware")
        if not sources:
            raise ValueError("instrument catalog requires retained source artifacts")
        endpoints = [source.endpoint for source in sources]
        if len(endpoints) != len(set(endpoints)):
            raise ValueError("instrument catalog source endpoints must be unique")

        previous = await self.current_status()
        version = (
            await self._session.execute(
                select(InstrumentCatalogVersionRecord).where(
                    InstrumentCatalogVersionRecord.environment == self._environment,
                    InstrumentCatalogVersionRecord.content_sha256 == catalog.content_sha256,
                )
            )
        ).scalar_one_or_none()
        created = version is None
        if version is None:
            version = InstrumentCatalogVersionRecord(
                environment=self._environment,
                content_sha256=catalog.content_sha256,
                total_symbols=catalog.total_symbols,
                candidate_symbols=catalog.candidate_symbols,
                instrument_count=len(catalog.specifications),
                excluded_count=len(catalog.exclusions),
                catalog_json=catalog.as_dict(),
            )
            self._session.add(version)
            await self._session.flush()

        observation = InstrumentCatalogObservationRecord(
            environment=self._environment,
            catalog_version_id=version.id,
            observed_at=observed_at,
            source_artifacts_json=[source.as_dict() for source in sources],
        )
        self._session.add(observation)
        await self._session.flush()
        return InstrumentCatalogSyncResult(
            status=await self._status(version, observed_at=observed_at),
            created_new_version=created,
            changed_from_previous=(
                previous is not None and previous.content_sha256 != version.content_sha256
            ),
        )

    async def review_current(
        self,
        *,
        content_sha256: str,
        action: InstrumentReviewAction,
        actor: str,
        reason: str,
    ) -> InstrumentCatalogStatus:
        current = await self.current_status()
        if current is None:
            raise RuntimeError("no instrument catalog has been synchronized")
        normalized_hash = content_sha256.strip().lower()
        if normalized_hash != current.content_sha256:
            raise RuntimeError(
                "refusing to review a stale instrument catalog; pass the current exact hash"
            )
        normalized_actor = actor.strip()
        normalized_reason = reason.strip()
        if not normalized_actor or not normalized_reason:
            raise ValueError("instrument review actor and reason cannot be empty")

        event = InstrumentCatalogReviewEventRecord(
            environment=self._environment,
            catalog_version_id=current.version_id,
            action=action.value,
            actor=normalized_actor,
            reason=normalized_reason,
        )
        self._session.add(event)
        await self._session.flush()
        version = await self._session.get(InstrumentCatalogVersionRecord, current.version_id)
        if version is None:  # pragma: no cover - protected by the foreign key
            raise RuntimeError("instrument catalog disappeared during review")
        return await self._status(version, observed_at=current.observed_at)

    async def counts(self) -> tuple[int, int, int]:
        versions = int(
            (
                await self._session.execute(
                    select(func.count(InstrumentCatalogVersionRecord.id)).where(
                        InstrumentCatalogVersionRecord.environment == self._environment
                    )
                )
            ).scalar_one()
        )
        observations = int(
            (
                await self._session.execute(
                    select(func.count(InstrumentCatalogObservationRecord.id)).where(
                        InstrumentCatalogObservationRecord.environment == self._environment
                    )
                )
            ).scalar_one()
        )
        reviews = int(
            (
                await self._session.execute(
                    select(func.count(InstrumentCatalogReviewEventRecord.id)).where(
                        InstrumentCatalogReviewEventRecord.environment == self._environment
                    )
                )
            ).scalar_one()
        )
        return versions, observations, reviews
