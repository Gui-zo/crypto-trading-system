"""Contract tests against **real** Binance responses recorded 2026-08-09/11.

Not synthetic fixtures. Every payload here is bytes the venue actually sent (two
are trimmed to a subset of a large list; see `tests/fixtures/binance/README.md`).
That is what makes these tests meaningful: they fail when our reading of the API
is wrong, which is exactly the risk ADR-0003 exists to contain.

Several tests below assert on things that contradicted our documented
assumptions. Those are marked, because each one would have been a silent bug.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from domain.instrument import (
    ContractType,
    InstrumentRef,
    InstrumentStatus,
    VenueEnvironment,
    VenueScope,
)
from domain.precision import parse_decimal
from venue_binance import mapping
from venue_binance.schemas import (
    BookTickerStreamWire,
    BookTickerWire,
    ExchangeInfoWire,
    FundingInfoWire,
    FundingRateWire,
    LeverageBracketWire,
    PremiumIndexWire,
    ServerTimeWire,
)

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "binance" / "recorded"
COLLECTED_AT = datetime(2026, 8, 9, 18, 21, tzinfo=UTC)


def load(name: str) -> object:
    return json.loads((RECORDED / name).read_text())


def instrument(symbol: str = "BTCUSDT", market: str = "usdm") -> InstrumentRef:
    return InstrumentRef(
        scope=VenueScope(venue="BINANCE", environment=VenueEnvironment.PRODUCTION),
        symbol=symbol,
        market=market,
    )


# ---------------------------------------------------------------------------
# premiumIndex
# ---------------------------------------------------------------------------


def test_premium_index_parses_and_maps() -> None:
    wire = PremiumIndexWire.model_validate(load("fapi_premiumIndex.json"))
    snapshot = mapping.to_mark_price(wire, instrument=instrument(), collected_at=COLLECTED_AT)

    assert snapshot.mark_price == Decimal("65100.70000000")
    assert snapshot.index_price == Decimal("65129.55391304")
    assert snapshot.last_funding_rate == Decimal("0.00009540")
    assert snapshot.next_funding_time == datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
    assert snapshot.venue_time.tzinfo is UTC


def test_prices_keep_every_recorded_digit() -> None:
    """`65129.55391304` has 8 decimals; a float would not survive the round trip."""
    wire = PremiumIndexWire.model_validate(load("fapi_premiumIndex.json"))
    assert wire.indexPrice == "65129.55391304"
    assert str(parse_decimal(wire.indexPrice)) == "65129.55391304"


def test_testnet_and_production_prices_are_plausibly_similar() -> None:
    """**Finding.** The environments do not differ obviously enough to notice.

    Both recordings are BTCUSDT at the same moment: production 65100.70, testnet
    65102.39 — a 0.003% difference. A series that interleaved them would look
    completely normal, which is precisely why environment scoping cannot rely on
    anyone eyeballing the numbers (ADR-0010, ADR-0015).
    """
    production = PremiumIndexWire.model_validate(load("fapi_premiumIndex.json"))
    testnet = PremiumIndexWire.model_validate(load("fapi_premiumIndex_testnet.json"))

    assert production.symbol == testnet.symbol == "BTCUSDT"
    prod_price = parse_decimal(production.markPrice or "0")
    test_price = parse_decimal(testnet.markPrice or "0")
    relative_gap = abs(prod_price - test_price) / prod_price
    assert relative_gap < Decimal("0.001")


# ---------------------------------------------------------------------------
# fundingRate
# ---------------------------------------------------------------------------


def test_funding_history_parses_and_maps() -> None:
    payload = load("fapi_fundingRate.json")
    assert isinstance(payload, list)
    observations = [
        mapping.to_funding_rate(
            FundingRateWire.model_validate(item),
            instrument=instrument(),
            collected_at=COLLECTED_AT,
        )
        for item in payload
    ]
    assert len(observations) == 3
    assert observations[0].funding_rate == Decimal("0.00004423")


def test_funding_time_is_not_exactly_on_the_boundary() -> None:
    """**Finding.** One settlement landed 5 ms past the hour.

    A point-in-time join that tests equality against a computed 8-hour boundary
    silently drops this row. Join on a window (ADR-0015).
    """
    payload = load("fapi_fundingRate.json")
    assert isinstance(payload, list)
    times = [item["fundingTime"] for item in payload]
    off_boundary = [t for t in times if t % (8 * 3600 * 1000) != 0]
    assert off_boundary, "expected at least one off-boundary settlement in this recording"
    assert off_boundary[0] % (8 * 3600 * 1000) == 5


def test_funding_rows_carry_an_undocumented_rate_type() -> None:
    """**Finding.** `rateType` was not in our founding assumptions."""
    payload = load("fapi_fundingRate.json")
    assert isinstance(payload, list)
    wire = FundingRateWire.model_validate(payload[0])
    assert wire.rateType == "Regular"


# ---------------------------------------------------------------------------
# fundingInfo — the biggest finding
# ---------------------------------------------------------------------------


def test_funding_interval_is_per_symbol_and_not_always_eight_hours() -> None:
    """**Finding.** 4-hourly funding is the majority across the venue.

    The full 2026-08-09 capture: 442 symbols at 4h, 296 at 8h, 4 at 1h. This
    trimmed fixture preserves one of each cadence. Assuming 8h globally would
    mis-accrue carry on most of the venue (ADR-0016).
    """
    payload = load("fapi_fundingInfo.trimmed.json")
    assert isinstance(payload, list)
    intervals = {
        item["symbol"]: FundingInfoWire.model_validate(item).fundingIntervalHours
        for item in payload
    }
    assert intervals["BTCUSDT"] == 8
    assert intervals["LPTUSDT"] == 4
    assert intervals["AAPLUSDT"] == 1
    assert len(set(intervals.values())) > 1


def test_funding_update_time_is_null_for_the_symbols_we_care_about_most() -> None:
    """**Finding.** A strict `int` here crashes on BTCUSDT and ETHUSDT."""
    payload = load("fapi_fundingInfo.trimmed.json")
    assert isinstance(payload, list)
    by_symbol = {item["symbol"]: FundingInfoWire.model_validate(item) for item in payload}
    assert by_symbol["BTCUSDT"].updateTime is None
    assert by_symbol["ETHUSDT"].updateTime is None
    assert by_symbol["GTCUSDT"].updateTime is not None


def test_funding_rate_caps_differ_by_nearly_an_order_of_magnitude() -> None:
    """**Finding.** BTC is capped at ±0.30%, GTC at ±2.00%.

    Maximum harvestable carry is therefore symbol-specific, not a venue constant.
    """
    payload = load("fapi_fundingInfo.trimmed.json")
    assert isinstance(payload, list)
    schedules = {
        item["symbol"]: mapping.to_funding_schedule(FundingInfoWire.model_validate(item))
        for item in payload
    }
    assert schedules["BTCUSDT"].rate_cap == Decimal("0.00300")
    assert schedules["GTCUSDT"].rate_cap == Decimal("0.02000000")
    assert schedules["BTCUSDT"].settlements_per_day == 3
    assert schedules["LPTUSDT"].settlements_per_day == 6


# ---------------------------------------------------------------------------
# exchangeInfo
# ---------------------------------------------------------------------------


def test_exchange_info_parses_and_exposes_its_weight_limit() -> None:
    wire = ExchangeInfoWire.model_validate(load("fapi_exchangeInfo.trimmed.json"))
    assert wire.weight_limit_per_minute() == 2400
    assert wire.symbols


def test_min_notional_lives_under_notional_not_min_notional() -> None:
    """**Finding.** The founding README used `minNotional`; the venue sends `notional`."""
    wire = ExchangeInfoWire.model_validate(load("fapi_exchangeInfo.trimmed.json"))
    btc = next(s for s in wire.symbols if s.symbol == "BTCUSDT")
    min_notional = btc.filter_of("MIN_NOTIONAL")
    assert min_notional is not None
    assert min_notional.notional == "50"


def test_btc_filters_carry_the_tick_and_step_sizes_sizing_depends_on() -> None:
    wire = ExchangeInfoWire.model_validate(load("fapi_exchangeInfo.trimmed.json"))
    btc = next(s for s in wire.symbols if s.symbol == "BTCUSDT")
    price_filter = btc.filter_of("PRICE_FILTER")
    lot_size = btc.filter_of("LOT_SIZE")
    assert price_filter is not None and price_filter.tickSize == "0.10"
    assert lot_size is not None and lot_size.stepSize == "0.001"


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [("BTCUSDT", True), ("ETHUSDT", True), ("AAPLUSDT", False)],
)
def test_tokenised_equities_are_excluded_from_the_carry_universe(
    symbol: str, expected: bool
) -> None:
    """**Finding.** 153 of 854 symbols are tokenised equities and metals.

    `AAPLUSDT` is a `TRADIFI_PERPETUAL`. Nothing about its wire shape
    distinguishes it from a crypto perpetual — only `contractType` does
    (ADR-0016).
    """
    wire = ExchangeInfoWire.model_validate(load("fapi_exchangeInfo.trimmed.json"))
    symbols = {s.symbol: s for s in wire.symbols}
    if symbol not in symbols:
        pytest.skip(f"{symbol} not in the trimmed fixture")
    assert mapping.is_carry_candidate(symbols[symbol]) is expected


def test_non_trading_statuses_are_present_and_excluded() -> None:
    wire = ExchangeInfoWire.model_validate(load("fapi_exchangeInfo.trimmed.json"))
    statuses = {mapping.to_instrument_status(s.status) for s in wire.symbols}
    assert InstrumentStatus.SETTLING in statuses or InstrumentStatus.PENDING_TRADING in statuses
    for symbol in wire.symbols:
        if not mapping.to_instrument_status(symbol.status).is_tradeable:
            assert not mapping.is_carry_candidate(symbol)


def test_an_unrecognised_status_is_not_tradeable() -> None:
    """Fail closed: a status Binance adds later must not parse as tradeable."""
    assert mapping.to_instrument_status("SOME_NEW_STATE") is InstrumentStatus.UNKNOWN
    assert not mapping.to_instrument_status("SOME_NEW_STATE").is_tradeable
    assert mapping.to_contract_type("SOME_NEW_TYPE") is ContractType.UNKNOWN


# ---------------------------------------------------------------------------
# leverageBracket — authenticated first contact
# ---------------------------------------------------------------------------


def test_authenticated_leverage_brackets_parse_and_map() -> None:
    """The signed production response matches the tolerant wire schema."""
    payload = load("fapi_leverageBracket.trimmed.json")
    assert isinstance(payload, list)
    schedules = {
        item["symbol"]: mapping.to_margin_schedule(LeverageBracketWire.model_validate(item))
        for item in payload
    }

    btc = schedules["BTCUSDT"]
    assert len(btc.tiers) == 12
    assert btc.tiers[0].initial_leverage == 150
    assert btc.tiers[0].notional_cap == Decimal("300000")
    assert btc.tiers[0].maintenance_margin_ratio == Decimal("0.004")
    assert btc.tiers[-1].notional_cap == Decimal("1800000000")

    long_tail = schedules["HUSDT"]
    assert len(long_tail.tiers) == 4
    assert long_tail.tiers[0].initial_leverage == 4
    assert long_tail.tiers[0].maintenance_margin_ratio == Decimal("0.145")


def test_recorded_margin_tiers_are_contiguous_and_increasingly_conservative() -> None:
    payload = load("fapi_leverageBracket.trimmed.json")
    assert isinstance(payload, list)
    for item in payload:
        schedule = mapping.to_margin_schedule(LeverageBracketWire.model_validate(item))
        assert schedule.tiers[0].notional_floor == 0
        for previous, current in zip(schedule.tiers, schedule.tiers[1:], strict=False):
            assert current.notional_floor == previous.notional_cap
            assert current.initial_leverage <= previous.initial_leverage
            assert current.maintenance_margin_ratio >= previous.maintenance_margin_ratio


# ---------------------------------------------------------------------------
# bookTicker — spot and futures disagree
# ---------------------------------------------------------------------------


def test_futures_book_ticker_carries_time_and_update_id() -> None:
    wire = BookTickerWire.model_validate(load("fapi_bookTicker.json"))
    ticker = mapping.to_book_ticker(wire, instrument=instrument(), collected_at=COLLECTED_AT)
    assert ticker.venue_time is not None
    assert ticker.last_update_id == 11249974491002
    assert not ticker.is_crossed


def test_spot_book_ticker_carries_neither_and_still_parses() -> None:
    """**Finding.** Same endpoint name, different fields, per market.

    Requiring `time` would have made the spot hedge leg unparseable (ADR-0015).
    """
    wire = BookTickerWire.model_validate(load("spot_bookTicker.json"))
    ticker = mapping.to_book_ticker(
        wire, instrument=instrument(market="spot"), collected_at=COLLECTED_AT
    )
    assert ticker.venue_time is None
    assert ticker.last_update_id is None
    assert ticker.bid_price == Decimal("65126.10000000")


def test_spot_and_futures_prices_differ_which_is_the_basis() -> None:
    """The two legs of the carry are not the same price; that gap is the basis."""
    futures = BookTickerWire.model_validate(load("fapi_bookTicker.json"))
    spot = BookTickerWire.model_validate(load("spot_bookTicker.json"))
    assert futures.bidPrice != spot.bidPrice


# ---------------------------------------------------------------------------
# klines, time, errors, websocket
# ---------------------------------------------------------------------------


def test_klines_map_from_positional_arrays() -> None:
    payload = load("fapi_klines.json")
    assert isinstance(payload, list)
    kline = mapping.to_kline(payload[0], instrument=instrument(), collected_at=COLLECTED_AT)
    assert kline.open == Decimal("64793.70")
    assert kline.close == Decimal("65207.70")
    assert kline.high >= kline.low
    assert kline.trades == 300870
    assert kline.close_time > kline.open_time


def test_a_short_kline_row_is_rejected_rather_than_padded() -> None:
    with pytest.raises(mapping.MappingError, match="expected at least"):
        mapping.to_kline([1, "2"], instrument=instrument(), collected_at=COLLECTED_AT)


def test_server_time_parses() -> None:
    wire = ServerTimeWire.model_validate(load("fapi_time.json"))
    assert mapping.to_utc(wire.serverTime, "serverTime", "server").year == 2026


def test_recorded_error_bodies_classify_correctly() -> None:
    from venue_binance.errors import BinanceAPIError

    invalid = load("fapi_error_invalid_symbol.json")
    assert isinstance(invalid, dict)
    error = BinanceAPIError(
        status_code=400, code=invalid["code"], message=invalid["msg"], path="/fapi/v1/premiumIndex"
    )
    assert error.is_invalid_symbol
    assert not error.is_retryable

    bad_key = load("fapi_error_bad_api_key.json")
    assert isinstance(bad_key, dict)
    auth_error = BinanceAPIError(
        status_code=401,
        code=bad_key["code"],
        message=bad_key["msg"],
        path="/fapi/v1/leverageBracket",
    )
    assert auth_error.is_auth_problem
    assert not auth_error.is_retryable


def test_unauthenticated_leverage_bracket_still_requires_a_key() -> None:
    """**Historical finding.** Maintenance-margin tiers are not public.

    `GET /fapi/v1/leverageBracket` returned HTTP 401 `-2014` unauthenticated. The
    successful signed response is covered separately above (ADR-0018).
    """
    body = load("fapi_error_bad_api_key.json")
    assert isinstance(body, dict)
    assert body["code"] == -2014


def test_websocket_book_ticker_frame_maps() -> None:
    frame = load("ws_bookTicker_frame.json")
    assert isinstance(frame, dict)
    assert frame["stream"] == "btcusdt@bookTicker"
    wire = BookTickerStreamWire.model_validate(frame["data"])
    ticker = mapping.stream_to_book_ticker(
        wire, instrument=instrument(), collected_at=COLLECTED_AT
    )
    assert ticker.bid_price == Decimal("65091.30")
    assert ticker.ask_price == Decimal("65091.40")
    assert ticker.last_update_id == 11249981101619


def test_websocket_and_rest_book_tickers_share_no_field_names() -> None:
    """Which is why the two have separate wire models and separate mappers."""
    rest = set(BookTickerWire.model_fields)
    stream = set(BookTickerStreamWire.model_fields)
    assert not (rest & stream)


# ---------------------------------------------------------------------------
# Tolerance
# ---------------------------------------------------------------------------


def test_unknown_fields_are_retained_rather_than_rejected() -> None:
    """ADR-0003's tolerant parsing, exercised on a real payload plus a new field."""
    payload = load("fapi_premiumIndex.json")
    assert isinstance(payload, dict)
    wire = PremiumIndexWire.model_validate({**payload, "someFutureField": "surprise"})
    assert wire.markPrice == "65100.70000000"
    assert wire.model_extra is not None
    assert wire.model_extra["someFutureField"] == "surprise"


def test_a_missing_required_number_raises_rather_than_defaulting_to_zero() -> None:
    """A mark price of 0 because a field vanished would flow straight into sizing."""
    payload = load("fapi_premiumIndex.json")
    assert isinstance(payload, dict)
    without_mark = {k: v for k, v in payload.items() if k != "markPrice"}
    wire = PremiumIndexWire.model_validate(without_mark)
    with pytest.raises(mapping.MappingError, match="markPrice"):
        mapping.to_mark_price(wire, instrument=instrument(), collected_at=COLLECTED_AT)
