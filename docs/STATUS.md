# Project status (living doc)

> **Purpose:** the single place to get oriented — what this system is, how it's
> built, what's done, what's known-broken, and what's next. **Read this first
> after a context reset.** Update it whenever something lands. Keep it terse and
> factual; when work completes, move it from **Backlog** to the relevant phase
> entry.

_Last updated: 2026-08-11 (Phase 3 partial; bounded production ingest verified)_

> Every figure below is a dated snapshot. Run **`dashboard`** for the live
> operator view — connectivity, kill switches, run history, and promotion-gate
> progress with the current binding constraint — before quoting any number here.

## What this is

An autonomous crypto trading platform. First strategy domain: **delta-neutral
funding-rate carry on Binance USDⓈ-M perpetual futures**, decided on the funding
cadence — which is **per symbol** (4h for most of the venue, 8h for BTC/ETH), not
a constant (ADR-0016). Near-term goal is **solid paper trading**, not live capital.

Guiding principle: **safety before autonomy** — models may propose, a
deterministic risk engine decides, promotion toward live is stepwise and gated,
and when unsure the system **fails closed**.

Sibling project: [`automated-trading-system`](../../automated-trading-system)
(Kalshi weather markets). It is the reference implementation for every reused
pattern, and it keeps running independently. The two repos share no code at
runtime.

Locked decisions: funding carry as the edge thesis (ADR-0004), funding-cadence
decisions (ADR-0005), local-first (ADR-0002), prospective-only promotion gates
(ADR-0012), venue-environment scoping from day one (ADR-0010), a filtered
instrument universe (ADR-0016), content-addressed catalog review (ADR-0017).
Full rationale in `docs/adr/` (19 ADRs).
**ADRs 0015, 0018, and 0019 are the load-bearing venue records** — public REST,
authenticated REST, and archive documentation met reality there.

## Current state (one paragraph)

**Phases 0–2 are complete; Phase 3 is partial.**
Phase 0 built the governance surface. Phase 1 added the public read-only Binance
adapter, raw retention, rate-limit budgeting, and WebSocket gap handling. Phase 2
added the signed read-only maintenance-bracket path, exact filter/funding/margin
domain values, canonical SHA-256 catalogs, immutable observations, and
append-only human review. A changed catalog returns to `PENDING_REVIEW`; it never
falls back to an older approved hash. Phase 3 now has checksum-verified monthly
funding/kline archives, independent archive/REST provenance, exact Decimal and
timestamp persistence, quality assessments, live funding/price collectors, and
real producer health checks. The public reconciliation produced seven
findings in ADR-0015; the signed production capture succeeded and its findings
are recorded in ADR-0018; archive reconciliation is in ADR-0019. The resulting
527-specification catalog is `APPROVED` under exact hash
`d3a5898667985f09ce7d6ea9e7c0be1b6b759cca499833f8cbbe71687e659787`.
The bounded production proof stored 93 July BTC funding settlements plus 744
USD-M and 744 spot 1h candles, replayed idempotently, recorded a live REST cycle,
and passed all eight health checks with zero blocked quality assessments.
**There is still no model, no risk engine, and no code path that can submit an
order.** Every promotion gate reads
`UNAVAILABLE`, correctly, because the system has never traded.

## Specification phases

`🟡 Partial` means components exist but the phase's exit criteria do not all
hold. No later phase is inferred from an earlier component existing.

| Phase | Destination | State |
|---|---|---|
| 0 — Governance and repository foundation | Reproducible env, CI, structured logging, audit controls, mode ladder, ported safety spine | ✅ **Done** |
| 1 — Read-only Binance integration | REST + WebSocket ingestion, tolerant schemas, raw retention, rate-limit budget, reconnect/gap tests, live-verified against production read-only | ✅ **Done** (ADR-0015) |
| 2 — Instrument and margin specification | `exchangeInfo` filters, `leverageBracket` tiers, per-symbol funding schedule, versioned and fail-closed on change | ✅ **Done** — signed production capture reconciled and exact catalog hash approved (ADR-0018) |
| 3 — Historical archive + live funding series | Full `data.binance.vision` backfill, complete funding history, point-in-time integrity, quality monitoring | 🟡 **Partial** — implementation + bounded BTC production proof complete; selected full history not yet backfilled (ADR-0019) |
| 4 — Funding-persistence model | Baseline + provenance, immutable predictions, calibration, naive-baseline skill, champion registry | ⬜ Not started |
| 5 — Carry economics and risk engine | Edge in bps net of all costs, **liquidation-distance invariant**, leverage cap, margin buffer, explainable proposals | ⬜ Not started |
| 6 — Historical backtester | Leakage-free replay, realistic fill/slippage, funding accrual, benchmark comparison | ⬜ Not started |
| 7 — Paper trading | Live-data two-leg simulation, mark-to-market, **reconciliation against a read-only real account**, dashboards, prospective evidence | ⬜ Not started |
| 8 — Testnet execution | Idempotent orders, cancellation, partial fills, restart recovery, order/fill streaming | ⬜ Not started |
| 9 — Shadow production | Production read-only, exact would-be orders, real cost/slippage/latency measurement | ⬜ Not started |
| 10 — Supervised live | Tiny allocation, per-order human approval, strict halts, daily reconciliation | ⬜ Not started |
| 11 — Limited autonomy | Narrow approved symbols/sizes/windows, anomaly fallback, independent review, rollback proof | ⬜ Not started |

**Do phases 0–3 in order and do not skip 2.** Instrument filters and margin tiers
are this project's analogue of the sibling's contract parser: the place where a
wrong assumption silently produces a wrong position size.

## What exists, file by file

```
packages/domain/            Pure logic. No framework, DB, or venue imports.
  modes.py                  Mode ladder (ported verbatim)
  safety.py                 8 kill-switch scopes + crypto decision context (ADR-0014)
  operational_health.py     Watchdog policy (ported verbatim)
  promotion.py              Prospective gates, accrual + ceiling kinds (ADR-0012)
  calibration.py            Brier/ECE/reliability/PIT/CRPS + Brier skill
  precision.py              Decimal discipline, filter quantization (ADR-0011)
  instrument.py             Identity + exact filters/funding/margin catalog
  market_data.py            Mark price, funding, book ticker, kline observations
  errors.py                 Domain exception base
packages/venue_binance/     Read-only venue adapter. No order path exists.
  endpoints.py              Base URLs and paths — one place to correct routing
  auth.py                   Live-verified HMAC-SHA256 signed read-only requests
  errors.py                 Status + error-code classification
  rate_limit.py             Weight budget driven by the venue's own headers
  schemas.py                Tolerant wire models (extra=allow)
  mapping.py                Wire to domain; the only place field names matter
  client.py                 REST; public data + signed read-only margin brackets
  archive.py                Checksum-verified production funding/kline archives
  ws_client.py              Combined-stream consumer, reconnect + gap detection
packages/config/            Settings (pydantic-settings) + SecretProvider
packages/storage/           Immutable content-addressed raw-payload store
packages/observability/     JSON logging with credential redaction
packages/db/                Audit, catalog, market history, and quality repos
apps/cli/main.py            Every command; owns the transaction
migrations/                 Alembic (3 migrations through 62848a719f99)
scripts/cron-run.sh         Scheduler entry point (flock, UTC, JSON logs)
scripts/crontab.example     Explicit BTC live-collector/watchdog schedule
tests/                      476 unit + integration + recorded contracts
tests/fixtures/binance/recorded/   Real responses captured 2026-08-09
```

## Commands

The governance surface plus the Phase-1–3 read-only venue/data commands.

| Command | Purpose |
|---|---|
| `dashboard` | **Operator view; read this first.** Status + safety + health + gates |
| `status` | Settings, DB connectivity, migration head, mode, order-path assertion |
| `safety-status` | Current scoped kill switches (`--active-only`, `--json`) |
| `safety-halt` / `safety-clear` | Append a control event (`--scope --key --reason --actor`) |
| `health-status` | Durable run history and watchdog verdicts |
| `health-check` | Evaluate funding/price run history and retained REST-artifact freshness |
| `promotion-status` | Gates and the binding constraint |
| `binance-status` | Venue connectivity, clock drift, weight budget, universe size |
| `binance-snapshot` | Fetch public market data and retain every raw byte |
| `sync-instruments` | Signed read-only sync; version filters, funding, and margin tiers |
| `instrument-status` | Current exact catalog hash, exclusions, and review status |
| `instrument-review` | Append APPROVE/REJECT for the exact current catalog hash |
| `backfill` | Plan or ingest checksum-verified monthly archives over `[start,end)` |
| `record-funding` | Persist settled funding plus a current mark/index snapshot |
| `record-prices` | Persist current mark/index and spot/USD-M best books |
| `market-data-status` | Persisted row/artifact counts, quality blocks, and live freshness |

Planned, each landing in its phase: `daily-sync`, `carry-scan`, `paper-trade`,
`paper-cycle`, `paper-report`, `reconcile`, `calibration`, `backtest`.

## Data model

| Table | Holds | Scoped by environment |
|---|---|---|
| `safety_control_events` | Append-only kill-switch transitions; current state = max sequence per (env, scope, key) | ✅ |
| `operational_job_runs` | One row per CLI/cron invocation, written before work starts | n/a (global) |
| `operational_health_assessments` | Every watchdog verdict, passing and blocking | ✅ |
| `instrument_catalog_versions` | Immutable canonical catalogs, unique by environment + SHA-256 | ✅ |
| `instrument_catalog_observations` | Every sync linked to its retained raw sources | ✅ |
| `instrument_catalog_review_events` | Append-only exact-hash approvals and rejections | ✅ |
| `market_data_source_artifacts` | Immutable REST/archive provenance, checksums, ranges, raw keys | ✅ |
| `funding_rate_observations` | Source-specific settled rates + optional source-native fields | ✅ |
| `kline_observations` | Source-specific closed exact OHLCV candles | ✅ |
| `mark_price_snapshots` | Point-in-time mark/index/current-funding snapshots | ✅ |
| `book_ticker_snapshots` | Spot/USD-M best bid/ask observations | ✅ |
| `market_data_quality_assessments` | Durable PASS/BLOCKED gap/duplicate/conflict verdicts | ✅ |

Every market-keyed table added later **must** carry `environment` (ADR-0010).

## Known limitations

Numbered so they can be referenced. This section exists to stop anyone drawing
wrong conclusions from the numbers above.

1. **Catalog approval is snapshot-specific.** The current 527-specification hash
   is approved, but any sizing-relevant venue change creates a new hash and
   immediately returns the authoritative catalog to `PENDING_REVIEW`; there is
   deliberately no fallback to this older approved version.
2. **Only one account-specific bracket capture has been observed.** Signing and
   the 4–12 tier wire shape are verified, but all 993 records omitted the optional
   `notionalCoef`. A future account-specific coefficient must create a new hash
   and return it to review (ADR-0018).
3. **WebSocket `markPrice` and `kline` payload shapes are unverified.** Only
   `bookTicker` was captured before the probe connections started timing out. The
   combined-stream envelope is confirmed.
4. **Rate-limit failure behaviour is unverified.** 429, 418, `Retry-After`, and
   ban duration are all inferred from documentation; the success-path headers are
   recorded.
5. **Phase 3 is not a full-history claim.** Only July 2026 BTC funding and 1h
   spot/USD-M klines have been production-ingested as a bounded proof. Choose and
   backfill the complete research range/universe before Phase 3 can be marked done.
6. **The liquidation-distance invariant is stated, not implemented** (ADR-0009).
   Its promotion gate exists; the risk engine does not. Phase 5.
7. **Reconciliation is a gate, not a package** (ADR-0013). `LEDGER_RECONCILED`
   blocks on `UNKNOWN` because nothing can yet produce `RECONCILED`. Phase 7.
8. **The cron file is an example, not installed state.** `health-check` is real
   and passed after the manual BTC live cycle, but freshness will age out unless
   `scripts/crontab.example` is installed on exactly one scheduler host.
9. **Every promotion gate reads `UNAVAILABLE`**, not `PASS`. Correct: no evidence
   exists. See ADR-0012 for why a naive construction would read `PASS`.
10. **Freshness tolerances are still mostly guesses.** The funding tolerance is
    now derived from the venue's own per-symbol interval (ADR-0016), but the
    60 s mark-price, 5 s order-pricing, and 60 s account-state figures have not
    been validated against recorded latency.
11. **Testnet and production are different venues in every way that matters.**
    854 symbols vs 731, weight limit 2400/min vs 6000/min, and *plausibly
    similar* prices that would not look wrong if interleaved (ADR-0015 finding
    3). Testnet evidence is never production evidence.
12. **One scheduler per database.** Two hosts writing the same database both
    append evidence and double-count accrual. Not enforced in code.
13. **Integration tests read committed rows.** They roll back their own
    transaction but see real data. Never assert on a global "latest"/"count" —
    assert on deltas or scope to a row the test created.
14. **Brazilian record-keeping is a stated requirement with no implementation.**
    The ledger must eventually export per-trade records with BRL valuation at
    trade time. Design for it when the ledger lands (Phase 7).
15. **Exchange counterparty risk is unmodelled.** Total capital at the venue is
    itself a risk limit; there is no code for it.

## Backlog (next increments, roughly ordered)

1. **Finish Phase 3 history.** Select the research date range/universe, dry-run
   it in bounded batches, then ingest all required spot/USD-M klines and funding.
2. **Install the collector schedule** from `scripts/crontab.example` on exactly
   one host and observe its durable health over wall-clock time.
3. **Capture the missing WebSocket payload shapes** (limitation 3) and extend the
   recorded corpus.
4. Validate the remaining freshness tolerances (limitation 10) against measured
   latency.
5. Immutable model provenance — port the *pattern* from the sibling's
   ADR-0009/0015. Instrument catalog versioning now exists as the local pattern.

## Verification

All four must pass before any commit:

```bash
uv run pytest -q          # 476 tests
uv run ruff check .
uv run mypy .             # strict
uv run alembic check      # must report no new upgrade operations
```
