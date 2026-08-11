# 19. Checksum-verified, source-aware market history

- Status: Accepted
- Date: 2026-08-11
- Related: [ADR-0003](0003-binance-schemas-synthetic-until-recorded.md),
  [ADR-0007](0007-fail-closed-instrument-lifecycle-and-freshness.md),
  [ADR-0010](0010-venue-environment-scoping.md),
  [ADR-0015](0015-binance-live-reconciliation-findings.md),
  [ADR-0016](0016-instrument-universe-and-funding-cadence.md)

## Context

Phase 3 needs years of closed candles and settled funding without turning a
bounded REST API into a bulk-transfer protocol. Binance's public archive is the
right transport, but it is not an immutable object store: Binance documents that
published archives may later be replaced. A URL alone is therefore not durable
provenance.

Archive and REST funding rows are also not the same schema. The production
archive carries the historical funding interval but omits the settlement mark
price and rate type. REST carries mark price and rate type but not the historical
interval. Filling either side's absent fields from the other source—or from the
current instrument catalog—would invent history.

## Decision

1. **Archive objects are production-only evidence.** `data.binance.vision` is
   never routed through or labelled as testnet.
2. **Verify before parsing.** Download the ZIP and its `.CHECKSUM`, verify the
   SHA-256, then retain both raw byte sequences. The retained payload key includes
   the verified digest, so a later replacement becomes a new immutable artifact.
   Refuse checksum-name mismatches, multi-member/encrypted/unsafe ZIPs, oversized
   members, changed CSV headers, and ambiguous timestamp units.
3. **Keep sources independent at ingress.** Archive and REST rows have separate
   uniqueness keys and explicit source provenance. Canonical funding reads merge
   an overlap only when the exact rates agree. REST contributes optional mark
   price/rate type; archive contributes optional historical interval. No missing
   value receives a default.
4. **Normalize time exactly.** Contemporary archive milliseconds and
   microseconds are converted with integer `timedelta` arithmetic, never through
   floating-point POSIX timestamps. Only closed REST candles are persisted.
5. **Backfill completed months, poll the live tail.** Archive requests are
   explicit half-open date ranges with a file-count ceiling and dry-run plan.
   The unpublished current-month tail comes from read-only REST collection.
6. **Quality is durable evidence.** Every funding/kline ingest records PASS or
   BLOCKED plus row, duplicate, gap, conflict, and ordering counts. Empty inputs,
   duplicates, gaps (including across archive-file boundaries), out-of-order
   input, and conflicting facts block. A raw key cannot be reused with changed
   provenance.
7. **Health follows fresh REST artifacts.** Re-polling an unchanged settlement
   is still evidence that the producer worked. Historical archive download time
   is not live freshness, and an immutable settlement row's first collection
   time must not make an otherwise healthy poller appear stale.

## Production reconciliation

On 2026-08-11 we downloaded and checksum-verified three July 2026 BTCUSDT monthly
objects:

| Dataset | SHA-256 | Parsed result |
|---|---|---|
| Funding rate | `e36fcc66f493d7d9ec348c852fc22e9f318c79cf7adae17398a3994ae0adc41e` | 93 settlements; header `calc_time,funding_interval_hours,last_funding_rate` |
| USD-M 1h klines | `a09f135066ace6925ce051ddbbdd4345c103423fbe35d32b568942de978dcc7c` | 744 closed candles; headered; millisecond timestamps |
| Spot 1h klines | `ec98553c10acdbf3f210c55045614e8d3daf616e407c36718269361b1e972b16` | 744 closed candles; headerless; microsecond timestamps |

All three passed with zero gaps and zero conflicts. Re-ingesting the same files
inserted zero rows and recognized all 1,581 facts as existing. A live REST cycle
then inserted ten recent funding observations, mark/index snapshots, and both
spot and USD-M books; the producer-health assessment passed all eight checks.

The timestamp and header asymmetry is under recorded contract test. It is a
venue fact, not a parser convenience.

## Consequences

- A backtest can later reconstruct exactly which bytes supported a fact and can
  detect disagreement instead of depending on source priority.
- Archive replacement is visible and recoverable; it cannot silently rewrite
  prior evidence.
- Raw storage grows with genuinely changed payloads and with each live REST poll.
  This is intentional audit cost.
- Phase 3 remains **partial** until the chosen research universe and historical
  range are explicitly backfilled. The implementation and bounded production
  proof do not claim a full-universe history.

## References

- [Binance public data README](https://github.com/binance/binance-public-data/blob/master/README.md)
- [USD-M funding-rate history](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History)
- [USD-M kline data](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Kline-Candlestick-Data)
