"""Decimal discipline and symbol-filter quantization (ADR-0011).

The sibling ``automated-trading-system`` represents money as integer cents and
sizes as integer hundredths of a contract, and warns loudly against floats. The
same rule applies here and is harder to keep: crypto quantities carry 8+ decimals
and every symbol has its own tick size, step size, and minimum notional.

The asymmetry that makes this a safety concern rather than a style preference:
a rounding error that pushes an order *below* ``minNotional`` is an annoyance —
the venue rejects it and the run logs a failure. A rounding error that pushes
size *above* a cap is a larger position than the risk engine approved, and on a
leveraged short that is a liquidation. **So capping always rounds down.**

Everything here is pure, exact, and float-free. :func:`parse_decimal` refuses
``float`` input outright, because ``Decimal(0.1)`` is
``0.1000000000000000055511151231257827021181583404541015625`` and no amount of
downstream care recovers from that.
"""

from __future__ import annotations

import decimal
from decimal import Decimal

#: Working precision for intermediate division. Generous enough that quantizing
#: a whole-account notional to a satoshi-scale step never loses a digit.
_CONTEXT = decimal.Context(prec=60, traps=[decimal.InvalidOperation, decimal.DivisionByZero])

#: One basis point as a fraction. Every threshold in this project is expressed in
#: basis points (§4 of the README): the predecessor's 8-cent spread gate is 8% of
#: a $1 binary, while a BTC perp spread is ~1bp, so cent-shaped numbers here are
#: not merely wrong but wrong by three orders of magnitude.
BPS = Decimal("0.0001")


class PrecisionError(ValueError):
    """Raised when a value cannot be represented or quantized exactly."""


def parse_decimal(raw: str | int | Decimal) -> Decimal:
    """Build a :class:`~decimal.Decimal` from wire or config input.

    ``float`` is rejected rather than converted: accepting it would silently
    admit binary-representation error at the one boundary where this project
    still has a chance to keep its arithmetic exact. Binance returns numbers as
    JSON *strings* precisely so this is possible — parse them as strings, never
    via ``json.loads`` into a float.

    NaN and infinity are rejected too. They arrive from malformed payloads, and a
    non-finite size compares ``False`` against every cap, so it would sail through
    a risk check that looks correct.
    """
    if isinstance(raw, float):
        raise PrecisionError(
            "refusing to build a Decimal from a float; pass the string form "
            "(Binance returns numbers as strings for exactly this reason)"
        )
    try:
        value = Decimal(raw)
    except (decimal.InvalidOperation, ValueError, TypeError) as exc:
        raise PrecisionError(f"not a valid decimal: {raw!r}") from exc
    if not value.is_finite():
        raise PrecisionError(f"decimal must be finite, got {raw!r}")
    return value


def _require_positive_step(step: Decimal) -> None:
    if not step.is_finite() or step <= 0:
        raise PrecisionError(f"quantization step must be finite and positive, got {step}")


def quantize_down(value: Decimal, step: Decimal) -> Decimal:
    """Largest multiple of ``step`` that is ``<= value``.

    This is the default for anything that must respect a cap: an order size
    against a risk limit, a notional against a maximum, a quantity against a
    ``LOT_SIZE`` filter.
    """
    return _quantize(value, step, decimal.ROUND_FLOOR)


def quantize_up(value: Decimal, step: Decimal) -> Decimal:
    """Smallest multiple of ``step`` that is ``>= value``.

    Correct only for *floors* — clearing ``minNotional``, meeting a minimum
    quantity. Never use it on a value that a cap constrains from above.
    """
    return _quantize(value, step, decimal.ROUND_CEILING)


def _quantize(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    if not value.is_finite():
        raise PrecisionError(f"cannot quantize a non-finite value: {value}")
    _require_positive_step(step)
    # ROUND_FLOOR/ROUND_CEILING rather than Decimal's `//`, which truncates
    # toward zero and would therefore round a negative value the wrong way.
    multiples = _CONTEXT.divide(value, step).to_integral_value(rounding=rounding)
    result = _CONTEXT.multiply(multiples, step)
    # Normalize the exponent to the step's, so 0.1 quantized by 0.01 prints as
    # "0.10" and not "0.1" — the venue compares string forms.
    return result.quantize(step, context=_CONTEXT)


def is_multiple_of(value: Decimal, step: Decimal) -> bool:
    """Whether ``value`` sits exactly on the ``step`` grid."""
    _require_positive_step(step)
    if not value.is_finite():
        return False
    return _CONTEXT.remainder(value, step) == 0


def to_bps(fraction: Decimal) -> Decimal:
    """Convert a fraction (``0.0001``) to basis points (``1``)."""
    return _CONTEXT.divide(fraction, BPS)


def from_bps(bps: Decimal) -> Decimal:
    """Convert basis points (``1``) to a fraction (``0.0001``)."""
    return _CONTEXT.multiply(bps, BPS)
