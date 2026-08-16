"""Persistence for model versions, predictions, evaluations, and the champion.

Follows the repository conventions used everywhere else here: flush but never
commit — the CLI command owns the transaction — and every symbol-keyed read
filters on ``environment`` (ADR-0010).

Three fail-closed rules live in this module rather than in a caller, because a
caller can forget:

* Registering a model version that already exists returns the existing row. It
  never rewrites provenance, because the content hash *is* the identity.
* Persisting a prediction that already exists is a no-op, and a prediction that
  exists with **different values** under the same identity is a conflict, not an
  update.
* A champion promotion requires an evaluation that actually cleared both skill
  gates, and refuses evidence that ADR-0012 says is worth zero.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    FundingPredictionRecord,
    ModelChampionEventRecord,
    ModelEvaluationRecord,
    ModelEvaluationSliceRecord,
    ModelVersionRecord,
)
from domain.errors import DomainError
from domain.funding_model import FundingTarget, ScoredCase, SkillReport, WalkForward
from domain.model_provenance import ModelProvenance
from domain.promotion import (
    REQUIRED_BRIER_SKILL_VS_CLIMATOLOGY,
    REQUIRED_BRIER_SKILL_VS_NAIVE,
    EvidenceSource,
)


class ModelRepositoryError(DomainError):
    """A model write that would contradict existing immutable evidence."""


#: Evidence that counts toward promotion. Everything else is research (ADR-0012).
PROMOTION_EVIDENCE = EvidenceSource.PAPER_PROSPECTIVE

RESEARCH_ONLY = "RESEARCH_ONLY"
PROMOTION_ELIGIBLE = "PROMOTION_ELIGIBLE"


@dataclass(frozen=True, slots=True)
class PredictionWriteResult:
    scored: int
    inserted: int
    existing: int


@dataclass(frozen=True, slots=True)
class VersionSummary:
    """One registered version with its latest evidence, for the operator view."""

    content_sha256: str
    semantic_version: str
    source_sha256: str
    code_commit: str
    created_at: datetime
    scored_cases: int | None
    brier_skill_vs_naive: float | None
    brier_skill_vs_climatology: float | None
    eligible_status: str | None


@dataclass(frozen=True, slots=True)
class ChampionState:
    """The current champion, or the explicit absence of one."""

    model_version_id: uuid.UUID | None
    semantic_version: str | None
    content_sha256: str | None
    actor: str | None
    reason: str | None
    recorded_at: datetime | None

    @property
    def has_champion(self) -> bool:
        return self.model_version_id is not None


class ModelRepository:
    def __init__(self, session: AsyncSession, *, environment: str) -> None:
        self._session = session
        self._environment = environment

    # --- model versions ---------------------------------------------------

    async def register_version(self, provenance: ModelProvenance) -> ModelVersionRecord:
        """Insert a model version, or return the identical one already recorded."""
        digest = provenance.content_sha256
        existing = (
            (
                await self._session.execute(
                    select(ModelVersionRecord).where(ModelVersionRecord.content_sha256 == digest)
                )
            )
            .scalars()
            .one_or_none()
        )
        if existing is not None:
            return existing
        record = ModelVersionRecord(
            content_sha256=digest,
            schema_version=provenance.schema,
            semantic_version=provenance.semantic_version,
            artifact_uri=provenance.artifact_uri,
            source_sha256=provenance.source_sha256,
            source_files_json=list(provenance.source_files),
            code_commit=provenance.code_commit,
            data_snapshot_id=provenance.data.snapshot_id,
            data_row_count=provenance.data.row_count,
            data_symbol_count=provenance.data.symbol_count,
            data_range_start=provenance.data.range_start,
            data_range_end=provenance.data.range_end,
            parameters_json=dict(sorted(provenance.parameters.items())),
            training_start=provenance.training_start,
            training_end=provenance.training_end,
            untrained=provenance.untrained,
            provenance_json=provenance.as_dict(),
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def version_by_digest(self, content_sha256: str) -> ModelVersionRecord | None:
        return (
            (
                await self._session.execute(
                    select(ModelVersionRecord).where(
                        ModelVersionRecord.content_sha256 == content_sha256
                    )
                )
            )
            .scalars()
            .one_or_none()
        )

    async def versions(self, limit: int = 20) -> tuple[VersionSummary, ...]:
        """Every registered version with its latest evidence, newest first.

        Exists so an operator can read the hash they need for ``model-promote``
        from the CLI. Requiring a hand-written SQL query to find it made the
        champion decision harder than the decision itself.
        """
        rows = (
            (
                await self._session.execute(
                    select(ModelVersionRecord)
                    .order_by(ModelVersionRecord.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        summaries: list[VersionSummary] = []
        for row in rows:
            evaluation = await self.latest_evaluation(row.id)
            summaries.append(
                VersionSummary(
                    content_sha256=row.content_sha256,
                    semantic_version=row.semantic_version,
                    source_sha256=row.source_sha256,
                    code_commit=row.code_commit,
                    created_at=row.created_at,
                    scored_cases=None if evaluation is None else evaluation.scored_cases,
                    brier_skill_vs_naive=(
                        None if evaluation is None else float(evaluation.brier_skill_vs_naive)
                    ),
                    brier_skill_vs_climatology=(
                        None if evaluation is None else float(evaluation.brier_skill_vs_climatology)
                    ),
                    eligible_status=None if evaluation is None else evaluation.eligible_status,
                )
            )
        return tuple(summaries)

    # --- predictions ------------------------------------------------------

    async def persist_predictions(
        self,
        scored: Sequence[ScoredCase],
        *,
        model_version_id: uuid.UUID,
        target: FundingTarget,
    ) -> PredictionWriteResult:
        """Write immutable predictions, refusing to silently change an existing one."""
        if not scored:
            return PredictionWriteResult(scored=0, inserted=0, existing=0)

        symbols = {item.case.symbol for item in scored}
        rows = (
            (
                await self._session.execute(
                    select(FundingPredictionRecord).where(
                        FundingPredictionRecord.environment == self._environment,
                        FundingPredictionRecord.model_version_id == model_version_id,
                        FundingPredictionRecord.symbol.in_(symbols),
                        FundingPredictionRecord.threshold_bps == target.threshold_bps,
                        FundingPredictionRecord.horizon == target.horizon,
                    )
                )
            )
            .scalars()
            .all()
        )
        known = {(row.symbol, row.decision_time): row for row in rows}

        inserted = 0
        existing = 0
        for item in scored:
            key = (item.case.symbol, item.case.decision_time)
            previous = known.get(key)
            if previous is not None:
                if bool(previous.outcome) != item.case.outcome or not _same_probability(
                    previous.model_probability, item.model_probability
                ):
                    raise ModelRepositoryError(
                        f"{item.case.symbol} at {item.case.decision_time.isoformat()}: a "
                        "prediction already exists with different values; predictions are "
                        "immutable, so this is a conflict rather than an update"
                    )
                existing += 1
                continue
            self._session.add(
                FundingPredictionRecord(
                    environment=self._environment,
                    model_version_id=model_version_id,
                    symbol=item.case.symbol,
                    decision_time=item.case.decision_time,
                    resolved_at=item.case.resolved_at,
                    threshold_bps=target.threshold_bps,
                    horizon=target.horizon,
                    model_probability=item.model_probability,
                    naive_probability=item.naive_probability,
                    climatology_probability=item.climatology_probability,
                    outcome=item.case.outcome,
                    previous_above=item.case.previous_above,
                    interval_hours=item.case.interval_hours,
                    max_step_hours=item.case.max_step_hours,
                    prior_cases=item.estimate.prior_cases,
                    matched_cases=item.estimate.matched_cases,
                )
            )
            inserted += 1
        await self._session.flush()
        return PredictionWriteResult(scored=len(scored), inserted=inserted, existing=existing)

    # --- evaluations ------------------------------------------------------

    async def record_evaluation(
        self,
        pooled: SkillReport,
        *,
        model_version_id: uuid.UUID,
        target: FundingTarget,
        evidence_source: EvidenceSource,
        data_snapshot_id: str,
        walk: WalkForward,
        by_symbol: Sequence[SkillReport] = (),
        by_interval: Sequence[SkillReport] = (),
    ) -> ModelEvaluationRecord:
        """Record calibration evidence and its slices.

        ``eligible_status`` is derived here rather than passed in, so a caller
        cannot label archive replay as promotion evidence by accident.

        Re-running over unchanged inputs returns the evidence already recorded.
        The identity — model version, target, evidence source, data snapshot —
        pins the source digest and the exact input rows, so the same key must
        produce the same numbers. If it does not, something is nondeterministic
        and that is a defect worth stopping for, not a row to overwrite.
        """
        eligible = PROMOTION_ELIGIBLE if evidence_source is PROMOTION_EVIDENCE else RESEARCH_ONLY
        existing = (
            (
                await self._session.execute(
                    select(ModelEvaluationRecord).where(
                        ModelEvaluationRecord.environment == self._environment,
                        ModelEvaluationRecord.model_version_id == model_version_id,
                        ModelEvaluationRecord.threshold_bps == target.threshold_bps,
                        ModelEvaluationRecord.horizon == target.horizon,
                        ModelEvaluationRecord.evidence_source == evidence_source.value,
                        ModelEvaluationRecord.data_snapshot_id == data_snapshot_id,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        if existing is not None:
            if existing.scored_cases != pooled.n or not _same_score(
                existing.model_brier, pooled.model.brier
            ):
                raise ModelRepositoryError(
                    "an evaluation already exists for this exact model, target, and data "
                    "snapshot but with different results; identical inputs must produce "
                    "identical evidence, so this is nondeterminism rather than an update"
                )
            return existing
        record = ModelEvaluationRecord(
            environment=self._environment,
            model_version_id=model_version_id,
            evaluated_at=datetime.now(UTC),
            evidence_source=evidence_source.value,
            eligible_status=eligible,
            data_snapshot_id=data_snapshot_id,
            threshold_bps=target.threshold_bps,
            horizon=target.horizon,
            scored_cases=pooled.n,
            skipped_cases=len(walk.skipped),
            model_brier=pooled.model.brier,
            naive_brier=pooled.naive.brier,
            climatology_brier=pooled.climatology.brier,
            model_ece=pooled.model.ece,
            brier_skill_vs_naive=pooled.brier_skill_vs_naive,
            brier_skill_vs_climatology=pooled.brier_skill_vs_climatology,
            positive_rate=pooled.positive_rate,
            details_json={
                "beats_naive": pooled.beats_naive,
                "beats_climatology": pooled.beats_climatology,
                "informative": pooled.informative,
                "skip_reasons": _skip_reasons(walk),
                "model_bins": [
                    {
                        "lower": bucket.lower,
                        "upper": bucket.upper,
                        "count": bucket.count,
                        "mean_predicted": bucket.mean_predicted,
                        "empirical_freq": bucket.empirical_freq,
                    }
                    for bucket in pooled.model.bins
                ],
            },
        )
        self._session.add(record)
        await self._session.flush()

        for dimension, reports in (("symbol", by_symbol), ("interval", by_interval)):
            for report in reports:
                self._session.add(
                    ModelEvaluationSliceRecord(
                        evaluation_id=record.id,
                        dimension=dimension,
                        label=report.label,
                        n=report.n,
                        model_brier=report.model.brier,
                        naive_brier=report.naive.brier,
                        climatology_brier=report.climatology.brier,
                        brier_skill_vs_naive=report.brier_skill_vs_naive,
                        brier_skill_vs_climatology=report.brier_skill_vs_climatology,
                        positive_rate=report.positive_rate,
                    )
                )
        await self._session.flush()
        return record

    async def latest_evaluation(self, model_version_id: uuid.UUID) -> ModelEvaluationRecord | None:
        return (
            (
                await self._session.execute(
                    select(ModelEvaluationRecord)
                    .where(
                        ModelEvaluationRecord.environment == self._environment,
                        ModelEvaluationRecord.model_version_id == model_version_id,
                    )
                    .order_by(ModelEvaluationRecord.evaluated_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .one_or_none()
        )

    # --- champion registry ------------------------------------------------

    async def promote_champion(
        self,
        *,
        model_version_id: uuid.UUID,
        evaluation_id: uuid.UUID,
        actor: str,
        reason: str,
    ) -> ModelChampionEventRecord:
        """Append a PROMOTE, refusing evidence that does not support it.

        The gates are re-checked here against the stored evaluation rather than
        trusted from the caller, so a champion can never be promoted on a number
        nobody wrote down.
        """
        evaluation = (
            (
                await self._session.execute(
                    select(ModelEvaluationRecord).where(
                        ModelEvaluationRecord.id == evaluation_id,
                        ModelEvaluationRecord.environment == self._environment,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        if evaluation is None:
            raise ModelRepositoryError("no such evaluation in this environment")
        if evaluation.model_version_id != model_version_id:
            raise ModelRepositoryError("the evaluation does not belong to that model version")
        if float(evaluation.brier_skill_vs_naive) <= REQUIRED_BRIER_SKILL_VS_NAIVE:
            raise ModelRepositoryError(
                "model does not beat naive persistence; ADR-0004 kill criterion 2"
            )
        if float(evaluation.brier_skill_vs_climatology) <= REQUIRED_BRIER_SKILL_VS_CLIMATOLOGY:
            raise ModelRepositoryError(
                "model does not beat climatology, so it has shown no information "
                "beyond being calibrated; ADR-0021"
            )
        return await self._append_champion_event(
            model_version_id=model_version_id,
            evaluation_id=evaluation_id,
            action="PROMOTE",
            actor=actor,
            reason=reason,
        )

    async def retire_champion(
        self, *, model_version_id: uuid.UUID, actor: str, reason: str
    ) -> ModelChampionEventRecord:
        return await self._append_champion_event(
            model_version_id=model_version_id,
            evaluation_id=None,
            action="RETIRE",
            actor=actor,
            reason=reason,
        )

    async def _append_champion_event(
        self,
        *,
        model_version_id: uuid.UUID,
        evaluation_id: uuid.UUID | None,
        action: str,
        actor: str,
        reason: str,
    ) -> ModelChampionEventRecord:
        if not actor.strip() or not reason.strip():
            raise ModelRepositoryError("a champion decision requires an actor and a reason")
        record = ModelChampionEventRecord(
            environment=self._environment,
            model_version_id=model_version_id,
            evaluation_id=evaluation_id,
            action=action,
            actor=actor.strip(),
            reason=reason.strip(),
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def champion(self) -> ChampionState:
        """The current champion: the latest event, if it was a PROMOTE.

        A RETIRE leaves no champion. There is deliberately no fallback to an
        earlier promoted version — one that was superseded for a reason is not a
        safe default (the ADR-0017 rule, applied to models).
        """
        latest = (
            (
                await self._session.execute(
                    select(ModelChampionEventRecord)
                    .where(ModelChampionEventRecord.environment == self._environment)
                    .order_by(ModelChampionEventRecord.sequence.desc())
                    .limit(1)
                )
            )
            .scalars()
            .one_or_none()
        )
        if latest is None or latest.action != "PROMOTE":
            return ChampionState(None, None, None, None, None, None)
        version = (
            (
                await self._session.execute(
                    select(ModelVersionRecord).where(
                        ModelVersionRecord.id == latest.model_version_id
                    )
                )
            )
            .scalars()
            .one()
        )
        return ChampionState(
            model_version_id=version.id,
            semantic_version=version.semantic_version,
            content_sha256=version.content_sha256,
            actor=latest.actor,
            reason=latest.reason,
            recorded_at=latest.recorded_at,
        )

    # --- status -----------------------------------------------------------

    async def counts(self) -> dict[str, int]:
        async def scalar(statement: Select[tuple[int]]) -> int:
            return int((await self._session.execute(statement)).scalar_one())

        return {
            # Model versions are code identity, so they are not environment-scoped.
            "model_versions": await scalar(select(func.count(ModelVersionRecord.id))),
            "predictions": await scalar(
                select(func.count(FundingPredictionRecord.id)).where(
                    FundingPredictionRecord.environment == self._environment
                )
            ),
            "evaluations": await scalar(
                select(func.count(ModelEvaluationRecord.id)).where(
                    ModelEvaluationRecord.environment == self._environment
                )
            ),
            "champion_events": await scalar(
                select(func.count(ModelChampionEventRecord.id)).where(
                    ModelChampionEventRecord.environment == self._environment
                )
            ),
        }


def _same_score(stored: object, computed: float) -> bool:
    """Brier scores are stored at 10 decimal places; compare at that precision."""
    return abs(Decimal(str(stored)) - Decimal(str(round(computed, 10)))) <= Decimal("0.0000000001")


def _same_probability(stored: object, computed: float) -> bool:
    return abs(Decimal(str(stored)) - Decimal(str(round(computed, 8)))) <= Decimal("0.00000001")


def _skip_reasons(walk: WalkForward) -> dict[str, int]:
    reasons: dict[str, int] = {}
    for _, reason in walk.skipped:
        reasons[reason.value] = reasons.get(reason.value, 0) + 1
    return reasons
