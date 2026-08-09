# Project status (living doc)

> **Purpose:** the single place to get oriented — what this system is, how it's
> built, what's done, what's known-broken, and what's next. **Read this first
> after a context reset.** Update it whenever something lands. Keep it terse and
> factual; when work completes, move it from **Backlog** to the relevant phase
> entry.

_Last updated: 2026-08-09_

> Every figure below is a dated snapshot. Run **`dashboard`** for the live
> operator view — connectivity, kill switches, run history, and promotion-gate
> progress with the current binding constraint — before quoting any number here.

## What this is

An autonomous crypto trading platform. First strategy domain: **delta-neutral
funding-rate carry on Binance USDⓈ-M perpetual futures**, decided on the 8-hour
funding cadence. Near-term goal is **solid paper trading**, not live capital.

Guiding principle: **safety before autonomy** — models may propose, a
deterministic risk engine decides, promotion toward live is stepwise and gated,
and when unsure the system **fails closed**.

Sibling project: [`automated-trading-system`](../../automated-trading-system)
(Kalshi weather markets). It is the reference implementation for every reused
pattern, and it keeps running independently. The two repos share no code at
runtime.

Locked decisions: funding carry as the edge thesis (ADR-0004), 8-hour cadence
(ADR-0005), local-first (ADR-0002), prospective-only promotion gates (ADR-0012),
venue-environment scoping from day one (ADR-0010). Full rationale in `docs/adr/`.

## Current state (one paragraph)

**Phase 0 is complete. Nothing else has started.** The governance surface is
built and tested: the mode ladder, the eight-scope append-only kill switch, the
operational-health watchdog policy, promotion-gate arithmetic, calibration
metrics, and Decimal/quantization discipline — 272 tests, ruff and strict mypy
clean, one Alembic migration with no drift. Three tables exist
(`safety_control_events`, `operational_job_runs`,
`operational_health_assessments`); every one is environment-scoped. The CLI runs
end to end against Postgres and every command records a durable run row. **There
is no Binance client, no market data, no model, no risk engine, and no code path
that can submit an order.** Every promotion gate reads `UNAVAILABLE`, which is
the correct reading for a system that has never traded.

## Specification phases

`🟡 Partial` means components exist but the phase's exit criteria do not all
hold. No later phase is inferred from an earlier component existing.

| Phase | Destination | State |
|---|---|---|
| 0 — Governance and repository foundation | Reproducible env, CI, structured logging, audit controls, mode ladder, ported safety spine | ✅ **Done** |
| 1 — Read-only Binance integration | REST + WebSocket ingestion, tolerant schemas, raw retention, rate-limit budget, reconnect/gap tests, live-verified against production read-only | ⬜ Not started |
| 2 — Instrument and margin specification | `exchangeInfo` filters, `leverageBracket` tiers, per-symbol funding schedule, versioned and fail-closed on change | ⬜ Not started |
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
  errors.py                 Domain exception base
packages/config/            Settings (pydantic-settings) + SecretProvider
packages/storage/           Immutable content-addressed raw-payload store
packages/observability/     JSON logging with credential redaction
packages/db/                Base, engine, Phase-0 models, safety + health repos
apps/cli/main.py            Every command; owns the transaction
migrations/                 Alembic (1 migration: c02df1421a01)
scripts/cron-run.sh         Scheduler entry point (flock, UTC, JSON logs)
tests/                      unit/ (243) + integration/ (29)
```

## Commands

Phase 0 exposes the governance surface only.

| Command | Purpose |
|---|---|
| `dashboard` | **Operator view; read this first.** Status + safety + health + gates |
| `status` | Settings, DB connectivity, migration head, mode, order-path assertion |
| `safety-status` | Current scoped kill switches (`--active-only`, `--json`) |
| `safety-halt` / `safety-clear` | Append a control event (`--scope --key --reason --actor`) |
| `health-status` | Durable run history and watchdog verdicts |
| `health-check` | Evaluate scheduled-producer health (`UNAVAILABLE` in Phase 0) |
| `promotion-status` | Gates and the binding constraint |

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

1. **Nothing has ever touched the Binance API.** Every Binance specific in this
   repo and its README — endpoints, funding intervals, fee tiers, rate-limit
   weights, margin brackets — is from documentation and memory (ADR-0003).
2. **The liquidation-distance invariant is stated, not implemented** (ADR-0009).
   Its promotion gate exists; the risk engine does not. Phase 5.
3. **Reconciliation is a gate, not a package** (ADR-0013). `LEDGER_RECONCILED`
   blocks on `UNKNOWN` today because nothing can produce `RECONCILED` yet.
   Phase 7.
4. **`health-check` reports `UNAVAILABLE`.** There are no scheduled producers in
   Phase 0, and a fabricated always-passing signal would be worse than an honest
   nothing. The run-history plumbing beneath it is live and tested.
5. **Every promotion gate reads `UNAVAILABLE`**, not `PASS`. Correct: no evidence
   exists. See ADR-0012 for why a naive construction would read `PASS`.
6. **Freshness tolerances are guesses** (60 s mark price, 5 s order pricing,
   1 interval funding, 60 s account state). None has been validated against
   recorded latency. Phase 1.
7. **Funding intervals are assumed 8 h in the defaults.** They are per-symbol and
   some pairs settle every 4 h. Must be read from the venue, not assumed.
8. **One scheduler per database.** Two hosts writing the same database both
   append evidence and double-count accrual. Not enforced in code.
9. **Integration tests read committed rows.** They roll back their own
   transaction but see real data. Never assert on a global "latest"/"count" —
   assert on deltas or scope to a row the test created.
10. **No CI secrets, no deployment.** Local is the only path. CI runs the four
    verification commands against a service Postgres.
11. **Brazilian record-keeping is a stated requirement with no implementation.**
    The ledger must eventually export per-trade records with BRL valuation at
    trade time. Retrofitting that into an append-only chain is painful; design
    for it when the ledger lands (Phase 7).
12. **Exchange counterparty risk is unmodelled.** Total capital at the venue is
    itself a risk limit; there is no code for it.

## Backlog (next increments, roughly ordered)

1. **Phase 1 — Binance read-only client.** Auth (HMAC-SHA256), REST clients for
   spot + USDⓈ-M, tolerant wire schemas, raw retention with environment-scoped
   keys, rate-limit weight tracking. First contact produces a
   reconciliation-findings ADR mirroring the sibling's ADR-0005.
2. **Register scheduled producers** in `SCHEDULED_PRODUCERS` and make
   `health-check` real.
3. **Phase 2 — `sync-instruments`.** `exchangeInfo` filters and `leverageBracket`
   tiers, content-addressed and versioned, fail-closed on change.
4. **Phase 3 — archive backfill.** `data.binance.vision` klines + full
   `fundingRate` history, point-in-time integrity checks.
5. Validate the freshness tolerances (limitation 6) against measured latency.
6. Content-addressed configuration versions and immutable model provenance —
   port the *pattern* from the sibling's ADR-0009/0015.

## Verification

All four must pass before any commit:

```bash
uv run pytest -q          # 272 tests
uv run ruff check .
uv run mypy .             # strict
uv run alembic check      # must report no new upgrade operations
```
