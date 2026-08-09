# 11. Decimal end to end, quantized to symbol filters, rounding down when capping

- Status: Accepted
- Date: 2026-08-09
- Mirrors: sibling ADR-0002's integer-cents discipline, strengthened
- Related: [ADR-0009](0009-liquidation-distance-invariant.md)

## Context

The sibling repo represents prices as integer **cents** and sizes as integer
**hundredths of a contract**, and warns loudly against floats. Integers work
there because the domain has exactly two decimal places and one of them is
legally defined.

Crypto has no such luxury. Quantities carry 8+ decimals, and every symbol has its
own `tickSize`, `stepSize`, and `minNotional` from `exchangeInfo`. Integer
arithmetic in a fixed unit does not generalize across symbols.

Floats fail here in a specific, quiet way: `Decimal(0.1)` is
`0.1000000000000000055511151231257827021181583404541015625`. Binance returns
numbers as JSON **strings** precisely so this is avoidable — and `json.loads`
turns them into floats by default, discarding that care in one line.

The asymmetry that makes this a safety concern rather than a style preference:

- A rounding error that pushes an order **below** `minNotional` is an annoyance.
  The venue rejects it, the run logs a failure, someone looks.
- A rounding error that pushes size **above** a cap is a larger position than the
  risk engine approved. On a leveraged short, that is a liquidation.

## Decision

1. **`Decimal` end to end.** No float anywhere in the money or size path.
2. **`parse_decimal()` refuses `float` input outright**, rather than converting
   it. That is the one boundary where exactness is still recoverable, so it is
   enforced rather than documented. Non-finite values (`NaN`, `Infinity`) are
   refused too — they arrive from malformed payloads, and a `NaN` size compares
   `False` against every cap, so it sails through a risk check that looks correct.
3. **Quantize to the symbol's own filters**, read from `exchangeInfo` and
   versioned, never hardcoded.
4. **Always round *down* when capping.** `quantize_down` is the default;
   `quantize_up` exists only for clearing floors (`minNotional`, minimum
   quantity) and must never be applied to a value a cap constrains from above.
5. Quantization uses `ROUND_FLOOR`/`ROUND_CEILING`, **not** `Decimal`'s `//`,
   which truncates toward zero and therefore rounds negative values the wrong
   way.
6. Results carry the step's exponent, so `0.1` quantized by `0.01` renders as
   `"0.10"` — the venue compares string forms.
7. **Every threshold is expressed in basis points.** The sibling's 8-cent spread
   gate is 800 bps of a $1 binary; a BTC perp spread is ~1 bp. A cent-shaped
   threshold copied across is not merely wrong, it is wrong by three orders of
   magnitude.

All of this lives in `packages/domain/precision.py` and is property-tested. The
invariant that matters most — `quantize_down(v, s) <= v` for every value and step
— is checked with Hypothesis rather than examples.

## Consequences

- Slightly more ceremony at every boundary, in exchange for arithmetic that is
  exact by construction.
- Wire parsing must avoid `json.loads`' default float coercion for numeric
  fields. That is a Phase-1 constraint on the Binance client.
- Any future numeric helper belongs in `precision.py` rather than at a call site,
  so the rounding direction is a reviewable decision in one file.
