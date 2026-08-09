"""Decimal-discipline tests (ADR-0011).

The property tests are the point: quantization has to hold for *every* value and
step a symbol filter might produce, not for the handful anyone thinks to write
down. The invariant that matters most is that rounding down never rounds up.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from domain.precision import (
    BPS,
    PrecisionError,
    from_bps,
    is_multiple_of,
    parse_decimal,
    quantize_down,
    quantize_up,
    to_bps,
)

# Realistic Binance filter steps: BTC quantity step, price tick, satoshi scale.
STEPS = [Decimal("0.001"), Decimal("0.1"), Decimal("0.00000001"), Decimal("1"), Decimal("25")]

decimals = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("10000000"),
    allow_nan=False,
    allow_infinity=False,
    places=8,
)


# ---------------------------------------------------------------------------
# parse_decimal
# ---------------------------------------------------------------------------


def test_float_input_is_refused_outright() -> None:
    with pytest.raises(PrecisionError, match="refusing to build a Decimal from a float"):
        parse_decimal(0.1)  # type: ignore[arg-type]


def test_strings_parse_exactly() -> None:
    assert parse_decimal("0.1") == Decimal("0.1")
    assert parse_decimal("64000.12345678") == Decimal("64000.12345678")
    assert parse_decimal(7) == Decimal("7")


def test_the_reason_floats_are_refused() -> None:
    """Documents the actual failure this rule prevents."""
    assert Decimal(0.1) != Decimal("0.1")  # noqa: RUF032 - the whole point of the test
    assert parse_decimal("0.1") == Decimal("0.1")


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_non_finite_values_are_refused(raw: str) -> None:
    with pytest.raises(PrecisionError, match="finite"):
        parse_decimal(raw)


@pytest.mark.parametrize("raw", ["", "abc", "1.2.3", "--1"])
def test_malformed_input_is_refused(raw: str) -> None:
    with pytest.raises(PrecisionError, match="not a valid decimal"):
        parse_decimal(raw)


# ---------------------------------------------------------------------------
# Quantization
# ---------------------------------------------------------------------------


def test_quantize_down_floors_to_the_step_grid() -> None:
    assert quantize_down(Decimal("0.1234"), Decimal("0.001")) == Decimal("0.123")
    assert quantize_down(Decimal("64000.99"), Decimal("0.1")) == Decimal("64000.9")


def test_quantize_up_raises_to_the_step_grid() -> None:
    assert quantize_up(Decimal("0.1231"), Decimal("0.001")) == Decimal("0.124")
    assert quantize_up(Decimal("64000.01"), Decimal("0.1")) == Decimal("64000.1")


def test_a_value_already_on_the_grid_is_unchanged_in_both_directions() -> None:
    value = Decimal("0.123")
    step = Decimal("0.001")
    assert quantize_down(value, step) == value
    assert quantize_up(value, step) == value


def test_the_result_carries_the_step_exponent() -> None:
    """The venue compares string forms, so 0.10 and 0.1 are not interchangeable."""
    assert str(quantize_down(Decimal("0.1"), Decimal("0.01"))) == "0.10"


def test_quantizing_below_the_step_floors_to_zero() -> None:
    """The minNotional case: annoying, but the safe direction to fail."""
    assert quantize_down(Decimal("0.0004"), Decimal("0.001")) == Decimal("0.000")


def test_negative_values_round_away_from_zero_downward() -> None:
    """`//` would truncate toward zero here and silently round the wrong way."""
    assert quantize_down(Decimal("-0.1234"), Decimal("0.001")) == Decimal("-0.124")
    assert quantize_up(Decimal("-0.1234"), Decimal("0.001")) == Decimal("-0.123")


@pytest.mark.parametrize("step", [Decimal("0"), Decimal("-0.001")])
def test_a_nonpositive_step_is_refused(step: Decimal) -> None:
    with pytest.raises(PrecisionError, match="finite and positive"):
        quantize_down(Decimal("1"), step)


def test_a_non_finite_value_cannot_be_quantized() -> None:
    with pytest.raises(PrecisionError, match="non-finite"):
        quantize_down(Decimal("NaN"), Decimal("0.1"))


@given(value=decimals, step=st.sampled_from(STEPS))
def test_quantize_down_never_exceeds_the_input(value: Decimal, step: Decimal) -> None:
    """The safety invariant: capping must never round a size *up* past its cap."""
    assert quantize_down(value, step) <= value


@given(value=decimals, step=st.sampled_from(STEPS))
def test_quantize_up_never_undercuts_the_input(value: Decimal, step: Decimal) -> None:
    assert quantize_up(value, step) >= value


@given(value=decimals, step=st.sampled_from(STEPS))
def test_quantized_results_land_on_the_step_grid(value: Decimal, step: Decimal) -> None:
    assert is_multiple_of(quantize_down(value, step), step)
    assert is_multiple_of(quantize_up(value, step), step)


@given(value=decimals, step=st.sampled_from(STEPS))
def test_the_two_directions_differ_by_at_most_one_step(value: Decimal, step: Decimal) -> None:
    assert quantize_up(value, step) - quantize_down(value, step) <= step


@given(value=decimals, step=st.sampled_from(STEPS))
def test_quantization_is_idempotent(value: Decimal, step: Decimal) -> None:
    once = quantize_down(value, step)
    assert quantize_down(once, step) == once


# ---------------------------------------------------------------------------
# Basis points
# ---------------------------------------------------------------------------


def test_basis_point_round_trip() -> None:
    assert to_bps(Decimal("0.0001")) == Decimal("1")
    assert to_bps(Decimal("0.01")) == Decimal("100")
    assert from_bps(Decimal("8")) == Decimal("0.0008")
    assert Decimal("0.0001") == BPS


@given(bps=st.decimals(min_value=0, max_value=10000, allow_nan=False, allow_infinity=False,
                       places=4))
def test_bps_conversions_are_inverses(bps: Decimal) -> None:
    assert to_bps(from_bps(bps)) == bps


def test_the_predecessors_eight_cent_gate_is_800_bps_here() -> None:
    """Documents §4: cent-shaped thresholds are wrong by ~3 orders of magnitude."""
    eight_cents_on_a_dollar = Decimal("0.08")
    assert to_bps(eight_cents_on_a_dollar) == Decimal("800")
