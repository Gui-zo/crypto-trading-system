"""Rate-limit budget tests.

The header names and casing asserted here are the ones observed on 2026-08-09.
Getting them wrong is silent: the budget simply never updates and drifts toward
a 429, then a 418 IP ban.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from venue_binance.endpoints import Market, documented_weight
from venue_binance.rate_limit import (
    RateLimitBudget,
    RateLimitExceeded,
    parse_retry_after,
)

NOW = datetime(2026, 8, 9, 16, 0, tzinfo=UTC)


def budget(**kwargs: object) -> RateLimitBudget:
    defaults: dict[str, object] = {"limit_per_minute": 100, "safety_fraction": 0.75, "now": NOW}
    defaults.update(kwargs)
    return RateLimitBudget(**defaults)  # type: ignore[arg-type]


def test_threshold_sits_below_the_venue_limit() -> None:
    assert budget().threshold == 75
    assert budget().limit == 100


def test_a_request_within_budget_is_allowed() -> None:
    b = budget()
    b.check(10, now=NOW)
    b.charge(10, now=NOW)
    assert b.snapshot(now=NOW).used == 10


def test_a_request_crossing_the_threshold_is_refused_before_sending() -> None:
    b = budget()
    b.charge(70, now=NOW)
    with pytest.raises(RateLimitExceeded, match="local threshold"):
        b.check(10, now=NOW)


def test_refusal_happens_at_the_local_threshold_not_the_venue_limit() -> None:
    """Being refused locally is always cheaper than a 429, let alone a ban."""
    b = budget()
    b.charge(74, now=NOW)
    b.check(1, now=NOW)  # exactly at the threshold is fine
    with pytest.raises(RateLimitExceeded):
        b.check(2, now=NOW)


def test_the_window_rolls_after_a_minute() -> None:
    b = budget()
    b.charge(70, now=NOW)
    later = NOW + timedelta(seconds=61)
    b.check(70, now=later)
    assert b.snapshot(now=later).used == 0


@pytest.mark.parametrize(
    "header",
    ["x-mbx-used-weight-1m", "X-MBX-USED-WEIGHT-1M", "X-Mbx-Used-Weight-1m"],
)
def test_the_used_weight_header_is_read_case_insensitively(header: str) -> None:
    """HTTP/2 lower-cases headers; the documented name is upper-case."""
    b = budget()
    snapshot = b.observe_headers({header: "42"}, now=NOW)
    assert snapshot is not None
    assert snapshot.used == 42


def test_the_venue_figure_overrides_the_local_estimate() -> None:
    """The venue's accounting is authoritative; ours only gates pre-flight."""
    b = budget()
    b.charge(5, now=NOW)
    b.observe_headers({"x-mbx-used-weight-1m": "60"}, now=NOW)
    assert b.snapshot(now=NOW).used == 60
    with pytest.raises(RateLimitExceeded):
        b.check(20, now=NOW)


def test_a_lower_venue_figure_is_also_adopted() -> None:
    """Their window may have rolled; trusting our higher count would over-refuse."""
    b = budget()
    b.charge(50, now=NOW)
    b.observe_headers({"x-mbx-used-weight-1m": "3"}, now=NOW)
    assert b.snapshot(now=NOW).used == 3


def test_spot_style_unsuffixed_header_is_understood() -> None:
    b = budget()
    assert b.observe_headers({"x-mbx-used-weight": "22"}, now=NOW) is not None


def test_the_suffixed_header_wins_when_both_are_present() -> None:
    """Spot sends both; the 1m form is the one that matches the 1m window."""
    b = budget()
    snapshot = b.observe_headers(
        {"x-mbx-used-weight": "5", "x-mbx-used-weight-1m": "22"}, now=NOW
    )
    assert snapshot is not None
    assert snapshot.used == 22


def test_absent_headers_leave_the_local_estimate_intact() -> None:
    """A response without the header must not reset the count to zero."""
    b = budget()
    b.charge(30, now=NOW)
    assert b.observe_headers({"content-type": "application/json"}, now=NOW) is None
    assert b.snapshot(now=NOW).used == 30


def test_a_malformed_header_value_is_ignored() -> None:
    b = budget()
    b.charge(30, now=NOW)
    assert b.observe_headers({"x-mbx-used-weight-1m": "not-a-number"}, now=NOW) is None
    assert b.snapshot(now=NOW).used == 30


def test_the_limit_can_be_adopted_from_exchange_info() -> None:
    b = budget()
    b.adopt_limit(2400)
    assert b.limit == 2400
    assert b.threshold == 1800


def test_snapshot_reports_remaining_and_utilisation() -> None:
    b = budget()
    b.charge(25, now=NOW)
    snapshot = b.snapshot(now=NOW)
    assert snapshot.remaining == 75
    assert snapshot.utilisation == pytest.approx(0.25)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"limit_per_minute": 0}, "limit must be positive"),
        ({"safety_fraction": 0}, "safety fraction"),
        ({"safety_fraction": 1.5}, "safety fraction"),
    ],
)
def test_invalid_configuration_is_refused(kwargs: dict[str, object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        budget(**kwargs)


def test_a_negative_request_weight_is_refused() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        budget().check(-1, now=NOW)


def test_retry_after_is_parsed_case_insensitively() -> None:
    assert parse_retry_after({"Retry-After": "30"}) == 30.0
    assert parse_retry_after({"retry-after": "0.5"}) == 0.5
    assert parse_retry_after({}) is None
    assert parse_retry_after({"retry-after": "soon"}) is None


def test_unknown_endpoints_are_assumed_expensive() -> None:
    """Defaulting an unknown path to 1 makes the pre-flight check useless."""
    assert documented_weight(Market.USDM, "/fapi/v1/premiumIndex") == 1
    assert documented_weight(Market.USDM, "/fapi/v1/something-new") == 10
