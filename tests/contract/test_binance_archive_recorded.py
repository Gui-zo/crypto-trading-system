"""Contracts against genuine Binance public-archive rows recorded 2026-08-11."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from venue_binance.archive import parse_funding_csv, parse_kline_csv
from venue_binance.endpoints import Market

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "binance" / "recorded"
COLLECTED_AT = datetime(2026, 8, 11, 18, tzinfo=UTC)


def recorded(name: str) -> bytes:
    return (RECORDED / name).read_bytes()


def test_monthly_funding_archive_preserves_historical_interval() -> None:
    rows = parse_funding_csv(
        recorded("archive_fundingRate_BTCUSDT_2026-07.trimmed.csv"),
        symbol="BTCUSDT",
        collected_at=COLLECTED_AT,
    )

    assert len(rows) == 4
    assert rows[0].funding_time == datetime(2026, 7, 1, tzinfo=UTC)
    assert rows[0].funding_rate == Decimal("0.00005532")
    assert {row.interval_hours for row in rows} == {8}
    assert all(row.mark_price is None and row.rate_type is None for row in rows)


def test_futures_archive_uses_millisecond_timestamps_and_a_header() -> None:
    rows = parse_kline_csv(
        recorded("archive_futures_klines_BTCUSDT_1h_2026-07.trimmed.csv"),
        symbol="BTCUSDT",
        market=Market.USDM,
        collected_at=COLLECTED_AT,
    )

    assert len(rows) == 3
    assert rows[0].open_time == datetime(2026, 7, 1, tzinfo=UTC)
    assert rows[0].close_time == datetime(2026, 7, 1, 0, 59, 59, 999000, tzinfo=UTC)
    assert rows[0].quote_volume == Decimal("323655246.75510")
    assert rows[0].trades == 145946


def test_spot_archive_uses_microsecond_timestamps_and_can_be_headerless() -> None:
    rows = parse_kline_csv(
        recorded("archive_spot_klines_BTCUSDT_1h_2026-07.trimmed.csv"),
        symbol="BTCUSDT",
        market=Market.SPOT,
        collected_at=COLLECTED_AT,
    )

    assert len(rows) == 3
    assert rows[0].open_time == datetime(2026, 7, 1, tzinfo=UTC)
    assert rows[0].close_time == datetime(2026, 7, 1, 0, 59, 59, 999999, tzinfo=UTC)
    assert rows[0].close == Decimal("58576.00000000")
