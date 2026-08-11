"""Database integration tests for fail-closed instrument catalog versioning."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from db.instrument_repo import CatalogSourceArtifact, InstrumentCatalogRepository
from domain.instrument import (
    ContractType,
    FundingSchedule,
    InstrumentCatalog,
    InstrumentRef,
    InstrumentReviewAction,
    InstrumentSpecification,
    InstrumentSpecReviewStatus,
    InstrumentStatus,
    MaintenanceMarginTier,
    MarginSchedule,
    PriceFilter,
    QuantityFilter,
    VenueEnvironment,
    VenueScope,
)

NOW = datetime(2099, 1, 1, tzinfo=UTC)
SCOPE = VenueScope(venue="BINANCE", environment=VenueEnvironment.TESTNET)


def catalog(*, tick_size: str = "0.10") -> InstrumentCatalog:
    symbol = "BTCUSDT"
    specification = InstrumentSpecification(
        instrument=InstrumentRef(scope=SCOPE, symbol=symbol, market="usdm"),
        status=InstrumentStatus.TRADING,
        contract_type=ContractType.PERPETUAL,
        base_asset="BTC",
        quote_asset="USDT",
        margin_asset="USDT",
        price_filter=PriceFilter(
            min_price=Decimal("1"),
            max_price=Decimal("1000000"),
            tick_size=Decimal(tick_size),
        ),
        quantity_filter=QuantityFilter(
            min_quantity=Decimal("0.001"),
            max_quantity=Decimal("1000"),
            step_size=Decimal("0.001"),
        ),
        minimum_notional=Decimal("5"),
        funding_schedule=FundingSchedule(
            interval_hours=8,
            rate_cap=Decimal("0.003"),
            rate_floor=Decimal("-0.003"),
        ),
        margin_schedule=MarginSchedule(
            symbol=symbol,
            tiers=(
                MaintenanceMarginTier(
                    bracket=1,
                    initial_leverage=125,
                    notional_floor=Decimal("0"),
                    notional_cap=Decimal("50000"),
                    maintenance_margin_ratio=Decimal("0.004"),
                    cumulative=Decimal("0"),
                ),
            ),
        ),
        liquidation_fee=Decimal("0.01"),
    )
    return InstrumentCatalog(
        scope=SCOPE,
        total_symbols=1,
        candidate_symbols=1,
        specifications=(specification,),
    )


def sources(*, suffix: str = "one") -> tuple[CatalogSourceArtifact, ...]:
    return tuple(
        CatalogSourceArtifact(
            endpoint=endpoint,
            key=f"binance/testnet/usdm/{endpoint}/{suffix}.json",
            sha256=character * 64,
            size=100,
            fetched_at=NOW,
        )
        for endpoint, character in (
            ("exchangeInfo", "a"),
            ("fundingInfo", "b"),
            ("leverageBracket", "c"),
        )
    )


async def test_first_catalog_is_pending_review(db_session: AsyncSession) -> None:
    repo = InstrumentCatalogRepository(db_session, environment="testnet")
    result = await repo.record_catalog(catalog(), sources=sources(), observed_at=NOW)
    assert result.created_new_version
    assert not result.changed_from_previous
    assert result.status.review_status is InstrumentSpecReviewStatus.PENDING_REVIEW


async def test_identical_sync_reuses_version_and_appends_observation(
    db_session: AsyncSession,
) -> None:
    repo = InstrumentCatalogRepository(db_session, environment="testnet")
    before = await repo.counts()
    first = await repo.record_catalog(catalog(), sources=sources(), observed_at=NOW)
    second = await repo.record_catalog(
        catalog(),
        sources=sources(suffix="two"),
        observed_at=NOW + timedelta(seconds=1),
    )
    after = await repo.counts()
    assert first.status.version_id == second.status.version_id
    assert not second.created_new_version
    assert not second.changed_from_previous
    assert after[0] - before[0] == 1
    assert after[1] - before[1] == 2


async def test_human_review_approves_the_exact_current_hash(db_session: AsyncSession) -> None:
    repo = InstrumentCatalogRepository(db_session, environment="testnet")
    synchronized = await repo.record_catalog(catalog(), sources=sources(), observed_at=NOW)
    approved = await repo.review_current(
        content_sha256=synchronized.status.content_sha256,
        action=InstrumentReviewAction.APPROVE,
        actor="integration-test",
        reason="fixture reviewed",
    )
    assert approved.review_status is InstrumentSpecReviewStatus.APPROVED
    assert (await repo.current_status()).review_status is InstrumentSpecReviewStatus.APPROVED  # type: ignore[union-attr]


async def test_changed_catalog_blocks_instead_of_falling_back_to_approved(
    db_session: AsyncSession,
) -> None:
    repo = InstrumentCatalogRepository(db_session, environment="testnet")
    first = await repo.record_catalog(catalog(), sources=sources(), observed_at=NOW)
    await repo.review_current(
        content_sha256=first.status.content_sha256,
        action=InstrumentReviewAction.APPROVE,
        actor="integration-test",
        reason="first version reviewed",
    )

    changed = await repo.record_catalog(
        catalog(tick_size="0.20"),
        sources=sources(suffix="changed"),
        observed_at=NOW + timedelta(seconds=1),
    )
    assert changed.changed_from_previous
    assert changed.created_new_version
    assert changed.status.review_status is InstrumentSpecReviewStatus.PENDING_REVIEW
    current = await repo.current_status()
    assert current is not None
    assert current.content_sha256 == changed.status.content_sha256
    assert current.review_status is InstrumentSpecReviewStatus.PENDING_REVIEW


async def test_review_refuses_a_stale_hash(db_session: AsyncSession) -> None:
    repo = InstrumentCatalogRepository(db_session, environment="testnet")
    first = await repo.record_catalog(catalog(), sources=sources(), observed_at=NOW)
    await repo.record_catalog(
        catalog(tick_size="0.20"),
        sources=sources(suffix="changed"),
        observed_at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(RuntimeError, match="stale"):
        await repo.review_current(
            content_sha256=first.status.content_sha256,
            action=InstrumentReviewAction.APPROVE,
            actor="integration-test",
            reason="wrong version",
        )
