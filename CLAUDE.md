# CLAUDE.md

Autonomous crypto trading platform. First domain: **delta-neutral funding-rate
carry on Binance USDⓈ-M perpetuals**. Near-term goal is **solid paper trading**,
not live capital.

## Read this first

**[`docs/STATUS.md`](docs/STATUS.md) is the orientation document.** Read it
before doing anything else. It carries the current state, the phase table, the
command reference, what lands in which table, and — most importantly — a numbered
**Known limitations** list that exists to stop you drawing wrong conclusions.
Do not skip that section.

Decision history is in [`docs/adr/`](docs/adr/) (21 ADRs). The seven most
load-bearing:

- **[ADR-0015](docs/adr/0015-binance-live-reconciliation-findings.md)** — the
  live reconciliation, 2026-08-09. Seven findings that contradicted our
  documented assumptions, including that **4-hourly funding is the majority** on
  the venue and that `leverageBracket` is not a public endpoint. This is where
  documentation met reality; read it before trusting any Binance specific.

- **[ADR-0009](docs/adr/0009-liquidation-distance-invariant.md)** — the sibling
  repo's `max_loss = cost + fee` invariant is **false** here. A short perp has
  unbounded loss and can be liquidated, and hedging delta does not prevent it
  because the legs sit in different wallets. Read this before touching sizing.
- **[ADR-0012](docs/adr/0012-prospective-only-promotion-gates.md)** — count-based
  promotion gates do not transfer. With five years of free archive you clear any
  count gate in an afternoon.
- **[ADR-0003](docs/adr/0003-binance-schemas-synthetic-until-recorded.md)** —
  the discipline that made ADR-0015 cheap: tolerant parsing, raw retention before
  parsing, single points of correction. Anything *not* in
  `tests/fixtures/binance/recorded/` is still unverified — notably WebSocket
  `markPrice`/`kline` frames and all rate-limit failure behaviour.
- **[ADR-0017](docs/adr/0017-content-addressed-instrument-catalog-review.md)** —
  the Phase-2 fail-closed rule: the latest catalog hash is authoritative, a
  change returns to `PENDING_REVIEW`, and an older approved version is never a
  fallback.
- **[ADR-0019](docs/adr/0019-checksum-verified-source-aware-market-history.md)** —
  archive URLs are replaceable, archive/REST schemas differ, and live freshness
  follows retained REST artifacts rather than historical rows. Read it before
  changing backfill, canonical series, or producer health.
- **[ADR-0020](docs/adr/0020-research-backfill-and-funding-cadence-mutability.md)** —
  the research universe and range, and what two years of real history proved: a
  symbol's funding cadence **changes over time**, the venue **skips settlements**,
  and a `BLOCKED` quality assessment is point-in-time evidence that a later pass
  can supersede. Read it before modelling funding or trusting a blocked count.

Update `docs/STATUS.md` and the relevant README section whenever something lands,
and write an ADR for any material decision.

## Guiding principle

**Safety before autonomy.** Models may *propose*; a deterministic risk engine
separate from any model *decides*. Promotion toward live is stepwise and gated by
explicit, testable criteria. When unsure, **fail closed** — reject on missing,
stale, or ambiguous input rather than guessing.

**There is no code path that can submit a real order. Do not add one.** Nor one
that changes leverage, margin mode, or transfers between wallets — those are
configured by hand in the Binance UI. And no market orders, ever: limit only, at
a price the risk engine approved.

## Toolchain

- **Always run via `uv`** — take it from `PATH` (`command -v uv`) rather than a
  hardcoded location; it is not always under `~/.local/bin`. System Python is
  3.11 with pydantic v1 and will break in confusing ways — never use it.
- Commands: `uv run python apps/cli/main.py <command>`.
- Postgres + Redis via `docker compose up -d`. **Ports are 5433/6380**, offset
  from the sibling `automated-trading-system` stack (5432/6379) so both run side
  by side. User/db `crypto`.
- Binance market data needs **no API key**. `binance-status` and
  `binance-snapshot` work against production read-only out of the box; set
  `BINANCE_ENV=production` to point at it.
- `sync-instruments` is the only authenticated path. It performs one signed
  **read-only** `leverageBracket` request; credentials belong in the ignored
  `.env`, and `BINANCE_API_SECRET_PATH` must name an owner-only file, not a
  directory. The first authenticated production capture succeeded on 2026-08-11.
- Cron drives collection via `scripts/cron-run.sh`; logs in `data/logs/`.
  `scripts/crontab.example` defines the explicit BTC collectors and watchdog but
  is not proof that a host crontab has been installed.

## Verification (all four must pass before committing)

```bash
uv run pytest -q          # 590 tests
uv run ruff check .
uv run mypy .             # strict
uv run alembic check      # must report no new upgrade operations
```

Integration tests roll back their transaction but **read committed rows**. Never
assert on a global "latest"/"count" query — assert on before/after deltas or
scope the query to a row you created. This bit the sibling repo repeatedly.

## Conventions

- Pure logic lives in `packages/domain/` — no framework, DB, or venue imports.
  Infrastructure sits behind Protocol *ports* so services test with fakes and no
  I/O.
- **`Decimal` everywhere, never float.** `parse_decimal()` refuses float input
  outright. Quantize to the symbol's own filters and **always round down when
  capping** (ADR-0011). Every threshold is in **basis points**, not cents.
- **Every market-keyed table carries `environment`** and every symbol-resolving
  read filters on it (ADR-0010). Binance reuses symbols across testnet and
  production. A missing `environment` column is a defect.
- Repositories **flush but never commit**; the CLI command owns the transaction.
- Append-only means append-only: audit rows are never edited or deleted.
  Corrections create a new artifact; halts clear with an audited event.
- Every CLI command writes an `operational_job_runs` row before it starts and
  closes it after, so a crash leaves RUNNING residue for the watchdog.
- All timestamps are timezone-aware **UTC**. Funding settles *near* UTC
  boundaries — the venue's published `calc_time` carries millisecond jitter, so
  compare with a tolerance, never for equality (ADR-0020).
- **Do not read the user's `.env` or any secret file.**

## Working on the venue adapter

- **Never assume a funding interval.** It is per symbol, from `fundingInfo` —
  442 of 742 symbols were 4-hourly on 2026-08-09, 296 were 8-hourly, 4 hourly
  (ADR-0016). There is no "the funding interval" in this codebase and there
  should never be. Use `max_funding_age_for(interval_hours)` for freshness.
- **Nor is it constant for one symbol over time.** ALICEUSDT ran 8h, then hourly,
  then 4h within the research range (ADR-0020). For any historical settlement use
  the archive's own `funding_interval_hours`, never today's catalog value applied
  backwards.
- **A scheduled settlement may not exist.** The venue skipped 2026-06-24 04:00 UTC
  for every 4-hourly symbol, confirmed against REST. Accrual must read the
  settlements that happened, not the ones the schedule implies.
- **Archive `calc_time` is not the boundary** — it carries millisecond jitter.
  Never compare a funding timestamp for exact equality with a UTC boundary.
- **The universe is filtered**: `TRADING` + `PERPETUAL` + USDT quote. 153 of 854
  symbols are `TRADIFI_PERPETUAL` — tokenised equities and metals (AAPLUSDT,
  TSLAUSDT, XAUUSDT) — and nothing but `contractType` distinguishes them.
  Unknown status or contract type → excluded, fail closed.
- **Correct wire bugs in one place.** URLs in `endpoints.py`, signing in
  `auth.py`, status/code interpretation in `errors.py`, field names in
  `schemas.py` + `mapping.py`. That is what makes a first-contact fix small.
- **Raw bytes are retained before parsing**, so a payload that breaks the parser
  is the one you still have. Keys are environment-scoped.
- Archive ZIPs and their checksum sidecars are retained by verified content
  hash. Archive and REST observations stay source-specific and merge only on
  exact agreement; missing source-native fields are never fabricated.
- Contract tests parse the **recorded** payloads in
  `tests/fixtures/binance/recorded/`. If you change a schema and they fail, the
  venue is right and you are wrong. Re-record with `binance-snapshot`.
- **No order path exists in `venue_binance`, and none may be added.** There is a
  test asserting the client exposes no method whose name suggests one.

## Checking current state

Run `uv run python apps/cli/main.py dashboard` before quoting any figure. Numbers
written into `README.md` / `docs/STATUS.md` are dated snapshots. Promotion-gate
thresholds live in `packages/domain/promotion.py`.

## Reading the promotion gates correctly

Every gate currently reads **`UNAVAILABLE`**, not `PASS`. That is correct: no
evidence exists. A gate with nothing behind it must never render as cleared —
"net carry 0 bps ≥ threshold 0" and "0 liquidation violations ≤ limit 0" would
both read `PASS` under a naive construction, on a system that has never traded.

Three gate kinds, three status rules (ADR-0012):

- `AccrualGate` — climbs to a threshold. `strict=True` needs `observed >
  required`, for gates phrased as *beating* something.
- `CeilingGate` — stays at or below a limit. A breach is `FAILED`, permanently:
  a violation that happened cannot be undone by waiting.
- `has_evidence=False` on either → `UNAVAILABLE`.

Only `EvidenceSource.PAPER_PROSPECTIVE` counts toward a gate. Backtest and
testnet days count for **zero**, and a backtest day cannot bridge a gap in a
consecutive paper run.

The model must clear **two** skill gates, not one (ADR-0021). Beating the naive
0/1 persistence rule is necessary but not sufficient: Brier punishes a confident
error far harder than a hedged one, so on the research history a climatology
forecaster that ignores the previous settlement entirely scored +0.139 against
naive **while using no information**. `brier_skill_vs_climatology` is the gate
that shows the model learned something.

## Relationship to the sibling repo

[`automated-trading-system`](../automated-trading-system) is the reference
implementation for reused patterns, and it keeps running independently — do not
modify it from here. The two repos share no code at runtime.

What ported verbatim: `modes.py`, `operational_health.py`, `calibration.py` (plus
`brier_skill_score`), the raw store, config/secrets, the DB conventions.

What did **not**, despite the founding README saying it would: `safety.py`'s
decision context (ADR-0014), and `promotion.py`'s thresholds (ADR-0012). What
must never be ported: `opportunity.py`, `paper.py`, `risk.py`, `kelly_fraction()`
— all four assume a binary contract paying 100¢.
