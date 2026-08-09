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

Decision history is in [`docs/adr/`](docs/adr/) (14 ADRs). The three most
load-bearing:

- **[ADR-0009](docs/adr/0009-liquidation-distance-invariant.md)** — the sibling
  repo's `max_loss = cost + fee` invariant is **false** here. A short perp has
  unbounded loss and can be liquidated, and hedging delta does not prevent it
  because the legs sit in different wallets. Read this before touching sizing.
- **[ADR-0012](docs/adr/0012-prospective-only-promotion-gates.md)** — count-based
  promotion gates do not transfer. With five years of free archive you clear any
  count gate in an afternoon.
- **[ADR-0003](docs/adr/0003-binance-schemas-synthetic-until-recorded.md)** —
  every Binance specific in this repo is from documentation, not a recorded
  response. Treat all of them as unverified.

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

- **Always run via `uv`** at `~/.local/bin/uv`. System Python is 3.11 with
  pydantic v1 and will break in confusing ways — never use it.
- Commands: `uv run python apps/cli/main.py <command>`.
- Postgres + Redis via `docker compose up -d`. **Ports are 5433/6380**, offset
  from the sibling `automated-trading-system` stack (5432/6379) so both run side
  by side. User/db `crypto`.
- Cron drives collection via `scripts/cron-run.sh`; logs in `data/logs/`.
  Nothing is scheduled yet — there are no producers.

## Verification (all four must pass before committing)

```bash
uv run pytest -q          # 272 tests
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
- All timestamps are timezone-aware **UTC**. Funding settles on UTC boundaries.
- **Do not read the user's `.env` or any secret file.**

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
