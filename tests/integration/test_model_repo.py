"""Phase-4 persistence: immutable versions, immutable predictions, fail-closed champion."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from db.model_repo import ModelRepository, ModelRepositoryError
from domain.funding_model import (
    ExpandingPersistenceModel,
    FundingTarget,
    Settlement,
    WalkForward,
    build_cases,
    score,
    score_by_interval,
    score_by_symbol,
    walk_forward,
)
from domain.model_provenance import DataSnapshot, ModelProvenance
from domain.promotion import EvidenceSource

START = datetime(2025, 1, 1, tzinfo=UTC)


def provenance(*, version: str = "test-v1", source: str | None = None) -> ModelProvenance:
    return ModelProvenance(
        semantic_version=version,
        source_sha256=source or uuid.uuid4().hex + uuid.uuid4().hex,
        source_files=("packages/domain/funding_model.py",),
        code_commit="c" * 40,
        data=DataSnapshot(
            content_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            row_count=100,
            symbol_count=1,
            range_start=START,
            range_end=START + timedelta(days=30),
        ),
        parameters={"horizon": "1"},
    )


def walked(symbol: str, rates: list[str], target: FundingTarget) -> WalkForward:
    settlements = [
        Settlement(START + timedelta(hours=8 * index), Decimal(rate), 8)
        for index, rate in enumerate(rates)
    ]
    model = ExpandingPersistenceModel(minimum_prior_cases=5, minimum_matched_cases=2)
    return walk_forward(build_cases(symbol, settlements, target), model)


def alternating(count: int) -> list[str]:
    return ["0.0001" if index % 3 else "-0.0001" for index in range(count)]


# --- model versions ----------------------------------------------------------


async def test_registering_the_same_provenance_twice_returns_one_version(
    db_session: AsyncSession,
) -> None:
    repository = ModelRepository(db_session, environment="testnet")
    model = provenance()

    first = await repository.register_version(model)
    second = await repository.register_version(model)

    assert first.id == second.id
    assert first.content_sha256 == model.content_sha256


async def test_the_same_semantic_version_with_changed_source_is_a_different_version(
    db_session: AsyncSession,
) -> None:
    """Reusing a label must not silently reuse an identity (sibling ADR-0015)."""
    repository = ModelRepository(db_session, environment="testnet")

    first = await repository.register_version(provenance(version="dup-v1"))
    second = await repository.register_version(provenance(version="dup-v1"))

    assert first.id != second.id


async def test_an_untrained_version_records_that_explicitly(db_session: AsyncSession) -> None:
    repository = ModelRepository(db_session, environment="testnet")

    record = await repository.register_version(provenance())

    assert record.untrained is True
    assert record.training_start is None
    assert record.artifact_uri.startswith("source-sha256://")


# --- predictions -------------------------------------------------------------


async def test_predictions_are_idempotent(db_session: AsyncSession) -> None:
    repository = ModelRepository(db_session, environment="testnet")
    version = await repository.register_version(provenance())
    target = FundingTarget(threshold_bps=Decimal("0"))
    result = walked(f"T{uuid.uuid4().hex[:10].upper()}USDT", alternating(60), target)

    first = await repository.persist_predictions(
        result.scored, model_version_id=version.id, target=target
    )
    second = await repository.persist_predictions(
        result.scored, model_version_id=version.id, target=target
    )

    assert first.inserted > 0
    assert first.existing == 0
    assert second.inserted == 0
    assert second.existing == first.inserted


async def test_the_same_decision_under_a_different_target_is_a_different_prediction(
    db_session: AsyncSession,
) -> None:
    """A probability under 0 bps and one under 1 bps answer different questions."""
    repository = ModelRepository(db_session, environment="testnet")
    version = await repository.register_version(provenance())
    symbol = f"T{uuid.uuid4().hex[:10].upper()}USDT"
    zero = FundingTarget(threshold_bps=Decimal("0"))
    one = FundingTarget(threshold_bps=Decimal("1"))

    first = await repository.persist_predictions(
        walked(symbol, alternating(60), zero).scored, model_version_id=version.id, target=zero
    )
    second = await repository.persist_predictions(
        walked(symbol, alternating(60), one).scored, model_version_id=version.id, target=one
    )

    assert first.inserted > 0
    assert second.inserted > 0
    assert second.existing == 0


async def test_a_changed_outcome_for_an_existing_prediction_is_a_conflict(
    db_session: AsyncSession,
) -> None:
    """Predictions are immutable: a contradiction is an error, never an update."""
    from dataclasses import replace

    repository = ModelRepository(db_session, environment="testnet")
    version = await repository.register_version(provenance())
    target = FundingTarget(threshold_bps=Decimal("0"))
    symbol = f"T{uuid.uuid4().hex[:10].upper()}USDT"
    result = walked(symbol, alternating(60), target)
    await repository.persist_predictions(result.scored, model_version_id=version.id, target=target)

    tampered = [
        replace(item, case=replace(item.case, outcome=not item.case.outcome))
        for item in result.scored
    ]

    with pytest.raises(ModelRepositoryError, match="immutable"):
        await repository.persist_predictions(tampered, model_version_id=version.id, target=target)


# --- evaluations -------------------------------------------------------------


async def test_archive_replay_is_recorded_as_research_only(db_session: AsyncSession) -> None:
    """ADR-0012: a backtest number cannot label itself promotion evidence."""
    repository = ModelRepository(db_session, environment="testnet")
    version = await repository.register_version(provenance())
    target = FundingTarget(threshold_bps=Decimal("0"))
    result = walked(f"T{uuid.uuid4().hex[:10].upper()}USDT", alternating(80), target)

    evaluation = await repository.record_evaluation(
        score(result.scored),
        model_version_id=version.id,
        target=target,
        evidence_source=EvidenceSource.BACKTEST,
        data_snapshot_id="rows-sha256://" + "a" * 64,
        walk=result,
    )

    assert evaluation.eligible_status == "RESEARCH_ONLY"


async def test_slices_are_recorded_alongside_the_pooled_number(
    db_session: AsyncSession,
) -> None:
    repository = ModelRepository(db_session, environment="testnet")
    version = await repository.register_version(provenance())
    target = FundingTarget(threshold_bps=Decimal("0"))
    result = walked(f"T{uuid.uuid4().hex[:10].upper()}USDT", alternating(80), target)

    evaluation = await repository.record_evaluation(
        score(result.scored),
        model_version_id=version.id,
        target=target,
        evidence_source=EvidenceSource.BACKTEST,
        data_snapshot_id="rows-sha256://" + "b" * 64,
        walk=result,
        by_symbol=score_by_symbol(result.scored),
        by_interval=score_by_interval(result.scored),
    )

    assert evaluation.details_json["skip_reasons"]


# --- champion registry -------------------------------------------------------


async def test_no_champion_until_one_is_promoted(db_session: AsyncSession) -> None:
    state = await ModelRepository(db_session, environment="testnet").champion()

    assert not state.has_champion


async def test_promotion_refuses_a_model_that_does_not_beat_climatology(
    db_session: AsyncSession,
) -> None:
    """ADR-0021: calibrated is not the same as informative."""
    repository = ModelRepository(db_session, environment="testnet")
    version = await repository.register_version(provenance())
    target = FundingTarget(threshold_bps=Decimal("0"))
    # A flat series: the model can add nothing over the base rate.
    flat = walked(f"T{uuid.uuid4().hex[:10].upper()}USDT", ["0.0001"] * 80, target)
    evaluation = await repository.record_evaluation(
        score(flat.scored),
        model_version_id=version.id,
        target=target,
        evidence_source=EvidenceSource.BACKTEST,
        data_snapshot_id="rows-sha256://" + "c" * 64,
        walk=flat,
    )

    with pytest.raises(ModelRepositoryError, match=r"climatology|naive"):
        await repository.promote_champion(
            model_version_id=version.id,
            evaluation_id=evaluation.id,
            actor="tester",
            reason="should be refused",
        )


async def test_promotion_refuses_an_evaluation_from_another_model(
    db_session: AsyncSession,
) -> None:
    repository = ModelRepository(db_session, environment="testnet")
    version = await repository.register_version(provenance())
    other = await repository.register_version(provenance())
    target = FundingTarget(threshold_bps=Decimal("0"))
    result = walked(f"T{uuid.uuid4().hex[:10].upper()}USDT", alternating(80), target)
    evaluation = await repository.record_evaluation(
        score(result.scored),
        model_version_id=version.id,
        target=target,
        evidence_source=EvidenceSource.BACKTEST,
        data_snapshot_id="rows-sha256://" + "d" * 64,
        walk=result,
    )

    with pytest.raises(ModelRepositoryError, match="does not belong"):
        await repository.promote_champion(
            model_version_id=other.id,
            evaluation_id=evaluation.id,
            actor="tester",
            reason="mismatched",
        )


async def test_a_promoted_model_becomes_the_champion_and_retiring_leaves_none(
    db_session: AsyncSession,
) -> None:
    """A RETIRE never falls back to an earlier champion: one superseded for a
    reason is not a safe default (the ADR-0017 rule applied to models)."""
    repository = ModelRepository(db_session, environment="testnet")
    first = await repository.register_version(provenance(version="champ-a"))
    second = await repository.register_version(provenance(version="champ-b"))
    target = FundingTarget(threshold_bps=Decimal("0"))

    for version in (first, second):
        result = walked(f"T{uuid.uuid4().hex[:10].upper()}USDT", alternating(90), target)
        evaluation = await repository.record_evaluation(
            score(result.scored),
            model_version_id=version.id,
            target=target,
            evidence_source=EvidenceSource.BACKTEST,
            data_snapshot_id=f"rows-sha256://{uuid.uuid4().hex}{uuid.uuid4().hex}",
            walk=result,
        )
        await repository.promote_champion(
            model_version_id=version.id,
            evaluation_id=evaluation.id,
            actor="tester",
            reason="research champion",
        )

    promoted = await repository.champion()
    assert promoted.model_version_id == second.id

    await repository.retire_champion(
        model_version_id=second.id, actor="tester", reason="superseded"
    )
    after = await repository.champion()

    assert not after.has_champion


async def test_a_champion_decision_requires_an_actor_and_a_reason(
    db_session: AsyncSession,
) -> None:
    repository = ModelRepository(db_session, environment="testnet")
    version = await repository.register_version(provenance())

    with pytest.raises(ModelRepositoryError, match="actor and a reason"):
        await repository.retire_champion(model_version_id=version.id, actor="  ", reason="x")


async def test_recording_the_same_evaluation_twice_returns_the_first(
    db_session: AsyncSession,
) -> None:
    """Identical model, target, and data snapshot must be idempotent, not a
    unique-constraint crash halfway through a long run."""
    repository = ModelRepository(db_session, environment="testnet")
    version = await repository.register_version(provenance())
    target = FundingTarget(threshold_bps=Decimal("0"))
    result = walked(f"T{uuid.uuid4().hex[:10].upper()}USDT", alternating(80), target)
    snapshot_id = f"rows-sha256://{uuid.uuid4().hex}{uuid.uuid4().hex}"

    first = await repository.record_evaluation(
        score(result.scored),
        model_version_id=version.id,
        target=target,
        evidence_source=EvidenceSource.BACKTEST,
        data_snapshot_id=snapshot_id,
        walk=result,
    )
    second = await repository.record_evaluation(
        score(result.scored),
        model_version_id=version.id,
        target=target,
        evidence_source=EvidenceSource.BACKTEST,
        data_snapshot_id=snapshot_id,
        walk=result,
    )

    assert first.id == second.id


async def test_the_same_snapshot_producing_different_numbers_is_a_conflict(
    db_session: AsyncSession,
) -> None:
    repository = ModelRepository(db_session, environment="testnet")
    version = await repository.register_version(provenance())
    target = FundingTarget(threshold_bps=Decimal("0"))
    symbol = f"T{uuid.uuid4().hex[:10].upper()}USDT"
    result = walked(symbol, alternating(80), target)
    snapshot_id = f"rows-sha256://{uuid.uuid4().hex}{uuid.uuid4().hex}"
    await repository.record_evaluation(
        score(result.scored),
        model_version_id=version.id,
        target=target,
        evidence_source=EvidenceSource.BACKTEST,
        data_snapshot_id=snapshot_id,
        walk=result,
    )

    different = walked(symbol, alternating(60), target)

    with pytest.raises(ModelRepositoryError, match="nondeterminism"):
        await repository.record_evaluation(
            score(different.scored),
            model_version_id=version.id,
            target=target,
            evidence_source=EvidenceSource.BACKTEST,
            data_snapshot_id=snapshot_id,
            walk=different,
        )
