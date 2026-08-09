# Project status (living doc)

> **Purpose:** the single place to get oriented — what this system is, how it's
> built, what's done, what's known-broken, and what's next. **Read this first
> after a context reset.** Update it whenever something lands. Keep it terse and
> factual; when work completes, move it from **Backlog** to the relevant phase
> entry.

_Last updated: 2026-08-09 (Phase 1)_

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
instrument universe (ADR-0016). Full rationale in `docs/adr/` (16 ADRs).
**ADR-0015 is the most load-bearing** — it is where documentation met reality.

## Current state (one paragraph)

**Phases 0 and 1 are complete.** Phase 0 built the governance surface: the mode
ladder, the eight-scope append-only kill switch, the watchdog policy,
promotion-gate arithmetic, calibration metrics, and Decimal/quantization
discipline. Phase 1 added a **read-only Binance adapter, live-verified against
production** — tolerant wire schemas, environment-scoped raw retention,
header-driven rate-limit budgeting, and a WebSocket consumer with reconnect and
sequence-gap detection. 425 tests, ruff and strict mypy clean, one Alembic
migration with no drift. The reconciliation against the live API produced
**seven findings that contradicted our documented assumptions**, recorded in
ADR-0015; the largest is that 4-hourly funding is the *majority* on the venue,
not the exception. **There is still no market-data persistence, no model, no risk
engine, and no code path that can submit an order.** Every promotion gate reads
`UNAVAILABLE`, which is the correct reading for a system that has never traded.

## Specification phases

`🟡 Partial` means components exist but the phase's exit criteria do not all
hold. No later phase is inferred from an earlier component existing.

| Phase | Destination | State |
|---|---|---|
| 0 — Governance and repository foundation | Reproducible env, CI, structured logging, audit controls, mode ladder, ported safety spine | ✅ **Done** |
| 1 — Read-only Binance integration | REST + WebSocket ingestion, tolerant schemas, raw retention, rate-limit budget, reconnect/gap tests, live-verified against production read-only | ✅ **Done** (ADR-0015) |
| 2 — Instrument and margin specification | `exchangeInfo` filters, `leverageBracket` tiers, per-symbol funding schedule, versioned and fail-closed on change | 🔴 **Blocked** — needs a read-only API key |
| 3 — Historical archive + live funding series | Full `data.binance.vision` backfill, complete funding history, point-in-time integrity, quality monitoring | ⬜ Not started |
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
  instrument.py             Symbol identity, venue scope, funding schedule
  market_data.py            Mark price, funding, book ticker, kline observations
  errors.py                 Domain exception base
packages/venue_binance/     Read-only venue adapter. No order path exists.
  endpoints.py              Base URLs and paths — one place to correct routing
  auth.py                   HMAC-SHA256 signing (UNVERIFIED — never sent)
  errors.py                 Status + error-code classification
  rate_limit.py             Weight budget driven by the venue's own headers
  schemas.py                Tolerant wire models (extra=allow)
  mapping.py                Wire to domain; the only place field names matter
  client.py                 REST client; retains raw bytes before parsing
  ws_client.py              Combined-stream consumer, reconnect + gap detection
packages/config/            Settings (pydantic-settings) + SecretProvider
packages/storage/           Immutable content-addressed raw-payload store
packages/observability/     JSON logging with credential redaction
packages/db/                Base, engine, Phase-0 models, safety + health repos
apps/cli/main.py            Every command; owns the transaction
migrations/                 Alembic (1 migration: c02df1421a01)
scripts/cron-run.sh         Scheduler entry point (flock, UTC, JSON logs)
tests/                      unit/ (367) + integration/ (29) + contract/ (29)
tests/fixtures/binance/recorded/   Real responses captured 2026-08-09
```

## Commands

The governance surface plus the Phase-1 read-only venue commands.

| Command | Purpose |
|---|---|
| `dashboard` | **Operator view; read this first.** Status + safety + health + gates |
| `status` | Settings, DB connectivity, migration head, mode, order-path assertion |
| `safety-status` | Current scoped kill switches (`--active-only`, `--json`) |
| `safety-halt` / `safety-clear` | Append a control event (`--scope --key --reason --actor`) |
| `health-status` | Durable run history and watchdog verdicts |
| `health-check` | Evaluate scheduled-producer health (`UNAVAILABLE` in Phase 0) |
| `promotion-status` | Gates and the binding constraint |
| `binance-status` | Venue connectivity, clock drift, weight budget, universe size |
| `binance-snapshot` | Fetch public market data and retain every raw byte |

Planned, each landing in its phase: `sync-instruments`, `backfill`,
`record-funding`, `record-prices`, `daily-sync`, `carry-scan`, `paper-trade`,
`paper-cycle`, `paper-report`, `reconcile`, `calibration`, `backtest`.

## Data model

| Table | Holds | Scoped by environment |
|---|---|---|
| `safety_control_events` | Append-only kill-switch transitions; current state = max sequence per (env, scope, key) | ✅ |
| `operational_job_runs` | One row per CLI/cron invocation, written before work starts | n/a (global) |
| `operational_health_assessments` | Every watchdog verdict, passing and blocking | ✅ |

Every market-keyed table added later **must** carry `environment` (ADR-0010).

## Known limitations

Numbered so they can be referenced. This section exists to stop anyone drawing
wrong conclusions from the numbers above.

1. **Phase 2 is blocked on a read-only API key.** `GET /fapi/v1/leverageBracket`
   returns HTTP 401 unauthenticated (ADR-0015 finding 2), and maintenance-margin
   tiers are the input to the liquidation-distance invariant. Create the key with
   **trading disabled and IP-restricted**.
2. **Signing has never been exercised.** `venue_binance/auth.py` is documentation
   plus reasoning; no signed request has ever been sent. Expect it to be wrong on
   first contact and to need one localized fix (ADR-0003).
3. **WebSocket `markPrice` and `kline` payload shapes are unverified.** Only
   `bookTicker` was captured before the probe connections started timing out. The
   combined-stream envelope is confirmed.
4. **Rate-limit failure behaviour is unverified.** 429, 418, `Retry-After`, and
   ban duration are all inferred from documentation; the success-path headers are
   recorded.
5. **Market data is fetched but not persisted.** Phase 1 retains raw bytes to the
   object store; there are no market-data tables. Those land in Phase 3.
6. **The liquidation-distance invariant is stated, not implemented** (ADR-0009).
   Its promotion gate exists; the risk engine does not. Phase 5.
7. **Reconciliation is a gate, not a package** (ADR-0013). `LEDGER_RECONCILED`
   blocks on `UNKNOWN` because nothing can yet produce `RECONCILED`. Phase 7.
8. **`health-check` reports `UNAVAILABLE`.** There are still no *scheduled*
   producers; the venue commands are run by hand. The run-history plumbing
   beneath it is live and tested.
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

1. **Create a read-only, IP-restricted Binance API key** — unblocks Phase 2 and
   limitation 1. This is a manual step for the operator, not a code change.
2. **Phase 2 — `sync-instruments`.** `exchangeInfo` filters and `leverageBracket`
   tiers, content-addressed and versioned, fail-closed on change. Needs (1).
3. **Verify the signing format** against any authenticated endpoint, then replace
   limitation 2 with a recorded finding.
4. **Phase 3 — archive backfill.** `data.binance.vision` klines + full
   `fundingRate` history, point-in-time integrity checks, market-data tables.
5. **Register scheduled producers** in `SCHEDULED_PRODUCERS` and make
   `health-check` real; add the cron entries.
6. **Capture the missing WebSocket payload shapes** (limitation 3) and extend the
   recorded corpus.
7. Validate the remaining freshness tolerances (limitation 10) against measured
   latency.
8. Content-addressed configuration versions and immutable model provenance —
   port the *pattern* from the sibling's ADR-0009/0015.

## Verification

All four must pass before any commit:

```bash
uv run pytest -q          # 425 tests
uv run ruff check .
uv run mypy .             # strict
uv run alembic check      # must report no new upgrade operations
```
