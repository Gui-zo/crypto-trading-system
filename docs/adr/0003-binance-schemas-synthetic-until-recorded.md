# 3. Binance wire schemas are synthetic until validated against real responses

- Status: Accepted
- Date: 2026-08-09
- Mirrors: sibling ADR-0003
- Related: [ADR-0002](0002-simplified-local-first-stack.md)

## Context

**Every Binance specific written down in this repository — endpoints, funding
intervals, fee tiers, rate-limit weights, maintenance-margin brackets, field
names — comes from documentation and memory, not from a recorded response.**
That includes the founding README. Nothing here has met the live API yet.

Specific things not yet verified:

- exact field names and types on `premiumIndex`, `fundingRate`, `exchangeInfo`,
  and `leverageBracket` responses;
- whether a given symbol's funding interval is 8h or 4h (it is per-symbol, and
  assuming 8h everywhere is a silent accrual error);
- the exact bytes signed for HMAC-SHA256 authentication and the query-string
  ordering that signature covers;
- rate-limit weights per endpoint and how the used-weight headers are named;
- the precise base URLs for testnet vs production, spot vs USDⓈ-M;
- maintenance-margin tier boundaries, which change.

The sibling repo made exactly this bet against Kalshi and recorded the outcome in
its ADR-0005: documentation and reality differed in ways that mattered.

## Decision

Proceed with synthetic, schema-faithful fixtures, but contain the risk. From the
first commit, not as a later hardening pass:

1. **Tolerant parsing.** Wire models allow extra fields and make most fields
   optional, so an unexpected or missing field never crashes ingestion.
2. **Retain raw verbatim.** Every response is stored byte-for-byte in the
   `RawStore` *before* mapping, so parser corrections replay against history and
   nothing is lost to a schema guess. Keys carry the venue environment
   ([ADR-0010](0010-venue-environment-scoping.md)).
3. **Single points of correction.** Base URLs, the signing routine, and status
   mapping each live in exactly one file, so fixing them after first contact is a
   small, localized change.
4. **Label synthetic fixtures as synthetic**, and replace them with recorded
   responses at first contact, diffing the two.
5. **Read the numbers from the API, never from a document.** Fee schedules,
   rate-limit weights, funding intervals, and maintenance-margin tiers are all
   fetched and versioned. A constant transcribed from documentation into source
   is a bug waiting for a schedule change.

## Consequences

- Development is unblocked without credentials; the whole read path is testable
  offline.
- There is a **mandatory** follow-up the first time we connect: capture real
  responses, diff them against these schemas, replace synthetic fixtures, and
  correct the signing routine if the API rejects our signature. Phase 1 is not
  done until that reconciliation happens, however green the tests are.
- The findings of that reconciliation get their own ADR, mirroring the sibling's
  ADR-0005. Expect it to be load-bearing.
- Because raw payloads are retained, this reconciliation carries no data-loss
  risk.
