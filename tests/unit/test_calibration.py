"""Calibration statistics tests.

These are the metrics that decide whether the funding-persistence model is
allowed to exist, so they are checked against values computable by hand rather
than against whatever the implementation happens to return.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from domain.calibration import (
    brier_skill_score,
    crps_ensemble,
    pit_histogram,
    pit_value,
    reliability,
)


def test_an_empty_sample_reports_nothing_rather_than_perfection() -> None:
    report = reliability([])
    assert report.n == 0
    assert report.bins == ()


def test_a_perfect_forecaster_scores_zero_brier() -> None:
    report = reliability([(1.0, True), (0.0, False), (1.0, True)])
    assert report.brier == 0.0
    assert report.ece == 0.0


def test_a_maximally_wrong_forecaster_scores_one() -> None:
    report = reliability([(0.0, True), (1.0, False)])
    assert report.brier == 1.0


def test_brier_is_the_mean_squared_probability_error() -> None:
    report = reliability([(0.7, True), (0.3, False)])
    assert report.brier == pytest.approx((0.09 + 0.09) / 2)


def test_a_calibrated_but_uncertain_forecaster_has_low_ece_and_nonzero_brier() -> None:
    """Fifty 0.5-forecasts that resolve half true: perfectly calibrated, unskilled."""
    samples = [(0.5, i % 2 == 0) for i in range(50)]
    report = reliability(samples)
    assert report.brier == pytest.approx(0.25)
    assert report.ece == pytest.approx(0.0)


def test_a_probability_of_one_lands_in_the_last_bin() -> None:
    report = reliability([(1.0, True)], bins=10)
    assert len(report.bins) == 1
    assert report.bins[0].lower == 0.9
    assert report.bins[0].upper == 1.0


def test_empty_bins_are_omitted() -> None:
    report = reliability([(0.05, False), (0.95, True)], bins=10)
    assert len(report.bins) == 2


def test_bin_contents_are_reported_faithfully() -> None:
    report = reliability([(0.15, True), (0.15, False)], bins=10)
    (single,) = report.bins
    assert single.count == 2
    assert single.mean_predicted == pytest.approx(0.15)
    assert single.empirical_freq == pytest.approx(0.5)


def test_a_nonpositive_bin_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="bins must be"):
        reliability([(0.5, True)], bins=0)


@given(
    samples=st.lists(
        st.tuples(st.floats(min_value=0.0, max_value=1.0), st.booleans()),
        min_size=1,
        max_size=50,
    )
)
def test_brier_and_ece_always_lie_in_the_unit_interval(
    samples: list[tuple[float, bool]],
) -> None:
    report = reliability(samples)
    assert 0.0 <= report.brier <= 1.0
    assert 0.0 <= report.ece <= 1.0
    assert sum(b.count for b in report.bins) == len(samples)


# ---------------------------------------------------------------------------
# PIT
# ---------------------------------------------------------------------------


def test_pit_value_is_the_mid_rank_fraction() -> None:
    assert pit_value([1.0, 2.0, 3.0, 4.0], 2.5) == 0.5
    assert pit_value([1.0, 2.0, 3.0, 4.0], 0.0) == 0.0
    assert pit_value([1.0, 2.0, 3.0, 4.0], 5.0) == 1.0


def test_pit_value_handles_ties_by_splitting_them() -> None:
    assert pit_value([1.0, 2.0, 2.0, 3.0], 2.0) == pytest.approx(0.5)


def test_pit_value_requires_an_ensemble() -> None:
    with pytest.raises(ValueError, match="non-empty ensemble"):
        pit_value([], 1.0)


def test_pit_histogram_buckets_observations() -> None:
    pairs = [([1.0, 2.0, 3.0], 0.0), ([1.0, 2.0, 3.0], 4.0)]
    counts = pit_histogram(pairs, bins=2)
    assert counts == (1, 1)


def test_a_nonpositive_pit_bin_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="bins must be"):
        pit_histogram([], bins=0)


# ---------------------------------------------------------------------------
# CRPS
# ---------------------------------------------------------------------------


def test_crps_of_a_point_forecast_is_the_absolute_error() -> None:
    assert crps_ensemble([5.0], 7.0) == pytest.approx(2.0)
    assert crps_ensemble([5.0], 5.0) == pytest.approx(0.0)


def test_crps_penalizes_a_wider_ensemble_at_the_same_center() -> None:
    tight = crps_ensemble([4.9, 5.0, 5.1], 5.0)
    wide = crps_ensemble([1.0, 5.0, 9.0], 5.0)
    assert tight < wide


def test_crps_requires_an_ensemble() -> None:
    with pytest.raises(ValueError, match="non-empty ensemble"):
        crps_ensemble([], 1.0)


# ---------------------------------------------------------------------------
# Brier skill — the gate that lets the model layer exist
# ---------------------------------------------------------------------------


def test_matching_the_naive_baseline_is_zero_skill() -> None:
    assert brier_skill_score(0.2, 0.2) == 0.0


def test_beating_the_baseline_is_positive_skill() -> None:
    assert brier_skill_score(0.1, 0.2) == pytest.approx(0.5)


def test_losing_to_the_baseline_is_negative_skill() -> None:
    assert brier_skill_score(0.4, 0.2) == pytest.approx(-1.0)


def test_a_perfect_baseline_admits_no_skill_rather_than_dividing_by_zero() -> None:
    assert brier_skill_score(0.0, 0.0) == 0.0


def test_negative_brier_scores_are_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        brier_skill_score(-0.1, 0.2)
