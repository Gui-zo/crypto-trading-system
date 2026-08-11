"""Checksum-verified Binance public-archive ingestion.

``data.binance.vision`` is production evidence, not a testnet service. Archive
objects are mutable at their URL (Binance documents replacement updates), so the
raw-store key contains the verified payload SHA-256. A replaced object is retained
as a new immutable version instead of overwriting the bytes used by an old run.

The parsers are based on production files captured 2026-08-11:

* funding CSV: ``calc_time,funding_interval_hours,last_funding_rate``;
* kline CSV: the REST 12-column positional shape with a header;
* spot kline timestamps use microseconds from 2025 onward, while USD-M and
  funding timestamps remain milliseconds.

Missing columns never acquire defaults. The archive and REST schemas deliberately
remain distinct at ingress and meet only in exact domain objects.
"""

from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import PurePosixPath

import httpx

from domain.instrument import InstrumentRef, VenueEnvironment, VenueScope
from domain.market_data import FundingRateObservation, Kline, kline_interval
from domain.precision import PrecisionError, parse_decimal
from storage.raw_store import RawObjectRef, RawStore
from venue_binance.endpoints import VENUE_CODE, Market

ARCHIVE_BASE_URL = "https://data.binance.vision"
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_CSV_BYTES = 1024 * 1024 * 1024


class ArchiveError(RuntimeError):
    """Base class for public-archive failures."""


class ArchiveUnavailable(ArchiveError):
    """The requested archive period has not been published."""


class ArchiveIntegrityError(ArchiveError):
    """Checksum, ZIP, or CSV structure is unsafe or inconsistent."""


class ArchiveDataset(StrEnum):
    FUNDING_RATE = "fundingRate"
    KLINES = "klines"


@dataclass(frozen=True, slots=True)
class ArchiveRequest:
    """One monthly production archive object."""

    dataset: ArchiveDataset
    market: Market
    symbol: str
    month: date
    interval: str | None = None

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if not symbol:
            raise ValueError("archive symbol cannot be empty")
        object.__setattr__(self, "symbol", symbol)
        if self.month.day != 1:
            raise ValueError("archive month must be the first calendar day")
        if self.dataset is ArchiveDataset.FUNDING_RATE:
            if self.market is not Market.USDM:
                raise ValueError("funding-rate archives exist only for USD-M futures")
            if self.interval is not None:
                raise ValueError("funding-rate archive requests do not take a kline interval")
        else:
            interval = (self.interval or "").strip()
            if not interval:
                raise ValueError("kline archive requests require an interval")
            kline_interval(interval)
            object.__setattr__(self, "interval", interval)

    @property
    def market_path(self) -> str:
        return "futures/um" if self.market is Market.USDM else "spot"

    @property
    def period_label(self) -> str:
        return self.month.strftime("%Y-%m")

    @property
    def filename(self) -> str:
        if self.dataset is ArchiveDataset.FUNDING_RATE:
            return f"{self.symbol}-fundingRate-{self.period_label}.zip"
        return f"{self.symbol}-{self.interval}-{self.period_label}.zip"

    @property
    def path(self) -> str:
        base = f"data/{self.market_path}/monthly/{self.dataset.value}/{self.symbol}"
        if self.dataset is ArchiveDataset.KLINES:
            base = f"{base}/{self.interval}"
        return f"{base}/{self.filename}"

    @property
    def url(self) -> str:
        return f"{ARCHIVE_BASE_URL}/{self.path}"

    @property
    def checksum_url(self) -> str:
        return f"{self.url}.CHECKSUM"

    @property
    def period_start(self) -> datetime:
        return datetime(self.month.year, self.month.month, 1, tzinfo=UTC)

    @property
    def period_end(self) -> datetime:
        if self.month.month == 12:
            next_month = date(self.month.year + 1, 1, 1)
        else:
            next_month = date(self.month.year, self.month.month + 1, 1)
        return datetime(next_month.year, next_month.month, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class ArchiveArtifact:
    request: ArchiveRequest
    payload: RawObjectRef
    checksum: RawObjectRef
    expected_payload_sha256: str
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class ArchivePayload:
    artifact: ArchiveArtifact
    csv_bytes: bytes


def monthly_requests(
    *,
    dataset: ArchiveDataset,
    market: Market,
    symbol: str,
    start: date,
    end: date,
    interval: str | None = None,
) -> tuple[ArchiveRequest, ...]:
    """Plan every monthly object intersecting the half-open ``[start, end)`` range."""

    if end <= start:
        raise ValueError("archive end must be after start")
    cursor = date(start.year, start.month, 1)
    requests: list[ArchiveRequest] = []
    while cursor < end:
        requests.append(
            ArchiveRequest(
                dataset=dataset,
                market=market,
                symbol=symbol,
                month=cursor,
                interval=interval,
            )
        )
        cursor = (
            date(cursor.year + 1, 1, 1)
            if cursor.month == 12
            else date(cursor.year, cursor.month + 1, 1)
        )
    return tuple(requests)


class BinanceArchiveClient:
    """Download, verify, retain, and unpack production archive objects."""

    def __init__(
        self,
        *,
        environment: VenueEnvironment,
        http: httpx.AsyncClient,
        raw_store: RawStore,
    ) -> None:
        if environment is not VenueEnvironment.PRODUCTION:
            raise ValueError("data.binance.vision is production-only evidence")
        self._http = http
        self._raw_store = raw_store

    async def fetch(self, request: ArchiveRequest) -> ArchivePayload:
        payload_bytes = await self._download(request.url)
        checksum_bytes = await self._download(request.checksum_url)
        if len(payload_bytes) > MAX_ARCHIVE_BYTES:
            raise ArchiveIntegrityError(
                f"{request.filename}: compressed archive exceeds {MAX_ARCHIVE_BYTES} bytes"
            )
        expected = _parse_checksum(checksum_bytes, expected_filename=request.filename)
        actual = hashlib.sha256(payload_bytes).hexdigest()
        if actual != expected:
            raise ArchiveIntegrityError(
                f"{request.filename}: checksum mismatch expected={expected} actual={actual}"
            )

        fetched_at = datetime.now(UTC)
        key_base = (
            f"binance/production/archive/{request.market.value}/"
            f"{request.dataset.value}/{request.symbol}/{request.period_label}/{actual}"
        )
        payload_ref = self._raw_store.put(f"{key_base}.zip", payload_bytes)
        checksum_ref = self._raw_store.put(f"{key_base}.zip.CHECKSUM", checksum_bytes)
        csv_bytes = _unpack_single_csv(payload_bytes, request.filename)
        return ArchivePayload(
            artifact=ArchiveArtifact(
                request=request,
                payload=payload_ref,
                checksum=checksum_ref,
                expected_payload_sha256=expected,
                fetched_at=fetched_at,
            ),
            csv_bytes=csv_bytes,
        )

    async def _download(self, url: str) -> bytes:
        try:
            response = await self._http.get(url)
        except httpx.HTTPError as exc:
            raise ArchiveError(f"{url}: {type(exc).__name__}: {exc}") from exc
        if response.status_code == 404:
            raise ArchiveUnavailable(f"archive object is not published: {url}")
        if response.status_code >= 400:
            raise ArchiveError(f"archive GET failed: status={response.status_code} url={url}")
        return response.content


def _parse_checksum(content: bytes, *, expected_filename: str) -> str:
    try:
        line = content.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ArchiveIntegrityError("archive checksum is not ASCII") from exc
    parts = line.split()
    if len(parts) != 2 or parts[1].lstrip("*") != expected_filename:
        raise ArchiveIntegrityError(
            f"archive checksum does not name expected file {expected_filename!r}"
        )
    digest = parts[0].lower()
    if len(digest) != 64:
        raise ArchiveIntegrityError("archive checksum is not a SHA-256 digest")
    try:
        int(digest, 16)
    except ValueError as exc:
        raise ArchiveIntegrityError("archive checksum is not hexadecimal") from exc
    return digest


def _unpack_single_csv(content: bytes, archive_name: str) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = [member for member in archive.infolist() if not member.is_dir()]
            if len(members) != 1:
                raise ArchiveIntegrityError(
                    f"{archive_name}: expected exactly one file, found {len(members)}"
                )
            member = members[0]
            path = PurePosixPath(member.filename)
            if path.name != member.filename or path.suffix.lower() != ".csv":
                raise ArchiveIntegrityError(f"{archive_name}: unsafe or non-CSV ZIP member")
            if member.flag_bits & 0x1:
                raise ArchiveIntegrityError(f"{archive_name}: encrypted ZIP members are refused")
            if member.file_size > MAX_CSV_BYTES:
                raise ArchiveIntegrityError(
                    f"{archive_name}: CSV exceeds {MAX_CSV_BYTES} uncompressed bytes"
                )
            return archive.read(member)
    except zipfile.BadZipFile as exc:
        raise ArchiveIntegrityError(f"{archive_name}: invalid ZIP archive") from exc


def archive_timestamp(value: str | int) -> datetime:
    """Normalize exact epoch milliseconds or microseconds without floating point."""

    try:
        raw = int(value)
    except (TypeError, ValueError) as exc:
        raise ArchiveIntegrityError(f"invalid archive timestamp {value!r}") from exc
    if raw < 0:
        raise ArchiveIntegrityError("archive timestamp cannot be negative")
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    # Contemporary epoch ms are ~10^12 and µs are ~10^15. Values in the gap are
    # not a documented Binance unit and are refused rather than guessed.
    if raw >= 100_000_000_000_000:
        return epoch + timedelta(microseconds=raw)
    if raw >= 100_000_000_000:
        return epoch + timedelta(milliseconds=raw)
    raise ArchiveIntegrityError(f"archive timestamp has an unsupported unit: {raw}")


def parse_funding_csv(
    content: bytes,
    *,
    symbol: str,
    collected_at: datetime,
) -> tuple[FundingRateObservation, ...]:
    rows = _read_csv(content)
    if not rows:
        return ()
    header, values = _split_header(
        rows,
        expected=("calc_time", "funding_interval_hours", "last_funding_rate"),
    )
    scope = VenueScope(venue=VENUE_CODE, environment=VenueEnvironment.PRODUCTION)
    instrument = InstrumentRef(scope=scope, symbol=symbol, market="usdm")
    observations: list[FundingRateObservation] = []
    for row_number, row in enumerate(values, start=2 if header else 1):
        if len(row) < 3:
            raise ArchiveIntegrityError(f"funding CSV row {row_number} has fewer than 3 columns")
        try:
            observations.append(
                FundingRateObservation(
                    instrument=instrument,
                    funding_time=archive_timestamp(row[0]),
                    funding_rate=parse_decimal(row[2]),
                    collected_at=collected_at,
                    interval_hours=int(row[1]),
                )
            )
        except (ValueError, PrecisionError) as exc:
            raise ArchiveIntegrityError(f"invalid funding CSV row {row_number}") from exc
    return tuple(observations)


def parse_kline_csv(
    content: bytes,
    *,
    symbol: str,
    market: Market,
    collected_at: datetime,
) -> tuple[Kline, ...]:
    rows = _read_csv(content)
    if not rows:
        return ()
    expected = (
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
        "quote_volume",
        "count",
        "taker_buy_volume",
        "taker_buy_quote_volume",
        "ignore",
    )
    header, values = _split_header(rows, expected=expected)
    scope = VenueScope(venue=VENUE_CODE, environment=VenueEnvironment.PRODUCTION)
    instrument = InstrumentRef(scope=scope, symbol=symbol, market=market.value)
    klines: list[Kline] = []
    for row_number, row in enumerate(values, start=2 if header else 1):
        if len(row) < 12:
            raise ArchiveIntegrityError(f"kline CSV row {row_number} has fewer than 12 columns")
        try:
            klines.append(
                Kline(
                    instrument=instrument,
                    open_time=archive_timestamp(row[0]),
                    open=parse_decimal(row[1]),
                    high=parse_decimal(row[2]),
                    low=parse_decimal(row[3]),
                    close=parse_decimal(row[4]),
                    volume=parse_decimal(row[5]),
                    close_time=archive_timestamp(row[6]),
                    quote_volume=parse_decimal(row[7]),
                    trades=int(row[8]),
                    collected_at=collected_at,
                    is_closed=True,
                )
            )
        except (ValueError, PrecisionError) as exc:
            raise ArchiveIntegrityError(f"invalid kline CSV row {row_number}") from exc
    return tuple(klines)


def _read_csv(content: bytes) -> list[list[str]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ArchiveIntegrityError("archive CSV is not UTF-8") from exc
    try:
        return [row for row in csv.reader(io.StringIO(text, newline="")) if row]
    except csv.Error as exc:
        raise ArchiveIntegrityError(f"invalid archive CSV: {exc}") from exc


def _split_header(
    rows: list[list[str]], *, expected: tuple[str, ...]
) -> tuple[bool, list[list[str]]]:
    first = tuple(value.strip() for value in rows[0][: len(expected)])
    if first == expected:
        return True, rows[1:]
    try:
        int(rows[0][0])
    except (IndexError, ValueError) as exc:
        raise ArchiveIntegrityError(
            f"archive CSV header differs from recorded schema: {first!r}"
        ) from exc
    return False, rows
