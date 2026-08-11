"""Phase-2 instrument specification and canonical-catalog tests."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from domain.instrument import (
    InstrumentCatalog,
    MaintenanceMarginTier,
    MarginSchedule,
    VenueEnvironment,
    VenueScope,
)
from venue_binance import mapping
from venue_binance.schemas import ExchangeInfoWire, FundingInfoWire, LeverageBracketWire

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "binance" / "recorded"
SCOPE = VenueScope(venue="BINANCE", environment=VenueEnvironment.PRODUCTION)


def load(name: str) -> object:
    return json.loads((RECORDED / name).read_text())


def exchange_info() -> ExchangeInfoWire:
    return ExchangeInfoWire.model_validate(load("fapi_exchangeInfo.trimmed.json"))


def funding_info() -> list[FundingInfoWire]:
    payload = load("fapi_fundingInfo.trimmed.json")
    assert isinstance(payload, list)
    return [FundingInfoWire.model_validate(item) for item in payload]


def bracket(symbol: str, *, ratio: str = "0.004") -> LeverageBracketWire:
    return LeverageBracketWire.model_validate(
        {
            "symbol": symbol,
            "brackets": [
                {
                    "bracket": 1,
                    "initialLeverage": 125,
                    "notionalCap": Decimal("50000"),
                    "notionalFloor": Decimal("0"),
                    "maintMarginRatio": Decimal(ratio),
                    "cum": Decimal("0"),
                },
                {
                    "bracket": 2,
                    "initialLeverage": 100,
                    "notionalCap": Decimal("250000"),
                    "notionalFloor": Decimal("50000"),
                    "maintMarginRatio": Decimal("0.005"),
                    "cum": Decimal("50"),
                },
            ],
        }
    )


def catalog(
    *,
    funding: list[FundingInfoWire] | None = None,
    brackets: list[LeverageBracketWire] | None = None,
) -> InstrumentCatalog:
    return mapping.to_instrument_catalog(
        exchange_info(),
        funding if funding is not None else funding_info(),
        brackets if brackets is not None else [bracket("BTCUSDT"), bracket("ETHUSDT")],
        scope=SCOPE,
    )


def test_a_complete_catalog_maps_filters_funding_and_margin_tiers() -> None:
    result = catalog()
    assert result.candidate_symbols == 2
    assert len(result.specifications) == 2
    assert not result.exclusions
    assert len(result.content_sha256) == 64

    btc = next(spec for spec in result.specifications if spec.instrument.symbol == "BTCUSDT")
    assert btc.price_filter.tick_size == Decimal("0.10")
    assert btc.quantity_filter.step_size == Decimal("0.001")
    assert btc.minimum_notional == Decimal("50")
    assert btc.funding_schedule.interval_hours == 8
    assert btc.margin_schedule.tiers[0].maintenance_margin_ratio == Decimal("0.004")


def test_catalog_hash_is_independent_of_source_order() -> None:
    first = catalog()
    second = catalog(
        funding=list(reversed(funding_info())),
        brackets=[bracket("ETHUSDT"), bracket("BTCUSDT")],
    )
    assert first.content_sha256 == second.content_sha256
    assert first.canonical_bytes == second.canonical_bytes


def test_a_margin_tier_change_produces_a_new_catalog_hash() -> None:
    before = catalog()
    after = catalog(brackets=[bracket("BTCUSDT", ratio="0.0041"), bracket("ETHUSDT")])
    assert before.content_sha256 != after.content_sha256


def test_missing_funding_metadata_excludes_instead_of_assuming_eight_hours() -> None:
    without_eth = [item for item in funding_info() if item.symbol != "ETHUSDT"]
    result = catalog(funding=without_eth)
    assert [spec.instrument.symbol for spec in result.specifications] == ["BTCUSDT"]
    assert result.exclusions[0].symbol == "ETHUSDT"
    assert "MISSING_FUNDING_SCHEDULE" in result.exclusions[0].reasons


def test_missing_margin_metadata_excludes_the_symbol() -> None:
    result = catalog(brackets=[bracket("BTCUSDT")])
    assert [spec.instrument.symbol for spec in result.specifications] == ["BTCUSDT"]
    assert result.exclusions[0].reasons == ("MISSING_MARGIN_SCHEDULE",)


def test_zero_complete_specs_fails_the_whole_catalog_closed() -> None:
    with pytest.raises(ValueError, match="no complete specifications"):
        catalog(funding=[])


def test_margin_tiers_must_be_contiguous_and_monotone() -> None:
    first = MaintenanceMarginTier(
        bracket=1,
        initial_leverage=125,
        notional_floor=Decimal("0"),
        notional_cap=Decimal("50000"),
        maintenance_margin_ratio=Decimal("0.004"),
        cumulative=Decimal("0"),
    )
    gap = MaintenanceMarginTier(
        bracket=2,
        initial_leverage=100,
        notional_floor=Decimal("60000"),
        notional_cap=Decimal("250000"),
        maintenance_margin_ratio=Decimal("0.005"),
        cumulative=Decimal("50"),
    )
    with pytest.raises(ValueError, match="contiguous"):
        MarginSchedule(symbol="BTCUSDT", tiers=(first, gap))
