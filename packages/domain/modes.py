"""Trading operating modes and the safety state machine that governs them.

Ported verbatim from the sibling ``automated-trading-system`` repo: the mode
ladder is venue-neutral and carries no prediction-market assumptions.

Every execution request must identify its operating mode, and the system must
never jump straight from research/backtesting to unrestricted live trading. This
module is the single source of truth for:

  * which modes exist,
  * what each mode permits (originating orders, submitting *real* orders),
  * and which mode-to-mode transitions are legal.

The rules are intentionally deterministic and framework-free so they can be unit-
and property-tested exhaustively, and so no model or strategy can widen them.

Transition rules
----------------
The non-HALTED modes form a safety ladder, from least to most permissive::

    RESEARCH < BACKTEST < PAPER < SHADOW < SUPERVISED_LIVE < AUTONOMOUS_LIMITED

From any ladder mode you may:
  * **advance exactly one step** up the ladder (no skipping — you cannot go from
    BACKTEST to SUPERVISED_LIVE);
  * **drop to any lower ladder mode** (de-risking is always allowed);
  * **go to HALTED** (an emergency stop is always allowed).

From ``HALTED`` you may resume only to a **non-live** ladder mode (RESEARCH
through SHADOW). Re-entering a live mode after a halt must go back through the
ladder step by step, so the system can never silently pop back into live trading.
"""

from __future__ import annotations

from enum import StrEnum

from domain.errors import InvalidModeTransition


class TradingMode(StrEnum):
    """The operating mode of the platform (or of a single execution request)."""

    RESEARCH = "RESEARCH"
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    SUPERVISED_LIVE = "SUPERVISED_LIVE"
    AUTONOMOUS_LIMITED = "AUTONOMOUS_LIMITED"
    HALTED = "HALTED"


# The safety ladder, least to most permissive. HALTED is deliberately not on it.
_LADDER: tuple[TradingMode, ...] = (
    TradingMode.RESEARCH,
    TradingMode.BACKTEST,
    TradingMode.PAPER,
    TradingMode.SHADOW,
    TradingMode.SUPERVISED_LIVE,
    TradingMode.AUTONOMOUS_LIMITED,
)

_LADDER_INDEX: dict[TradingMode, int] = {mode: i for i, mode in enumerate(_LADDER)}

# Modes in which orders are submitted to a real venue with real money.
_LIVE_MODES: frozenset[TradingMode] = frozenset(
    {TradingMode.SUPERVISED_LIVE, TradingMode.AUTONOMOUS_LIMITED}
)

# Modes in which the system originates orders at all (real or simulated).
_ORDER_ORIGINATING_MODES: frozenset[TradingMode] = frozenset(
    {
        TradingMode.PAPER,
        TradingMode.SHADOW,
        TradingMode.SUPERVISED_LIVE,
        TradingMode.AUTONOMOUS_LIMITED,
    }
)


def is_live(mode: TradingMode) -> bool:
    """Whether the mode submits real orders with real capital."""
    return mode in _LIVE_MODES


def permits_new_orders(mode: TradingMode) -> bool:
    """Whether the mode may originate new orders (real or simulated).

    HALTED, RESEARCH, and BACKTEST never originate live/simulated orders against
    live data. (A halt still permits cancellation, unwinding, and reconciliation;
    that is an execution-engine concern, not a mode concern.)
    """
    return mode in _ORDER_ORIGINATING_MODES


def submits_real_orders(mode: TradingMode) -> bool:
    """Whether the mode sends orders to a real venue with real money."""
    return mode in _LIVE_MODES


def can_transition(from_mode: TradingMode, to_mode: TradingMode) -> bool:
    """Return whether moving from ``from_mode`` to ``to_mode`` is permitted."""
    if from_mode == to_mode:
        return False

    # An emergency stop is always available from any mode.
    if to_mode == TradingMode.HALTED:
        return True

    # Resuming from a halt: only to a non-live ladder mode; live re-entry must go
    # back through the ladder explicitly.
    if from_mode == TradingMode.HALTED:
        return to_mode in _LADDER and to_mode not in _LIVE_MODES

    # Both are ladder modes from here on.
    from_idx = _LADDER_INDEX[from_mode]
    to_idx = _LADDER_INDEX[to_mode]

    if to_idx < from_idx:
        return True  # de-risking to any safer mode is always allowed
    return to_idx == from_idx + 1  # advancing: exactly one step, no skipping


def assert_transition(from_mode: TradingMode, to_mode: TradingMode) -> None:
    """Raise :class:`InvalidModeTransition` if the transition is not permitted."""
    if can_transition(from_mode, to_mode):
        return

    if from_mode == to_mode:
        reason = "mode is unchanged"
    elif from_mode == TradingMode.HALTED and to_mode in _LIVE_MODES:
        reason = "cannot resume from HALTED directly into a live mode; re-enter via the ladder"
    elif to_mode in _LADDER and from_mode in _LADDER:
        reason = "cannot skip ladder steps; advance one mode at a time"
    else:
        reason = "no such transition"
    raise InvalidModeTransition(from_mode, to_mode, reason)


def next_mode_up(mode: TradingMode) -> TradingMode | None:
    """The next, more-permissive mode on the ladder, or ``None`` at the top/off-ladder."""
    idx = _LADDER_INDEX.get(mode)
    if idx is None or idx + 1 >= len(_LADDER):
        return None
    return _LADDER[idx + 1]
