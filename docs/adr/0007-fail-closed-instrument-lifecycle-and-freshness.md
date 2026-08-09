# 7. Fail-closed instrument lifecycle and tiered input freshness

- Status: Accepted
- Date: 2026-08-09
- Mirrors: sibling ADR-0007, with single-tier freshness replaced by tiers
- Related: [ADR-0005](0005-eight-hour-decision-cadence.md),
  [ADR-0014](0014-crypto-safety-context.md)

## Context

The sibling repo has **one** freshness tolerance — 90 minutes on a market quote —
because a decision there reads essentially one time-varying input against a
daily-settling market. Ninety minutes is safe in that domain.

A funding-carry decision reads at least three independently-aged inputs, and they
refresh on cadences that differ by four orders of magnitude:

| Input | Refresh cadence | What a stale value does |
|---|---|---|
| Mark / index price | continuous | Mis-sizes the hedge and mis-computes liquidation distance |
| Funding rate | per interval (commonly 8h) | Mis-values the entire carry |
| Account / margin state | on request | Sizes against margin that is no longer there |

A single tolerance would have to be as tight as the tightest input, which would
reject every decision, or as loose as the loosest, which is how a 30-minute-old
mark price gets used to compute a liquidation distance.

## Decision

**Freshness is tiered, per input, and every tolerance is configured and
versioned.** Starting points, all to be validated against recorded latency:

| Input | Tolerance | Rationale |
|---|---|---|
| Mark/index price for a **decision** | ≤ 60 s | Sizing and liquidation distance depend on it |
| Price for **order pricing** | ≤ 5 s, with a mandatory pre-submit refresh | The submitted price is the executed price |
| Funding rate | ≤ 1 funding interval | Older than that and the current interval is unknown |
| Account / margin state | ≤ 60 s | Margin moves with the market |

Every check **fails closed**: missing, stale, ambiguous, or future-dated input
blocks the decision rather than falling back to a default. Future-dating is
checked separately from staleness, because they are different bugs — stale data
is a collection failure, future-dated data is a clock failure — and a report that
conflates them sends the operator to the wrong place.

Instrument lifecycle is equally fail-closed. A decision requires:

- `instrument_status == "TRADING"` from `exchangeInfo`; any other value blocks;
- `instrument_spec_status == "APPROVED"` — our own review of the symbol's
  filters (tick size, step size, minimum notional) and its maintenance-margin
  tiers. This is the analogue of the sibling's contract-approval gate, and it
  guards the same class of failure: the place where a wrong assumption silently
  produces a wrong position size.

Both live in `domain/safety.py` and are evaluated on every decision, never cached
past the tolerance.

## Consequences

- The safety report names *which* input was stale, which is the difference
  between a five-minute fix and an afternoon.
- More checks means more ways to block, and early paper operation will block
  often. That is the gate working, not a reason to widen a tolerance.
- The order-pricing tier has no code yet — there is no order path — but it is
  recorded now so the number is not invented under deadline later.
- Every tolerance is a starting guess. Validating them against recorded latency
  is a Phase-1 task, and changing one is an ADR-worthy decision.
