# 20. Research backfill, funding-cadence mutability, and superseded quality evidence

- Status: Accepted
- Date: 2026-08-15
- Related: [ADR-0012](0012-prospective-only-promotion-gates.md),
  [ADR-0016](0016-instrument-universe-and-funding-cadence.md),
  [ADR-0017](0017-content-addressed-instrument-catalog-review.md),
  [ADR-0019](0019-checksum-verified-source-aware-market-history.md)

## Context

ADR-0019 built checksum-verified archive ingestion and proved it on one bounded
month of BTCUSDT. Phase 3 could not be called done until an explicit research
universe and range were chosen and ingested. Choosing them is a research
decision, not an implementation one, so it was deferred until the machinery was
trustworthy.

Two constraints shaped the choice. Delta-neutral carry needs a spot leg, so a
perpetual without a Binance spot pair is not tradeable by this strategy and its
funding history is not usable. And the backfill is fail-closed on a missing
archive object: `ArchiveUnavailable` aborts the whole atomic transaction, so a
symbol listed after the range start destroys the run rather than skipping.

## Decision

**Research range: `[2024-08-01, 2026-08-01)`. Research universe: 19 symbols.**

- 15 eight-hourly majors — BTC, ETH, SOL, BNB, XRP, DOGE, ADA, AVAX, LINK, LTC,
  DOT, TRX, NEAR, SUI, BCH.
- 4 four-hourly symbols with equally complete history — ACE, ALICE, ORDI, TIA.

The second group exists because the first is not representative. 415 of the 527
approved specifications are four-hourly and only 110 are eight-hourly; the top
majors sit in the 21% minority. A funding model fit on majors alone would have
no example of the cadence that covers most of the venue.

Archive history is for model fitting and backtesting only. Under ADR-0012 it
counts **zero** toward any promotion gate, so range length was chosen for
regime coverage rather than for volume of evidence.

**Every planned object is probed for existence before ingest.** All 1,368
objects returned 200 before any transaction opened. This is the practical
counterweight to fail-closed abort; it is cheaper than discovering a hole after
twenty minutes of downloads.

**A `BLOCKED` quality assessment stops counting once a later assessment for the
same artifact passes.** The audit row is never edited or deleted — the operator
count simply excludes superseded evidence, and reports it separately.

## Production reconciliation

Ingested on 2026-08-15: **48,905 archive funding settlements** and **665,760
closed 1h candles** (19 symbols × 17,520 hours × spot and USD-M), across 1,381
immutable artifacts. Every symbol is exactly complete in both markets — 17,520
rows each, zero non-1h steps. Re-ingesting inserted zero rows.

### Finding 1 — the funding interval is not constant within a symbol

CLAUDE.md already forbids assuming *the* funding interval because it varies per
symbol. It also varies **per symbol over time**:

| Symbol | Date | Transition |
|---|---|---|
| ACEUSDT | 2025-12-16 | 4h → 1h, reverting to 4h on 2025-12-27 (275 hourly rows) |
| ALICEUSDT | 2026-02-28 | 8h → 1h, settling to 4h from 2026-03-03 (79 hourly rows) |

ALICEUSDT is a complete 8h → 4h migration inside the research range. Any
freshness tolerance, accrual calculation, or backtest must use the interval **in
force at that settlement**, which is the archive's own `funding_interval_hours`
column — never the current catalog value applied retroactively.

### Finding 2 — the venue skips settlements

On **2026-06-24 the 04:00 UTC settlement does not exist** for ACE, ALICE, ORDI,
or TIA. The 00:00 and 08:00 settlements are both present, so the four-hourly
schedule has a genuine hole rather than a shifted boundary.

This was confirmed against the REST funding-history API, which returns the same
four timestamps with the same millisecond jitter and no 04:00 row. Archive and
REST agree independently, so this is venue behaviour and not an archive defect.
A backtest that assumes a settlement exists at every scheduled boundary will
over-accrue carry on that date.

### Finding 3 — archive `calc_time` is not on the settlement boundary

Funding timestamps carry millisecond jitter around the boundary (observed ±5 ms;
BTCUSDT steps range 07:59:59.995 to 08:00:00.005). The published column is the
calculation time, not the idealised boundary. Gap detection therefore needs its
existing ±1 minute tolerance, and no code may test funding timestamps for exact
boundary equality.

### Finding 4 — point-in-time blocks outlive the condition they describe

Backfilling into a range that already held the July 2026 bounded-proof rows made
each newly ingested month block on its forward boundary: the next existing row
was up to two years away. Sixty-six such assessments were written, all BTCUSDT,
all correct at the moment they ran. Re-ingesting BTCUSDT over the completed range
appended fresh `PASS` assessments and inserted zero rows.

The six assessments that remain `BLOCKED` are findings 1 and 2 — true positives
that a human should read. That is the distinction the count now preserves.

## Consequences

- The research dataset under-represents the venue by construction: 19 of 527
  symbols, and only today's survivors. Symbols that paid rich funding and then
  delisted are absent, which flatters any carry backtest built on it.
- Phase 3 exit criteria are met for this universe and range. Widening either is
  a new explicit decision, not an inferred one.
- `blocked_assessments` is now actionable — a non-zero value means unresolved
  evidence — and `superseded_blocked_assessments` preserves the history.
- Cadence mutability is a modelling input, not a nuisance. A symbol moving to
  hourly funding is the venue reacting to extreme funding, which is exactly the
  regime a carry strategy cares about.

## References

- [USD-M funding-rate history](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History)
- [Binance public data README](https://github.com/binance/binance-public-data/blob/master/README.md)
