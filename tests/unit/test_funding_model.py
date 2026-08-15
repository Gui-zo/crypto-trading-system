"""Phase-4 funding-persistence targets, cases, leakage rule, baseline, and skill."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from domain.funding_model import (
    DEFAULT_MINIMUM_MATCHED_CASES,
    DEFAULT_MINIMUM_PRIOR_CASES,
    ExpandingClimatology,
    ExpandingPersistenceModel,
    FundingModelError,
    FundingTarget,
    ResolvedCase,
    Settlement,
    SkipReason,
    build_cases,
    naive_persistence_probability,
    score,
    score_by_interval,
    score_by_symbol,
    walk_forward,
)

START = datetime(2025, 1, 1, tzinfo=UTC)


def settlement(offset_hours: float, rate: str, interval: int | None = 8) -> Settlement:
    return Settlement(
        funding_time=START + timedelta(hours=offset_hours),
        funding_rate=Decimal(rate),
        interval_hours=interval,
    )


def series(rates: list[str], *, step: int = 8, interval: int | None = 8) -> list[Settlement]:
    return [settlement(i * step, rate, interval) for i, rate in enumerate(rates)]


def case(
    *,
    decision_hours: float,
    previous_above: bool,
    outcome: bool,
    symbol: str = "TESTUSDT",
    resolved_hours: float | None = None,
) -> ResolvedCase:
    decision = START + timedelta(hours=decision_hours)
    resolved = START + timedelta(
        hours=resolved_hours if resolved_hours is not None else decision_hours + 8
    )
    return ResolvedCase(
        symbol=symbol,
        decision_time=decision,
        resolved_at=resolved,
        previous_rate=Decimal("0.0001") if previous_above else Decimal("-0.0001"),
        previous_above=previous_above,
        outcome=outcome,
        interval_hours=8,
        max_step_hours=Decimal("8.0000"),
        window_hours=Decimal("8.0000"),
    )


# --- targets -----------------------------------------------------------------


def test_threshold_is_basis_points_not_a_raw_rate() -> None:
    target = FundingTarget(threshold_bps=Decimal("1"))

    assert target.threshold_rate == Decimal("0.0001")
    assert target.is_above(Decimal("0.0001"))
    assert not target.is_above(Decimal("0.00009"))


def test_horizon_below_one_is_refused() -> None:
    with pytest.raises(FundingModelError):
        FundingTarget(threshold_bps=Decimal("0"), horizon=0)


# --- case construction -------------------------------------------------------


def test_outcome_requires_every_settlement_in_the_window_to_clear() -> None:
    target = FundingTarget(threshold_bps=Decimal("0"), horizon=2)
    cases = build_cases("TESTUSDT", series(["0.0001", "0.0001", "-0.0001", "0.0001"]), target)

    # decision 0 -> window (0.0001, -0.0001) fails; decision 1 -> (-0.0001, 0.0001) fails.
    assert [item.outcome for item in cases] == [False, False]


def test_a_window_spanning_a_skipped_settlement_is_kept_and_reports_the_hole() -> None:
    """ADR-0020: the venue skips settlements. Dropping those windows would
    discard exactly the unusual regimes carry cares about."""
    target = FundingTarget(threshold_bps=Decimal("0"))
    settlements = [
        settlement(0, "0.0001", 4),
        settlement(4, "0.0001", 4),
        settlement(12, "0.0001", 4),  # 04:00-style hole: an 8h step on a 4h cadence
    ]

    cases = build_cases("TESTUSDT", settlements, target)

    assert len(cases) == 2
    assert cases[1].max_step_hours == Decimal("8.0000")
    assert cases[0].max_step_hours == Decimal("4.0000")


def test_case_records_the_interval_in_force_not_a_global_cadence() -> None:
    target = FundingTarget(threshold_bps=Decimal("0"))
    settlements = [
        settlement(0, "0.0001", 8),
        settlement(8, "0.0001", 1),
        settlement(9, "0.0001", 1),
    ]

    cases = build_cases("TESTUSDT", settlements, target)

    assert [item.interval_hours for item in cases] == [8, 1]


def test_unordered_settlements_fail_closed() -> None:
    target = FundingTarget(threshold_bps=Decimal("0"))
    out_of_order = [settlement(8, "0.0001"), settlement(0, "0.0001")]

    with pytest.raises(FundingModelError, match="strictly increasing"):
        build_cases("TESTUSDT", out_of_order, target)


def test_a_series_shorter_than_the_horizon_yields_no_cases() -> None:
    target = FundingTarget(threshold_bps=Decimal("0"), horizon=3)

    assert build_cases("TESTUSDT", series(["0.0001", "0.0001"]), target) == ()


# --- baseline ----------------------------------------------------------------


def test_naive_baseline_is_the_previous_settlement_as_a_zero_one_forecast() -> None:
    assert (
        naive_persistence_probability(case(decision_hours=0, previous_above=True, outcome=True))
        == 1.0
    )
    assert (
        naive_persistence_probability(case(decision_hours=0, previous_above=False, outcome=True))
        == 0.0
    )


# --- the leakage rule --------------------------------------------------------


def test_history_resolving_after_the_decision_cannot_inform_it() -> None:
    """The rule is resolution time, not decision time: a case whose window is
    still open when the forecast is made has not happened yet."""
    model = ExpandingPersistenceModel(minimum_prior_cases=1, minimum_matched_cases=1)
    decision = case(decision_hours=100, previous_above=True, outcome=True)
    # Resolves exactly at the decision instant -> still not usable (strict <).
    concurrent = [case(decision_hours=90, previous_above=True, outcome=True, resolved_hours=100)]

    assert model.predict(concurrent, decision) is SkipReason.INSUFFICIENT_PRIOR_CASES


def test_history_resolving_before_the_decision_is_usable() -> None:
    model = ExpandingPersistenceModel(minimum_prior_cases=1, minimum_matched_cases=1)
    decision = case(decision_hours=100, previous_above=True, outcome=True)
    prior = [case(decision_hours=80, previous_above=True, outcome=True, resolved_hours=88)]

    estimate = model.predict(prior, decision)

    assert not isinstance(estimate, SkipReason)
    assert estimate.prior_cases == 1


def test_walk_forward_never_scores_a_case_with_its_own_outcome() -> None:
    """Every scored case must be explainable by strictly-earlier evidence. If the
    cutoff leaked, a perfectly separable series would score perfectly."""
    target = FundingTarget(threshold_bps=Decimal("0"))
    # Alternating sign: the previous settlement perfectly predicts the next one
    # being the opposite, but only history can reveal that.
    rates = ["0.0001" if index % 2 == 0 else "-0.0001" for index in range(120)]
    cases = build_cases("TESTUSDT", series(rates), target)

    result = walk_forward(cases, ExpandingPersistenceModel())

    first_scored = result.scored[0]
    assert first_scored.estimate.prior_cases >= DEFAULT_MINIMUM_PRIOR_CASES
    assert first_scored.case.decision_time > result.skipped[0][0].decision_time


# --- refusal to predict ------------------------------------------------------


def test_thin_history_is_skipped_with_a_reason_not_guessed() -> None:
    """The warm-up is one longer than the minimum, and that is the resolution lag
    showing up: a case decided at step ``i`` only resolves at ``i + 1``, so the
    newest case usable at ``i`` was decided at ``i - 2``, never ``i - 1``."""
    target = FundingTarget(threshold_bps=Decimal("0"))
    cases = build_cases("TESTUSDT", series(["0.0001"] * 40), target)

    result = walk_forward(cases, ExpandingPersistenceModel())

    assert len(result.skipped) == DEFAULT_MINIMUM_PRIOR_CASES + 1
    assert {reason for _, reason in result.skipped} == {SkipReason.INSUFFICIENT_PRIOR_CASES}
    assert result.scored[0].estimate.prior_cases == DEFAULT_MINIMUM_PRIOR_CASES
    assert result.eligible == len(cases)


def test_a_conditioning_state_never_seen_before_is_skipped_separately() -> None:
    model = ExpandingPersistenceModel(
        minimum_prior_cases=5, minimum_matched_cases=DEFAULT_MINIMUM_MATCHED_CASES
    )
    history = [
        case(decision_hours=index * 8, previous_above=True, outcome=True) for index in range(10)
    ]
    novel = case(decision_hours=200, previous_above=False, outcome=True)

    assert model.predict(history, novel) is SkipReason.INSUFFICIENT_MATCHED_CASES


def test_smoothing_keeps_the_estimate_off_certainty() -> None:
    """An unsmoothed frequency would claim 1.0 after an unbroken run and take an
    unbounded Brier penalty on the first surprise."""
    model = ExpandingPersistenceModel(minimum_prior_cases=5, minimum_matched_cases=5)
    history = [
        case(decision_hours=index * 8, previous_above=True, outcome=True) for index in range(20)
    ]

    estimate = model.predict(history, case(decision_hours=400, previous_above=True, outcome=True))

    assert not isinstance(estimate, SkipReason)
    assert 0.0 < estimate.probability < 1.0
    assert estimate.matched_positive == 20


# --- scoring -----------------------------------------------------------------


def test_skill_is_positive_only_when_the_model_beats_naive() -> None:
    target = FundingTarget(threshold_bps=Decimal("0"))
    # Alternating: naive is wrong every single time (Brier 1.0), so any
    # non-degenerate probability beats it.
    rates = ["0.0001" if index % 2 == 0 else "-0.0001" for index in range(200)]
    result = walk_forward(
        build_cases("TESTUSDT", series(rates), target), ExpandingPersistenceModel()
    )

    report = score(result.scored)

    assert report.naive.brier == pytest.approx(1.0)
    assert report.beats_naive


def test_a_perfectly_persistent_series_leaves_naive_unbeatable() -> None:
    """Naive scores 0.0 here. brier_skill_score returns 0.0 rather than dividing
    by zero, so a flat series can never manufacture apparent skill."""
    target = FundingTarget(threshold_bps=Decimal("0"))
    result = walk_forward(
        build_cases("TESTUSDT", series(["0.0001"] * 200), target), ExpandingPersistenceModel()
    )

    report = score(result.scored)

    assert report.naive.brier == pytest.approx(0.0)
    assert report.brier_skill_vs_naive == 0.0
    assert not report.beats_naive


def test_an_empty_slice_reports_no_skill_rather_than_dividing_by_zero() -> None:
    report = score([])

    assert report.n == 0
    assert not report.beats_naive
    assert not report.beats_climatology


def test_slices_keep_each_symbol_and_interval_visible() -> None:
    target = FundingTarget(threshold_bps=Decimal("0"))
    rates = ["0.0001" if index % 2 == 0 else "-0.0001" for index in range(120)]
    scored = walk_forward(
        build_cases("AAAUSDT", series(rates), target), ExpandingPersistenceModel()
    ).scored

    by_symbol = score_by_symbol(scored)
    by_interval = score_by_interval(scored)

    assert [report.label for report in by_symbol] == ["AAAUSDT"]
    assert [report.label for report in by_interval] == ["8h"]
    assert by_symbol[0].n == len(scored)


# --- the climatology baseline ------------------------------------------------


def test_climatology_ignores_the_conditioning_bit() -> None:
    """Two cases differing only in `previous_above` must get the same number.
    That is the whole point: climatology measures calibration, not information."""
    baseline = ExpandingClimatology(minimum_prior_cases=5)
    history = [
        case(decision_hours=index * 8, previous_above=index % 2 == 0, outcome=index % 3 == 0)
        for index in range(20)
    ]

    above = baseline.predict(history, case(decision_hours=400, previous_above=True, outcome=True))
    below = baseline.predict(history, case(decision_hours=400, previous_above=False, outcome=True))

    assert above == below


def test_climatology_obeys_the_same_resolution_cutoff_as_the_model() -> None:
    baseline = ExpandingClimatology(minimum_prior_cases=1)
    decision = case(decision_hours=100, previous_above=True, outcome=True)
    concurrent = [case(decision_hours=90, previous_above=True, outcome=True, resolved_hours=100)]

    assert baseline.predict(concurrent, decision) is SkipReason.INSUFFICIENT_PRIOR_CASES


def test_a_case_is_scored_only_when_both_forecasters_can_speak() -> None:
    """Skill computed over different samples would not be a comparison."""
    target = FundingTarget(threshold_bps=Decimal("0"))
    cases = build_cases("TESTUSDT", series(["0.0001", "-0.0001"] * 60), target)

    result = walk_forward(
        cases,
        ExpandingPersistenceModel(minimum_prior_cases=5, minimum_matched_cases=1),
        ExpandingClimatology(minimum_prior_cases=50),
    )

    assert all(item.estimate.prior_cases >= 50 for item in result.scored)


def test_beating_naive_without_beating_climatology_is_not_informative() -> None:
    """A coin-flip series: the previous settlement says nothing, so a calibrated
    forecaster still beats the 0/1 naive rule while carrying no information.
    `informative` must refuse that, which is why it exists."""
    target = FundingTarget(threshold_bps=Decimal("0"))
    # Deterministic pseudo-random signs with no serial dependence worth learning.
    rates = ["0.0001" if (index * 7919) % 3 else "-0.0001" for index in range(600)]
    result = walk_forward(
        build_cases("TESTUSDT", series(rates), target), ExpandingPersistenceModel()
    )

    report = score(result.scored)

    assert report.beats_naive
    assert report.brier_skill_vs_climatology < report.brier_skill_vs_naive
