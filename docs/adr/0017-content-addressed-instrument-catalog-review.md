# 17. Instrument catalogs are content-addressed and reviewed by exact hash

- Status: Accepted
- Date: 2026-08-11
- Implements: Phase 2's versioned, fail-closed instrument specification
- Related: [ADR-0003](0003-binance-schemas-synthetic-until-recorded.md),
  [ADR-0007](0007-fail-closed-instrument-lifecycle-and-freshness.md),
  [ADR-0009](0009-liquidation-distance-invariant.md),
  [ADR-0016](0016-instrument-universe-and-funding-cadence.md)

## Context

The liquidation-distance invariant depends on three venue responses that change
independently: `exchangeInfo` filters, `fundingInfo` schedules and caps, and the
authenticated account-specific `leverageBracket` tiers. Overwriting yesterday's
values with today's makes a tier change invisible after the fact. Continuing to
use yesterday's approved values after observing a change is worse: the system
would knowingly size against stale venue rules.

Raw response hashes are not catalog versions. `exchangeInfo` contains a server
timestamp, and transport-level bytes can change without any sizing fact changing.
A raw-byte hash would therefore create false versions; a hand-selected subset
without canonicalization could miss a real one.

There is also no safe default for incomplete joins. Binance documents
`fundingInfo` as the set of symbols whose funding settings were adjusted. A
symbol absent from that response cannot be assigned an eight-hour cadence merely
because eight hours is a venue default elsewhere: ADR-0016 says the schedule is
per symbol and venue-observed. The same applies to a missing filter or margin
schedule.

## Decision

1. `sync-instruments` retains all three raw responses, maps them to exact
   `Decimal` domain values, and constructs one canonical JSON catalog. Decimal
   values are strings, instruments and tiers are ordered, JSON keys are sorted,
   and transport metadata is excluded. SHA-256 of those bytes is the catalog
   identity.
2. Every carry candidate is accounted for exactly once: either as a complete
   specification or as an explicit exclusion with reason codes. No partially
   populated specification exists. A catalog with zero complete specifications
   is rejected entirely.
3. Catalog versions are immutable and deduplicated by
   `(environment, content_sha256)`. Every sync still appends an observation that
   links the version to the exact retained source objects.
4. Human review is a separate append-only `APPROVE` or `REJECT` event tied to the
   full current hash. Reviews never mutate a version row.
5. The latest observation is authoritative. A new hash starts
   `PENDING_REVIEW`, even when the immediately previous hash was approved. There
   is deliberately no fallback to an older approved catalog.
6. `instrument-review` refuses a stale hash. The operator must name the exact
   current SHA-256, actor, and reason, making approval an explicit decision rather
   than a side effect of synchronization.

## Consequences

- Any filter, funding, universe, or maintenance-tier change becomes visible and
  blocks sizing until reviewed.
- Repeated identical syncs append provenance without multiplying catalog
  versions.
- Phase 5 can load one atomic catalog and know every value came from the same
  observed join, instead of independently querying tables that may represent
  different venue moments.
- Catalog JSON duplicates normalized facts that a later analytical schema may
  also expose relationally. That duplication is intentional: this artifact is
  the immutable input to risk decisions, not a mutable convenience view.
- The first authenticated production capture was completed on 2026-08-11. Its
  signing, bracket shape, and recorded findings are reconciled in
  [ADR-0018](0018-authenticated-leverage-bracket-reconciliation.md).
