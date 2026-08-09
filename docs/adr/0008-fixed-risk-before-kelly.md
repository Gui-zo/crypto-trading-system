# 8. Fixed risk before Kelly — and why Kelly is *worse* here than on a binary

- Status: Accepted
- Date: 2026-08-09
- Mirrors: sibling ADR-0008, with a strictly stronger argument
- Related: [ADR-0009](0009-liquidation-distance-invariant.md)

## Context

The sibling repo sizes with a fixed fractional risk unit and refuses Kelly until
calibration is demonstrated. Its reasoning: Kelly is optimal *given* correct
probabilities, and an uncalibrated model supplies incorrect ones, so Kelly
amplifies model error into position size.

That argument carries here unchanged. But there is a second argument that applies
**only** here, and it is the stronger one.

On a binary contract, Kelly's failure mode is bounded. Over-betting a binary
loses the premium — painful, survivable, and the loss is capped by the
instrument's own structure regardless of how wrong the sizing was.

On a leveraged short perpetual there is no such structure. Over-sizing does not
merely lose more; it moves the **liquidation price** closer to the current price.
Past a threshold the position is not a bad bet, it is a bet that the exchange
closes for you, at the worst moment, at a price you did not choose. And the
sibling's `kelly_fraction()` is binary-odds Kelly — the arithmetic itself is
inapplicable, not merely the risk appetite.

There is a third wrinkle: a delta-neutral carry has no "win probability" in the
Kelly sense at all. The forecast is `P(funding persists)`, which maps to a cash
flow, not to a payoff on a directional bet. Applying Kelly would require
inventing odds that the position does not have.

## Decision

**Fixed, configured risk units only.** No Kelly, no fractional Kelly, no
volatility-targeting that behaves like Kelly, until:

1. the funding-persistence model has demonstrated calibration on prospective
   evidence ([ADR-0012](0012-prospective-only-promotion-gates.md)), **and**
2. the liquidation-distance invariant
   ([ADR-0009](0009-liquidation-distance-invariant.md)) is property-tested and
   has held across a full paper campaign, **and**
3. a specific superseding ADR argues the case for the sizing method actually
   proposed, on this instrument's payoff structure.

The sibling's `kelly_fraction()` is **not ported**. Porting it and leaving it
unused would be worse than not having it: it is binary-odds arithmetic sitting in
a crypto codebase, waiting to be called by someone who assumes it was written for
this domain.

## Consequences

- Sizing is boring, predictable, and testable. That is the goal.
- We give up theoretical growth-optimality we could not have safely realized
  anyway, given uncalibrated probabilities and an unbounded-loss instrument.
- Any future sizing method must be argued against the liquidation invariant
  first, and be shown to respect it, before it is argued on returns.
