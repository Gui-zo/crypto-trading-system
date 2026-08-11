"""Phase-3 persistence, provenance, idempotency, and quality gates."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from db.market_data_repo import (
    DataQualityStatus,
    MarketDataArtifact,
    MarketDataConflict,
    MarketDataRepository,
)
from domain.instrument import InstrumentRef, VenueEnvironment, VenueScope
from domain.market_data import (
    BookTicker,
    FundingRateObservation,
    Kline,
    MarketDataSource,
    MarkPriceSnapshot,
)

START = datetime(2040, 1, 1, tzinfo=UTC)


def instrument(symbol: str, market: str = "usdm") -> InstrumentRef:
    return InstrumentRef(
        scope=VenueScope(venue="BINANCE", environment=VenueEnvironment.TESTNET),
        symbol=symbol,
        market=market,
    )


def artifact(
    symbol: str,
    *,
    source: MarketDataSource,
    dataset: str = "funding_rate",
    market: str = "usdm",
    interval: str | None = None,
) -> MarketDataArtifact:
    common = {
        "source_type": source,
        "dataset": dataset,
        "market": market,
        "symbol": symbol,
        "interval": interval,
        "source_url": "https://example.invalid/immutable-source",
        "raw_key": f"tests/market-data/{uuid.uuid4()}",
        "raw_sha256": "a" * 64,
        "raw_size": 123,
        "fetched_at": START + timedelta(days=1),
    }
    if source is MarketDataSource.ARCHIVE:
        common.update(
            {
                "period_start": START,
                "period_end": START + timedelta(days=31),
                "checksum_key": f"tests/market-data/{uuid.uuid4()}.CHECKSUM",
                "checksum_sha256": "b" * 64,
                "expected_payload_sha256": "a" * 64,
            }
        )
    return MarketDataArtifact(**common)  # type: ignore[arg-type]


def funding(
    symbol: str,
    offset_hours: int,
    *,
    rate: str = "0.0001",
    source: MarketDataSource = MarketDataSource.ARCHIVE,
    collected_at: datetime | None = None,
) -> FundingRateObservation:
    return FundingRateObservation(
        instrument=instrument(symbol),
        funding_time=START + timedelta(hours=offset_hours),
        funding_rate=Decimal(rate),
        collected_at=collected_at or START + timedelta(days=1),
        mark_price=(Decimal("50000.12345678") if source is MarketDataSource.REST else None),
        rate_type=("Regular" if source is MarketDataSource.REST else None),
        interval_hours=(8 if source is MarketDataSource.ARCHIVE else None),
    )


def kline(symbol: str, offset_hours: int, market: str = "usdm") -> Kline:
    open_at = START + timedelta(hours=offset_hours)
    return Kline(
        instrument=instrument(symbol, market),
        open_time=open_at,
        close_time=open_at + timedelta(hours=1) - timedelta(milliseconds=1),
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        volume=Decimal("12.345"),
        quote_volume=Decimal("1295.25"),
        trades=42,
        collected_at=START + timedelta(days=1),
    )


async def test_funding_ingestion_is_exact_and_idempotent(db_session: AsyncSession) -> None:
    symbol = f"TST{uuid.uuid4().hex[:12].upper()}"
    repository = MarketDataRepository(db_session, environment="testnet")
    source = artifact(symbol, source=MarketDataSource.ARCHIVE)
    observations = tuple(funding(symbol, offset) for offset in (0, 8, 16))

    first = await repository.ingest_funding(observations, artifact=source)
    second = await repository.ingest_funding(observations, artifact=source)

    assert first.status is DataQualityStatus.PASS
    assert (first.inserted, first.existing) == (3, 0)
    assert second.status is DataQualityStatus.PASS
    assert (second.inserted, second.existing) == (0, 3)


async def test_rest_and_archive_merge_only_when_exact_rate_agrees(
    db_session: AsyncSession,
) -> None:
    symbol = f"TST{uuid.uuid4().hex[:12].upper()}"
    repository = MarketDataRepository(db_session, environment="testnet")
    archive_row = funding(symbol, 0)
    rest_row = funding(symbol, 0, source=MarketDataSource.REST)

    await repository.ingest_funding(
        (archive_row,),
        artifact=artifact(symbol, source=MarketDataSource.ARCHIVE),
    )
    rest_result = await repository.ingest_funding(
        (rest_row,),
        artifact=artifact(symbol, source=MarketDataSource.REST),
        expected_interval_hours=8,
    )
    canonical = await repository.funding_series(symbol)

    assert rest_result.status is DataQualityStatus.PASS
    assert len(canonical) == 1
    assert canonical[0].funding_rate == Decimal("0.0001")
    assert canonical[0].interval_hours == 8
    assert canonical[0].mark_price == Decimal("50000.12345678")
    assert canonical[0].rate_type == "Regular"


async def test_cross_source_disagreement_is_recorded_and_not_inserted(
    db_session: AsyncSession,
) -> None:
    symbol = f"TST{uuid.uuid4().hex[:12].upper()}"
    repository = MarketDataRepository(db_session, environment="testnet")
    await repository.ingest_funding(
        (funding(symbol, 0),),
        artifact=artifact(symbol, source=MarketDataSource.ARCHIVE),
    )

    result = await repository.ingest_funding(
        (funding(symbol, 0, rate="0.0002", source=MarketDataSource.REST),),
        artifact=artifact(symbol, source=MarketDataSource.REST),
        expected_interval_hours=8,
    )

    assert result.status is DataQualityStatus.BLOCKED
    assert result.conflicts == 1
    assert result.inserted == 0
    assert [row.funding_rate for row in await repository.funding_series(symbol)] == [
        Decimal("0.0001")
    ]


async def test_funding_gap_and_duplicate_fail_closed(db_session: AsyncSession) -> None:
    symbol = f"TST{uuid.uuid4().hex[:12].upper()}"
    repository = MarketDataRepository(db_session, environment="testnet")
    first = funding(symbol, 0)
    result = await repository.ingest_funding(
        (first, first, funding(symbol, 16)),
        artifact=artifact(symbol, source=MarketDataSource.ARCHIVE),
    )

    assert result.status is DataQualityStatus.BLOCKED
    assert result.duplicates == 1
    assert result.gaps == 1


async def test_funding_gap_across_two_archive_files_is_detected(
    db_session: AsyncSession,
) -> None:
    symbol = f"TST{uuid.uuid4().hex[:12].upper()}"
    repository = MarketDataRepository(db_session, environment="testnet")
    first = await repository.ingest_funding(
        (funding(symbol, 0), funding(symbol, 8)),
        artifact=artifact(symbol, source=MarketDataSource.ARCHIVE),
    )
    second = await repository.ingest_funding(
        (funding(symbol, 24),),
        artifact=artifact(symbol, source=MarketDataSource.ARCHIVE),
    )

    assert first.status is DataQualityStatus.PASS
    assert second.status is DataQualityStatus.BLOCKED
    assert second.gaps == 1


async def test_empty_source_is_a_durable_block_not_a_silent_pass(
    db_session: AsyncSession,
) -> None:
    symbol = f"TST{uuid.uuid4().hex[:12].upper()}"
    result = await MarketDataRepository(db_session, environment="testnet").ingest_funding(
        (), artifact=artifact(symbol, source=MarketDataSource.ARCHIVE)
    )

    assert result.status is DataQualityStatus.BLOCKED
    assert result.rows == result.inserted == 0


async def test_raw_key_cannot_be_reused_with_changed_provenance(
    db_session: AsyncSession,
) -> None:
    symbol = f"TST{uuid.uuid4().hex[:12].upper()}"
    repository = MarketDataRepository(db_session, environment="testnet")
    source = artifact(symbol, source=MarketDataSource.ARCHIVE)
    observation = funding(symbol, 0)
    await repository.ingest_funding((observation,), artifact=source)

    with pytest.raises(MarketDataConflict, match="reused"):
        await repository.ingest_funding(
            (observation,),
            artifact=replace(source, source_url="https://example.invalid/changed"),
        )


async def test_kline_ingestion_detects_gaps_and_is_idempotent(
    db_session: AsyncSession,
) -> None:
    symbol = f"TST{uuid.uuid4().hex[:12].upper()}"
    repository = MarketDataRepository(db_session, environment="testnet")
    source = artifact(
        symbol,
        source=MarketDataSource.ARCHIVE,
        dataset="kline",
        interval="1h",
    )
    contiguous = tuple(kline(symbol, offset) for offset in (0, 1, 2))

    first = await repository.ingest_klines(contiguous, interval="1h", artifact=source)
    second = await repository.ingest_klines(contiguous, interval="1h", artifact=source)

    assert first.status is second.status is DataQualityStatus.PASS
    assert (first.inserted, second.existing) == (3, 3)

    gapped_symbol = f"TST{uuid.uuid4().hex[:12].upper()}"
    gapped = await repository.ingest_klines(
        (kline(gapped_symbol, 0), kline(gapped_symbol, 2)),
        interval="1h",
        artifact=artifact(
            gapped_symbol,
            source=MarketDataSource.ARCHIVE,
            dataset="kline",
            interval="1h",
        ),
    )
    assert gapped.status is DataQualityStatus.BLOCKED
    assert gapped.gaps == 1


async def test_kline_gap_across_two_archive_files_is_detected(
    db_session: AsyncSession,
) -> None:
    symbol = f"TST{uuid.uuid4().hex[:12].upper()}"
    repository = MarketDataRepository(db_session, environment="testnet")
    first = await repository.ingest_klines(
        (kline(symbol, 0), kline(symbol, 1)),
        interval="1h",
        artifact=artifact(
            symbol,
            source=MarketDataSource.ARCHIVE,
            dataset="kline",
            interval="1h",
        ),
    )
    second = await repository.ingest_klines(
        (kline(symbol, 3),),
        interval="1h",
        artifact=artifact(
            symbol,
            source=MarketDataSource.ARCHIVE,
            dataset="kline",
            interval="1h",
        ),
    )

    assert first.status is DataQualityStatus.PASS
    assert second.status is DataQualityStatus.BLOCKED
    assert second.gaps == 1


async def test_live_snapshots_are_immutable_and_environment_scoped(
    db_session: AsyncSession,
) -> None:
    symbol = f"TST{uuid.uuid4().hex[:12].upper()}"
    repository = MarketDataRepository(db_session, environment="testnet")
    collected_at = START + timedelta(days=2)
    mark = MarkPriceSnapshot(
        instrument=instrument(symbol),
        mark_price=Decimal("50000"),
        index_price=Decimal("50001"),
        last_funding_rate=Decimal("0.0001"),
        interest_rate=Decimal("0.0001"),
        next_funding_time=START + timedelta(hours=8),
        venue_time=START,
        collected_at=collected_at,
    )
    mark_source = replace(
        artifact(
            symbol,
            source=MarketDataSource.REST,
            dataset="mark_price",
        ),
        fetched_at=collected_at,
    )
    book = BookTicker(
        instrument=instrument(symbol, "spot"),
        bid_price=Decimal("49999"),
        bid_qty=Decimal("1"),
        ask_price=Decimal("50000"),
        ask_qty=Decimal("2"),
        collected_at=collected_at,
    )

    assert await repository.record_mark_price(mark, artifact=mark_source)
    assert not await repository.record_mark_price(mark, artifact=mark_source)
    assert await repository.record_book_ticker(
        book,
        artifact=replace(
            artifact(
                symbol,
                source=MarketDataSource.REST,
                dataset="book_ticker",
                market="spot",
            ),
            fetched_at=collected_at,
        ),
    )
    assert await repository.latest_prices_at() == collected_at


async def test_archive_download_time_does_not_satisfy_live_funding_freshness(
    db_session: AsyncSession,
) -> None:
    symbol = f"TST{uuid.uuid4().hex[:12].upper()}"
    repository = MarketDataRepository(db_session, environment="testnet")
    previous = await repository.latest_funding_at()
    rest_fetched = (previous or datetime(2098, 1, 1, tzinfo=UTC)) + timedelta(days=1)
    archive_collected = rest_fetched + timedelta(days=1)
    await repository.ingest_funding(
        (funding(symbol, 0, collected_at=archive_collected),),
        artifact=artifact(symbol, source=MarketDataSource.ARCHIVE),
    )
    assert await repository.latest_funding_at() == previous

    await repository.ingest_funding(
        (
            funding(
                symbol,
                8,
                source=MarketDataSource.REST,
                collected_at=START + timedelta(days=1),
            ),
        ),
        artifact=replace(artifact(symbol, source=MarketDataSource.REST), fetched_at=rest_fetched),
        expected_interval_hours=8,
    )
    assert await repository.latest_funding_at() == rest_fetched

    next_poll = rest_fetched + timedelta(minutes=30)
    repeated = await repository.ingest_funding(
        (
            funding(
                symbol,
                8,
                source=MarketDataSource.REST,
                collected_at=START + timedelta(days=1),
            ),
        ),
        artifact=replace(artifact(symbol, source=MarketDataSource.REST), fetched_at=next_poll),
        expected_interval_hours=8,
    )
    assert repeated.existing == 1
    assert await repository.latest_funding_at() == next_poll


async def test_repository_refuses_an_observation_from_another_environment(
    db_session: AsyncSession,
) -> None:
    symbol = f"TST{uuid.uuid4().hex[:12].upper()}"
    production_observation = replace(
        funding(symbol, 0),
        instrument=InstrumentRef(
            scope=VenueScope(venue="BINANCE", environment=VenueEnvironment.PRODUCTION),
            symbol=symbol,
            market="usdm",
        ),
    )
    with pytest.raises(ValueError, match="environment"):
        await MarketDataRepository(db_session, environment="testnet").ingest_funding(
            (production_observation,),
            artifact=artifact(symbol, source=MarketDataSource.ARCHIVE),
        )
