# 13. Reconciliation is a first-class package, built before any order path

- Status: Accepted
- Date: 2026-08-09
- **New**; the sibling repo has no equivalent and could afford not to
- Related: [ADR-0009](0009-liquidation-distance-invariant.md),
  [ADR-0014](0014-crypto-safety-context.md)

## Context

The sibling repo defers reconciliation entirely, and that is a defensible choice
there: it has no order path, one leg, no margin, and capital that is never at a
venue. Local ledger drift has nothing to drift against.

Every one of those conditions is false here.

- **Two legs.** A carry position is a short perp and a long spot, in different
  wallets, filled by different order paths, each of which can partially fill,
  reject, or fill at a price other than the one requested.
- **Real margin.** The futures wallet's balance changes without any action from
  us — funding settles, unrealized P&L marks, fees deduct.
- **Continuous financing.** Funding accrues every interval whether the system is
  running or not. A ledger that accrues on decision rather than on schedule
  diverges by construction.

Local ledger drift is the failure mode that ends accounts, and it is
insidious: the system believes it holds a hedged position while actually holding a
naked one, and every subsequent risk calculation is computed from the wrong
book. The liquidation-distance invariant
([ADR-0009](0009-liquidation-distance-invariant.md)) is computed from local
state; if local state is wrong, the invariant is enforced against fiction.

## Decision

1. **`packages/reconciliation/` is a first-class package**, not a later add-on.
   It compares venue account state — balances, positions, margin, funding
   payments received — against the local ledger.
2. **A discrepancy latches a halt.** `ReconciliationStatus.DISCREPANCY` produces
   an automatic `ACCOUNT`-scoped kill switch through the same append-only control
   log as every other halt, clearable only by an audited event.
3. **`UNKNOWN` blocks.** This is the part that is easy to get wrong. "Nobody has
   checked" is not a neutral state — it is precisely the state in which drift
   goes unnoticed — so it blocks the decision. It does **not** latch a halt,
   because not having looked is not evidence of a problem; only a proven
   discrepancy latches.
4. **Reconciliation exists before the first order is ever submitted.** The order
   path (Phase 8) does not begin until reconciliation runs against a read-only
   real account in Phase 7. Building the detector after the thing it detects is
   how drift goes unobserved for exactly as long as it takes to matter.
5. **Unexplained discrepancies are a permanent promotion failure**, not a
   statistic ([ADR-0012](0012-prospective-only-promotion-gates.md)).

## Status of implementation

Phase 0 implements the *gate*: `LEDGER_RECONCILED` in `domain/safety.py`, with
`UNKNOWN` blocking and `DISCREPANCY` latching, plus its tests. The package that
performs the comparison lands in Phase 7, against a read-only real account.

Stating the gate first means the reconciliation implementation is written to
satisfy a standard that already exists, rather than the standard being written to
match whatever the implementation happened to do.

## Consequences

- Early paper operation blocks on `LEDGER_RECONCILED: UNKNOWN` until Phase 7.
  That is correct and expected, and it should not be worked around by defaulting
  the status to `RECONCILED`.
- Reconciliation needs venue account reads, so it needs API keys earlier than a
  pure market-data system would — read-only and IP-restricted.
- The reconciliation cadence becomes an operational parameter: too rare and drift
  accumulates, too frequent and it burns rate-limit weight.
