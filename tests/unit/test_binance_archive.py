"""Checksum, retention, routing, and parser boundaries for Binance archives."""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import UTC, date, datetime

import httpx
import pytest

from domain.instrument import VenueEnvironment
from storage.raw_store import InMemoryRawStore
from venue_binance.archive import (
    ArchiveDataset,
    ArchiveIntegrityError,
    ArchiveRequest,
    ArchiveUnavailable,
    BinanceArchiveClient,
    archive_timestamp,
    monthly_requests,
    parse_funding_csv,
)
from venue_binance.endpoints import Market


def zipped(member: str, content: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, content)
    return buffer.getvalue()


def request() -> ArchiveRequest:
    return ArchiveRequest(
        dataset=ArchiveDataset.FUNDING_RATE,
        market=Market.USDM,
        symbol="btcusdt",
        month=date(2026, 7, 1),
    )


def transport(payload: bytes, checksum: bytes | None = None) -> httpx.MockTransport:
    expected_checksum = checksum or (
        f"{hashlib.sha256(payload).hexdigest()}  {request().filename}\n".encode()
    )

    def respond(http_request: httpx.Request) -> httpx.Response:
        body = expected_checksum if http_request.url.path.endswith(".CHECKSUM") else payload
        return httpx.Response(200, content=body)

    return httpx.MockTransport(respond)


def test_monthly_request_builds_the_recorded_production_urls() -> None:
    funding = request()
    spot = ArchiveRequest(
        dataset=ArchiveDataset.KLINES,
        market=Market.SPOT,
        symbol="BTCUSDT",
        month=date(2026, 7, 1),
        interval="1h",
    )

    assert funding.url.endswith(
        "/data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2026-07.zip"
    )
    assert spot.url.endswith("/data/spot/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2026-07.zip")
    assert funding.period_end == datetime(2026, 8, 1, tzinfo=UTC)


def test_monthly_planner_covers_every_intersecting_month() -> None:
    planned = monthly_requests(
        dataset=ArchiveDataset.KLINES,
        market=Market.USDM,
        symbol="BTCUSDT",
        start=date(2025, 12, 15),
        end=date(2026, 2, 2),
        interval="1h",
    )

    assert [item.period_label for item in planned] == ["2025-12", "2026-01", "2026-02"]


def test_archive_requests_refuse_impossible_combinations() -> None:
    with pytest.raises(ValueError, match="only for USD-M"):
        ArchiveRequest(
            dataset=ArchiveDataset.FUNDING_RATE,
            market=Market.SPOT,
            symbol="BTCUSDT",
            month=date(2026, 7, 1),
        )
    with pytest.raises(ValueError, match="require an interval"):
        ArchiveRequest(
            dataset=ArchiveDataset.KLINES,
            market=Market.USDM,
            symbol="BTCUSDT",
            month=date(2026, 7, 1),
        )
    with pytest.raises(ValueError, match="unsupported fixed-width"):
        ArchiveRequest(
            dataset=ArchiveDataset.KLINES,
            market=Market.USDM,
            symbol="BTCUSDT",
            month=date(2026, 7, 1),
            interval="1M",
        )


async def test_fetch_verifies_and_retains_the_exact_archive_and_checksum() -> None:
    csv_bytes = b"calc_time,funding_interval_hours,last_funding_rate\n1782864000000,8,0.1\n"
    payload = zipped("BTCUSDT-fundingRate-2026-07.csv", csv_bytes)
    store = InMemoryRawStore()
    async with httpx.AsyncClient(transport=transport(payload)) as http:
        client = BinanceArchiveClient(
            environment=VenueEnvironment.PRODUCTION,
            http=http,
            raw_store=store,
        )
        result = await client.fetch(request())

    digest = hashlib.sha256(payload).hexdigest()
    assert result.csv_bytes == csv_bytes
    assert result.artifact.expected_payload_sha256 == digest
    assert result.artifact.payload.key.endswith(f"/{digest}.zip")
    assert store.get(result.artifact.payload.key) == payload
    assert store.get(result.artifact.checksum.key).endswith(b"BTCUSDT-fundingRate-2026-07.zip\n")


async def test_checksum_mismatch_fails_before_any_bytes_are_retained() -> None:
    payload = zipped("BTCUSDT-fundingRate-2026-07.csv", b"data")
    wrong = f"{'0' * 64}  {request().filename}\n".encode()
    store = InMemoryRawStore()
    async with httpx.AsyncClient(transport=transport(payload, wrong)) as http:
        client = BinanceArchiveClient(
            environment=VenueEnvironment.PRODUCTION,
            http=http,
            raw_store=store,
        )
        with pytest.raises(ArchiveIntegrityError, match="checksum mismatch"):
            await client.fetch(request())

    assert store._objects == {}


async def test_unsafe_zip_member_is_refused() -> None:
    payload = zipped("../BTCUSDT.csv", b"data")
    async with httpx.AsyncClient(transport=transport(payload)) as http:
        client = BinanceArchiveClient(
            environment=VenueEnvironment.PRODUCTION,
            http=http,
            raw_store=InMemoryRawStore(),
        )
        with pytest.raises(ArchiveIntegrityError, match="unsafe or non-CSV"):
            await client.fetch(request())


async def test_a_missing_month_is_typed_as_unavailable() -> None:
    def respond(http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = BinanceArchiveClient(
            environment=VenueEnvironment.PRODUCTION,
            http=http,
            raw_store=InMemoryRawStore(),
        )
        with pytest.raises(ArchiveUnavailable, match="not published"):
            await client.fetch(request())


async def test_public_archive_is_never_treated_as_testnet_evidence() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as http:
        with pytest.raises(ValueError, match="production-only"):
            BinanceArchiveClient(
                environment=VenueEnvironment.TESTNET,
                http=http,
                raw_store=InMemoryRawStore(),
            )


def test_timestamp_unit_normalization_is_exact() -> None:
    assert archive_timestamp("1782867599999").microsecond == 999000
    assert archive_timestamp("1782867599999999").microsecond == 999999
    with pytest.raises(ArchiveIntegrityError, match="unsupported unit"):
        archive_timestamp("17828675999")


def test_changed_archive_header_is_refused_instead_of_guessed() -> None:
    changed = b"time,interval,rate\n1782864000000,8,0.1\n"
    with pytest.raises(ArchiveIntegrityError, match="header differs"):
        parse_funding_csv(changed, symbol="BTCUSDT", collected_at=datetime.now(UTC))


def test_invalid_historical_funding_interval_is_a_typed_archive_error() -> None:
    invalid = b"calc_time,funding_interval_hours,last_funding_rate\n1782864000000,5,0.1\n"
    with pytest.raises(ArchiveIntegrityError, match="invalid funding CSV row"):
        parse_funding_csv(invalid, symbol="BTCUSDT", collected_at=datetime.now(UTC))
