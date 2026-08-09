# 12. Prospective-only promotion gates; count gates do not transfer

- Status: Accepted
- Date: 2026-08-09
- **Diverges from** sibling `domain/promotion.py` thresholds
- Related: [ADR-0004](0004-funding-carry-edge-thesis.md),
  [ADR-0009](0009-liquidation-distance-invariant.md)

## Context

The sibling repo gates promotion on counts: 500 resolved predictions, 500 matched
forecasts, 60 outcome days, 80% baseline coverage. Those work as safety
mechanisms **because that project's history accrues one day per day.** They are
time gates wearing count costumes, and an entire ADR there (0025) exists solely to
work around how slowly they fill.

Here, `data.binance.vision` publishes years of history for free. Any count gate
can be satisfied in an afternoon by backtesting the archive. **A gate you can
clear in an afternoon is not a gate.**

This is the single most dangerous thing to copy from the sibling repo, because
copying it *looks* conservative — the numbers are large, the thresholds are
strict, the code is battle-tested — while providing no safety at all.

There is a second, subtler trap. The sibling has only *accrual* gates, so its
status rule is `observed >= required`. Two of this project's gates are the
opposite shape: reconciliation discrepancies and liquidation-invariant violations
must stay at **zero**. Evaluating those with an accrual rule reports five
violations as a comfortable PASS against a threshold of zero.

And a third, found by running `promotion-status` on the empty Phase-0 database:
with a naive construction, *"net carry 0 bps ≥ threshold 0"* and *"0 liquidation
violations ≤ limit 0"* both render **PASS** on a system that has never traded.

## Decision

### Gates are prospective wall-clock time on unseen data

| Gate | Threshold | Why |
|---|---|---|
| Prospective paper days | ≥ 90 **consecutive** | Forward-only; a backtest cannot contribute |
| Funding settlements observed prospectively | ≥ 250 | ~3 months at 3/day |
| Net carry vs USDT-hold benchmark | **strictly** positive | Kill criterion 1, as a gate |
| Funding-persistence Brier skill vs naive | **strictly** positive | Kill criterion 2, as a gate |
| Unexplained reconciliation discrepancies | 0, ever | The account-ending failure mode |
| Liquidation-distance invariant violations | 0, ever | Not a statistic; a hard stop |

"Consecutive" is strict: one missed day resets the run. The thing being
demonstrated is that the system runs unattended without breaking, and 90 scattered
days over a year does not demonstrate it.

### Eligibility is structural, not remembered

`EvidenceSource` names where a day came from, and only `PAPER_PROSPECTIVE`
counts. `BACKTEST`, `TESTNET`, and `SYNTHETIC` are recorded — they are genuinely
useful — but `prospective_days()` filters them out **before** measuring the run,
so an interleaved backtest day cannot bridge a gap in paper operation. The rule
lives in the domain and is tested there, rather than depending on every call site
remembering to filter.

### Three gate kinds, three status rules

- `AccrualGate` — cleared by climbing to a threshold. `strict=True` requires
  `observed > required`, for gates phrased as *beating* something: tying with the
  naive baseline means the model added nothing.
- `CeilingGate` — cleared by staying at or below a limit. A breach is
  `GateStatus.FAILED`, not `STALLED`: a violation that already happened cannot be
  undone by waiting. Clearing it takes a root-cause fix and a fresh campaign,
  which is a human decision.
- **`has_evidence`** on both. A gate with nothing behind it is `UNAVAILABLE`,
  never `PASS`. Zero violations across zero opportunities to violate is not
  evidence of safety, it is the absence of a test. (A *breach* still reports
  FAILED without a full window — observing a violation is itself evidence.)

`binding_gate()` orders by severity: FAILED, then UNAVAILABLE, then STALLED, then
the latest projection.

### Projections remain planning aids

A gate is cleared by its observed value reaching the threshold, never by a
projected date arriving. Wall-clock gates project the exact remainder (an
unbroken campaign accrues one day per day), which is more honest than
extrapolating a measured rate.

## Consequences

- Promotion takes **at least 90 days of wall-clock paper operation**, no matter
  how much archive is backfilled. That is the intended cost.
- Backtests remain enormously valuable — for rejecting ideas cheaply, sizing the
  opportunity, and finding bugs. They are simply not promotion evidence.
- Testnet fills carry real order semantics but not production liquidity, so they
  are marked ineligible in the database, exactly as the sibling marks non-production
  fills.
- On the Phase-0 database every gate reads UNAVAILABLE, which is the correct
  reading and is visible from commit one.
