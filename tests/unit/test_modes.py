"""The mode ladder is the outermost safety boundary; it is tested exhaustively.

Every ordered pair of modes is enumerated rather than sampled — there are only 49
of them, and "no path from backtest straight to live" is a claim that deserves a
proof rather than a spot check.
"""

from __future__ import annotations

import itertools

import pytest

from domain.errors import InvalidModeTransition
from domain.modes import (
    TradingMode,
    assert_transition,
    can_transition,
    is_live,
    next_mode_up,
    permits_new_orders,
    submits_real_orders,
)

LADDER = [
    TradingMode.RESEARCH,
    TradingMode.BACKTEST,
    TradingMode.PAPER,
    TradingMode.SHADOW,
    TradingMode.SUPERVISED_LIVE,
    TradingMode.AUTONOMOUS_LIMITED,
]


def test_only_the_two_live_modes_submit_real_orders() -> None:
    live = {mode for mode in TradingMode if is_live(mode)}
    assert live == {TradingMode.SUPERVISED_LIVE, TradingMode.AUTONOMOUS_LIMITED}
    assert all(submits_real_orders(mode) is is_live(mode) for mode in TradingMode)


def test_research_backtest_and_halted_never_originate_orders() -> None:
    for mode in (TradingMode.RESEARCH, TradingMode.BACKTEST, TradingMode.HALTED):
        assert not permits_new_orders(mode)


def test_every_live_mode_also_permits_origination() -> None:
    for mode in TradingMode:
        if is_live(mode):
            assert permits_new_orders(mode)


@pytest.mark.parametrize("mode", list(TradingMode))
def test_a_mode_never_transitions_to_itself(mode: TradingMode) -> None:
    assert not can_transition(mode, mode)


@pytest.mark.parametrize("mode", list(TradingMode))
def test_halt_is_always_reachable(mode: TradingMode) -> None:
    if mode is not TradingMode.HALTED:
        assert can_transition(mode, TradingMode.HALTED)


@pytest.mark.parametrize(("lower", "upper"), list(itertools.combinations(LADDER, 2)))
def test_advancing_more_than_one_step_is_refused(lower: TradingMode, upper: TradingMode) -> None:
    distance = LADDER.index(upper) - LADDER.index(lower)
    assert can_transition(lower, upper) is (distance == 1)


@pytest.mark.parametrize(("lower", "upper"), list(itertools.combinations(LADDER, 2)))
def test_de_risking_to_any_safer_mode_is_always_allowed(
    lower: TradingMode, upper: TradingMode
) -> None:
    assert can_transition(upper, lower)


def test_halt_never_resumes_directly_into_a_live_mode() -> None:
    for mode in LADDER:
        expected = not is_live(mode)
        assert can_transition(TradingMode.HALTED, mode) is expected


def test_there_is_no_path_from_backtest_to_a_live_mode_in_one_step() -> None:
    assert not can_transition(TradingMode.BACKTEST, TradingMode.SUPERVISED_LIVE)
    assert not can_transition(TradingMode.BACKTEST, TradingMode.AUTONOMOUS_LIMITED)


def test_assert_transition_explains_why_it_refused() -> None:
    with pytest.raises(InvalidModeTransition, match="skip ladder steps"):
        assert_transition(TradingMode.BACKTEST, TradingMode.SUPERVISED_LIVE)
    with pytest.raises(InvalidModeTransition, match="re-enter via the ladder"):
        assert_transition(TradingMode.HALTED, TradingMode.SUPERVISED_LIVE)
    with pytest.raises(InvalidModeTransition, match="unchanged"):
        assert_transition(TradingMode.PAPER, TradingMode.PAPER)


def test_assert_transition_is_silent_on_legal_moves() -> None:
    assert_transition(TradingMode.PAPER, TradingMode.SHADOW)
    assert_transition(TradingMode.SHADOW, TradingMode.RESEARCH)
    assert_transition(TradingMode.AUTONOMOUS_LIMITED, TradingMode.HALTED)


def test_next_mode_up_walks_the_ladder_and_stops_at_the_top() -> None:
    assert next_mode_up(TradingMode.RESEARCH) is TradingMode.BACKTEST
    assert next_mode_up(TradingMode.SUPERVISED_LIVE) is TradingMode.AUTONOMOUS_LIMITED
    assert next_mode_up(TradingMode.AUTONOMOUS_LIMITED) is None
    assert next_mode_up(TradingMode.HALTED) is None


def test_reaching_live_from_research_takes_the_full_ladder() -> None:
    """Walking up one legal step at a time must visit every intermediate mode."""
    mode = TradingMode.RESEARCH
    visited = [mode]
    while (nxt := next_mode_up(mode)) is not None:
        assert can_transition(mode, nxt)
        mode = nxt
        visited.append(mode)
    assert visited == LADDER
