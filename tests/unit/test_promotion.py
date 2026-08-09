"""Promotion-gate tests.

The most important tests in this file are the ones that fail on a naive port:
a backtest contributing days, a zero-evidence gate reading PASS, and a ceiling
gate satisfied by ``observed >= required``. Each is a way a real system quietly
promotes itself.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from domain.promotion import (
    REQUIRED_PROSPECTIVE_PAPER_DAYS,
    AccrualGate,
    AccrualSample,
    CeilingGate,
    EvidenceDay,
    EvidenceSource,
    Gate,
    GateStatus,
    binding_gate,
    estimate_daily_rate,
    is_promotion_eligible,
    prospective_days,
    wall_clock_gate,
)

TODAY = date(2026, 8, 9)


def days_back(count: int, source: EvidenceSource = EvidenceSource.PAPER_PROSPECTIVE,
              *, offset: int = 1) -> list[EvidenceDay]:
    """``count`` consecutive evidence days ending ``offset`` days before TODAY."""
    return [
        EvidenceDay(observed_on=TODAY - timedelta(days=offset + i), source=source)
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# Evidence eligibility
# ---------------------------------------------------------------------------


def test_only_prospective_paper_evidence_is_promotion_eligible() -> None:
    assert is_promotion_eligible(EvidenceSource.PAPER_PROSPECTIVE)
    for source in (EvidenceSource.BACKTEST, EvidenceSource.TESTNET, EvidenceSource.SYNTHETIC):
        assert not is_promotion_eligible(source)


def test_five_years_of_backtest_days_count_for_nothing() -> None:
    """The whole point of ADR-0012, as an executable assertion."""
    archive = days_back(5 * 365, EvidenceSource.BACKTEST)
    assert prospective_days(archive, today=TODAY) == 0


def test_testnet_days_do_not_count_either() -> None:
    assert prospective_days(days_back(200, EvidenceSource.TESTNET), today=TODAY) == 0


# ---------------------------------------------------------------------------
# Consecutive-day arithmetic
# ---------------------------------------------------------------------------


def test_an_unbroken_run_counts_every_day() -> None:
    assert prospective_days(days_back(30), today=TODAY) == 30


def test_today_is_never_counted_because_it_is_still_accruing() -> None:
    evidence = [*days_back(10), EvidenceDay(TODAY, EvidenceSource.PAPER_PROSPECTIVE)]
    assert prospective_days(evidence, today=TODAY) == 10


def test_a_single_missed_day_resets_the_run() -> None:
    """89 days, one missed day, then 40 more — is 40, not 129 and not 89."""
    recent = days_back(40, offset=1)  # days 1..40 before today
    # day 41 is missing
    old = days_back(89, offset=42)  # days 42..130 before today
    assert prospective_days(old + recent, today=TODAY) == 40


def test_contiguous_blocks_do_join_into_one_run() -> None:
    """Guards the test above: the reset must come from the gap, not the split."""
    recent = days_back(40, offset=1)
    older = days_back(89, offset=41)
    assert prospective_days(older + recent, today=TODAY) == 129


def test_a_backtest_day_cannot_bridge_a_gap_in_paper_operation() -> None:
    before_gap = days_back(20, offset=6)
    bridge = [EvidenceDay(TODAY - timedelta(days=5), EvidenceSource.BACKTEST)]
    after_gap = days_back(4, offset=1)
    assert prospective_days(before_gap + bridge + after_gap, today=TODAY) == 4


def test_a_campaign_that_stopped_has_no_current_run() -> None:
    """Ninety days ending a week ago is not ninety consecutive days as of today."""
    stale = days_back(90, offset=8)
    assert prospective_days(stale, today=TODAY) == 0


def test_no_evidence_at_all_is_zero_days() -> None:
    assert prospective_days([], today=TODAY) == 0


def test_duplicate_days_are_counted_once() -> None:
    duplicated = days_back(5) + days_back(5)
    assert prospective_days(duplicated, today=TODAY) == 5


def test_future_dated_days_are_ignored() -> None:
    future = [EvidenceDay(TODAY + timedelta(days=3), EvidenceSource.PAPER_PROSPECTIVE)]
    assert prospective_days(days_back(4) + future, today=TODAY) == 4


@given(run_length=st.integers(min_value=0, max_value=400))
def test_run_length_is_exactly_what_was_recorded(run_length: int) -> None:
    assert prospective_days(days_back(run_length), today=TODAY) == run_length


# ---------------------------------------------------------------------------
# Accrual gates
# ---------------------------------------------------------------------------


def test_a_gate_with_no_evidence_is_unavailable_not_passing() -> None:
    """`0 net carry bps >= threshold 0` must never render as a cleared gate."""
    gate = AccrualGate(
        key="net_carry",
        label="net carry",
        observed=0.0,
        required=0.0,
        daily_rate=None,
        has_evidence=False,
    )
    assert gate.status is GateStatus.UNAVAILABLE
    assert gate.projected_days is None


def test_a_strict_gate_is_not_cleared_by_a_tie() -> None:
    """Matching the naive baseline means the model added nothing."""
    tie = AccrualGate(
        key="skill", label="brier skill", observed=0.0, required=0.0,
        daily_rate=None, strict=True,
    )
    assert tie.status is not GateStatus.PASS

    better = AccrualGate(
        key="skill", label="brier skill", observed=0.05, required=0.0,
        daily_rate=None, strict=True,
    )
    assert better.status is GateStatus.PASS


def test_a_non_strict_gate_is_cleared_by_reaching_the_threshold() -> None:
    gate = AccrualGate(
        key="settlements", label="settlements", observed=250.0, required=250.0, daily_rate=3.0
    )
    assert gate.status is GateStatus.PASS
    assert gate.projected_days == 0


def test_a_gate_with_no_rate_is_stalled_not_accruing() -> None:
    gate = AccrualGate(
        key="settlements", label="settlements", observed=10.0, required=250.0, daily_rate=None
    )
    assert gate.status is GateStatus.STALLED
    assert gate.projected_days is None


def test_projection_rounds_up_to_whole_days() -> None:
    gate = AccrualGate(
        key="settlements", label="settlements", observed=240.0, required=250.0, daily_rate=3.0
    )
    assert gate.projected_days == 4  # 10 / 3 = 3.33 -> 4
    assert gate.projected_date(TODAY) == TODAY + timedelta(days=4)


def test_fraction_complete_is_clamped() -> None:
    over = AccrualGate(key="k", label="l", observed=500.0, required=250.0, daily_rate=1.0)
    assert over.fraction_complete == 1.0
    zero_required = AccrualGate(key="k", label="l", observed=0.0, required=0.0, daily_rate=1.0)
    assert zero_required.fraction_complete == 1.0


# ---------------------------------------------------------------------------
# Wall-clock gates
# ---------------------------------------------------------------------------


def test_a_wall_clock_gate_projects_the_exact_remainder() -> None:
    gate = wall_clock_gate("days", "paper days", observed_days=30)
    assert gate.required == float(REQUIRED_PROSPECTIVE_PAPER_DAYS)
    assert gate.projected_days == 60
    assert gate.status is GateStatus.ACCRUING


def test_a_wall_clock_gate_clears_on_the_threshold_day() -> None:
    gate = wall_clock_gate("days", "paper days", observed_days=90)
    assert gate.status is GateStatus.PASS
    assert gate.projected_days == 0


def test_a_wall_clock_gate_with_no_campaign_is_unavailable() -> None:
    """No projected date for a campaign nobody has started."""
    gate = wall_clock_gate("days", "paper days", observed_days=0, campaign_running=False)
    assert gate.status is GateStatus.UNAVAILABLE
    assert gate.projected_days is None


# ---------------------------------------------------------------------------
# Ceiling gates
# ---------------------------------------------------------------------------


def test_a_ceiling_gate_fails_on_a_single_breach() -> None:
    gate = CeilingGate(key="violations", label="invariant violations", observed=1.0, limit=0.0)
    assert gate.status is GateStatus.FAILED
    assert gate.breach == 1.0


def test_a_breach_is_permanent_and_has_no_projected_date() -> None:
    gate = CeilingGate(key="violations", label="invariant violations", observed=3.0, limit=0.0)
    assert gate.projected_days is None
    assert gate.projected_date(TODAY) is None


def test_a_ceiling_gate_with_no_evidence_is_unavailable() -> None:
    """Zero violations across zero opportunities to violate proves nothing."""
    gate = CeilingGate(
        key="violations", label="invariant violations", observed=0.0, limit=0.0,
        has_evidence=False,
    )
    assert gate.status is GateStatus.UNAVAILABLE


def test_a_breach_fails_even_without_a_full_evidence_window() -> None:
    """Observing a violation is itself the evidence; it disqualifies either way."""
    gate = CeilingGate(
        key="violations", label="invariant violations", observed=2.0, limit=0.0,
        has_evidence=False,
    )
    assert gate.status is GateStatus.FAILED


# ---------------------------------------------------------------------------
# Binding constraint
# ---------------------------------------------------------------------------


def test_a_failed_gate_outranks_everything() -> None:
    gates: list[Gate] = [
        AccrualGate(key="a", label="a", observed=1.0, required=10.0, daily_rate=1.0),
        AccrualGate(key="b", label="b", observed=1.0, required=10.0, daily_rate=None),
        CeilingGate(key="c", label="c", observed=1.0, limit=0.0),
    ]
    binding = binding_gate(gates)
    assert binding is not None and binding.key == "c"


def test_unavailable_outranks_stalled_and_accruing() -> None:
    gates: list[Gate] = [
        AccrualGate(key="accruing", label="a", observed=1.0, required=10.0, daily_rate=1.0),
        AccrualGate(key="stalled", label="s", observed=1.0, required=10.0, daily_rate=None),
        AccrualGate(
            key="unavailable", label="u", observed=0.0, required=10.0,
            daily_rate=None, has_evidence=False,
        ),
    ]
    binding = binding_gate(gates)
    assert binding is not None and binding.key == "unavailable"


def test_among_projectable_gates_the_latest_binds() -> None:
    gates: list[Gate] = [
        AccrualGate(key="fast", label="f", observed=9.0, required=10.0, daily_rate=1.0),
        AccrualGate(key="slow", label="s", observed=1.0, required=100.0, daily_rate=1.0),
    ]
    binding = binding_gate(gates)
    assert binding is not None and binding.key == "slow"


def test_all_gates_passing_has_no_binding_constraint() -> None:
    gates: list[Gate] = [
        AccrualGate(key="a", label="a", observed=10.0, required=10.0, daily_rate=1.0),
        CeilingGate(key="c", label="c", observed=0.0, limit=0.0),
    ]
    assert binding_gate(gates) is None


# ---------------------------------------------------------------------------
# Accrual rate estimation
# ---------------------------------------------------------------------------


def test_daily_rate_uses_complete_days_only() -> None:
    samples = [
        AccrualSample(TODAY - timedelta(days=3), 0.0),
        AccrualSample(TODAY - timedelta(days=1), 20.0),
        AccrualSample(TODAY, 25.0),  # today is still accruing; must be ignored
    ]
    assert estimate_daily_rate(samples, today=TODAY) == 10.0


def test_a_flat_series_yields_no_rate_rather_than_zero() -> None:
    samples = [
        AccrualSample(TODAY - timedelta(days=5), 42.0),
        AccrualSample(TODAY - timedelta(days=1), 42.0),
    ]
    assert estimate_daily_rate(samples, today=TODAY) is None


def test_a_single_sample_yields_no_rate() -> None:
    assert estimate_daily_rate([AccrualSample(TODAY - timedelta(days=1), 5.0)], today=TODAY) is None


def test_an_empty_series_yields_no_rate() -> None:
    assert estimate_daily_rate([], today=TODAY) is None


def test_a_nonpositive_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one day"):
        estimate_daily_rate([], today=TODAY, window_days=0)
