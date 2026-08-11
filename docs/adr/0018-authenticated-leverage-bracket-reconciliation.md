# 18. Authenticated leverage-bracket first contact matched the schema

- Status: Accepted
- Date: 2026-08-11
- Implements: ADR-0003's authenticated first-contact reconciliation for Phase 2
- Related: [ADR-0003](0003-binance-schemas-synthetic-until-recorded.md),
  [ADR-0009](0009-liquidation-distance-invariant.md),
  [ADR-0015](0015-binance-live-reconciliation-findings.md),
  [ADR-0017](0017-content-addressed-instrument-catalog-review.md)

## Context

The public reconciliation in ADR-0015 established that
`GET /fapi/v1/leverageBracket` rejects unauthenticated requests. Phase 2 then
implemented an HMAC-signed, read-only path and a fail-closed catalog mapper using
documentation and synthetic bracket data. ADR-0003 required a successful
production capture, a schema diff, and replacement of that synthetic fixture
before treating maintenance-margin tiers as verified.

On 2026-08-11, `sync-instruments` called the signed production endpoint through
the normal `SecretProvider` boundary. Binance accepted the signature and API key;
no credential value was printed, retained with the response, or added to the
fixture. The raw response remains in the environment-scoped immutable raw store.

## Recorded findings

1. **The signing implementation was accepted without correction.** The exact
   query bytes signed by `auth.py`, the `timestamp`/`recvWindow` parameters, and
   the `X-MBX-APIKEY` header form a valid read-only request in production.
2. **The response is a top-level array and can be larger than `exchangeInfo`.**
   It contained 993 unique symbols while the simultaneous `exchangeInfo`
   response contained 859. The catalog must continue to join by explicit symbol
   identity and filter from `exchangeInfo`; bracket response size is not a
   universe definition.
3. **Tier counts are symbol-specific.** The capture ranged from 4 to 12 tiers.
   BTCUSDT had 12; HUSDT had 4. Code must not assume a fixed bracket count.
4. **The real BTC schedule invalidated the synthetic numbers.** BTCUSDT's first
   tier reported 150 initial leverage and a 300,000 USDT notional cap, not the
   synthetic fixture's 125 and 50,000. Margin schedules remain venue data, never
   constants.
5. **Numeric fields arrived as JSON numbers.** `bracket` and `initialLeverage`
   were integers; notional bounds, ratios, and cumulative amounts were JSON
   numbers. The client's `json.loads(..., parse_float=Decimal)` boundary and the
   tolerant Pydantic models preserved exact decimal values as designed.
6. **`notionalCoef` was absent from all 993 records.** It remains optional because
   Binance documents it as account-dependent. Absence is represented as `None`,
   never silently replaced with a numeric default.
7. **The three-way production join was complete for the intended universe.** Of
   859 venue symbols, 527 met the v1 status/contract/quote filters. All 527 had
   exact filters, adjusted funding metadata, and margin schedules; the canonical
   catalog therefore contains 527 specifications and zero exclusions. Its first
   content hash is
   `d3a5898667985f09ce7d6ea9e7c0be1b6b759cca499833f8cbbe71687e659787`.

## Decision

- Replace the synthetic margin-bracket client payload with the trimmed recorded
  `fapi_leverageBracket.trimmed.json` fixture. Preserve BTCUSDT's 12 tiers and
  HUSDT's 4 tiers so both ends of the observed range remain under contract test.
- Keep the existing wire schema and mapper. The live payload required no parser
  correction.
- Keep `notionalCoef` optional and content-address every value that affects
  sizing. A later account-specific coefficient therefore changes the catalog
  hash and returns it to `PENDING_REVIEW`.
- Do not turn successful synchronization into implicit approval. At capture time,
  the exact hash above remained `PENDING_REVIEW` until a human named it, an actor,
  and a reason through `instrument-review` as required by ADR-0017.

## Review outcome

The owner explicitly approved the exact hash on 2026-08-11. The append-only
production review event records action `APPROVE`, actor `myself`, and the reason
that all 527 specifications were complete, no candidates were excluded, the
recorded leverage-bracket contract fixture matched the retained source, and the
full verification suite passed. The current catalog status is therefore
`APPROVED`; a later changed hash will return to `PENDING_REVIEW` automatically.

## Consequences

- Phase 2's signed transport and bracket wire shape are live-verified rather than
  documentation-only.
- Contract tests now exercise real account-specific maintenance schedules and
  will expose incompatible wire changes offline.
- A new key, account, venue response, or sizing-relevant field can produce a new
  hash. The latest observation remains authoritative and sizing fails closed
  until that exact version is reviewed.
- Account state, user-data streams, rate-limit failure behaviour, and the missing
  WebSocket stream shapes remain outside this reconciliation.
