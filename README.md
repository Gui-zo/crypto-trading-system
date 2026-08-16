# crypto-trading-system

An autonomous crypto trading platform. First strategy domain: **delta-neutral
funding-rate carry on Binance USDⓈ-M perpetual futures**, decided on each
symbol's own funding cadence.

Guiding principle: **safety before autonomy.** Models may *propose*; a
deterministic risk engine, separate from any model, *decides*. Promotion toward
live capital is stepwise and gated by explicit, testable criteria. When unsure,
**fail closed** — reject on missing, stale, or ambiguous input rather than
guessing.

> **Status: Phases 0–3 complete.** Checksum-verified monthly archive ingestion,
> source-aware market-data persistence, live funding/price collectors, and
> producer health checks are built and exercised. The research backfill holds
> 48,905 funding settlements and 665,760 closed 1h candles over
> `[2024-08-01, 2026-08-01)` for 19 symbols — every one exactly complete in both
> spot and USD-M, replaying with zero inserts. Six quality assessments remain
> `BLOCKED`, all true positives: two mid-history funding-cadence changes and one
> settlement the venue skipped (ADR-0020). **Phase 4 has begun:** the funding-
> persistence baseline beats naive by +0.294 and climatology by +0.180 on the
> research history, which is why the model must now clear both gates
> (ADR-0021) — archive replay is worth zero toward promotion. 48,322 immutable
> predictions are recorded under a content-addressed model version, and
> `funding-persistence-v1` is the promoted champion on that research evidence.
> There is no risk engine and no code path that can submit an order.
> **[`docs/STATUS.md`](docs/STATUS.md) is the orientation document** — read it
> first. This README covers what the project is, how to run it, and where to look.

---

## The edge thesis, and why it is not price prediction

This is a sibling of [`automated-trading-system`](../automated-trading-system)
(Kalshi weather markets). That project works — the analytical loop is complete
and the model genuinely beats its market — but it cannot trade, because Kalshi is
not legally available here.

**Copying its architecture without copying its edge structure would produce a
beautiful machine that never trades.** Its edge is *structural*, not statistical:
it consumes a published, exogenous, authoritative input (NWS/ECMWF ensembles)
that a retail-priced market does not price.

BTC price is the opposite situation, and the abundance of free historical data is
exactly why: everyone has it. A directional model trained on OHLCV will be well
calibrated and will never clear an edge gate.

So this project does not predict price. It is built on the nearest honest crypto
analogue to a weather ensemble: **perpetual funding** — a published, exogenous,
mechanically-determined cash flow on a fixed schedule. Short the perp, hold the
equivalent spot, and get paid to provide the leverage that leveraged longs
demand. No directional prediction anywhere. The model layer forecasts *funding
persistence*, which is a genuine probabilistic problem, so the sibling's entire
calibration apparatus ports unchanged.

Funding settles on a published schedule that is **per symbol**: measured on the
live venue, 442 of 742 symbols settle every 4 hours, 296 every 8, and 4 every
hour ([ADR-0016](docs/adr/0016-instrument-universe-and-funding-cadence.md)). BTC
and ETH are 8-hourly, which is why an 8-hour assumption looks right until the
universe widens.

That schedule is not fixed over time, and it is not always kept. A symbol's
cadence changes mid-history — ALICEUSDT ran 8h, then hourly, then 4h inside a
two-year window — and the venue occasionally skips a settlement outright, as it
did at 2026-06-24 04:00 UTC. Both are measured facts from the ingested history
([ADR-0020](docs/adr/0020-research-backfill-and-funding-cadence-mutability.md)),
so accrual must use the interval in force at each settlement and must never
assume a settlement exists because the schedule says it should.

It is a crowded, well-known trade. That is fine — the goal is a *defensible*
edge, not an undiscovered one. Four falsifiable kill criteria are recorded in
[ADR-0004](docs/adr/0004-funding-carry-edge-thesis.md), three of them encoded
directly as promotion gates so failing one shows up in `promotion-status` rather
than requiring anyone to remember the document.

**Out of scope for v1:** directional prediction, intraday/HFT, market making,
options, altcoin rotation, any venue but Binance.com.

## Non-negotiable safety boundary

- **There is no code path that can submit a real order.** Do not add one.
- **No code path may change leverage, margin mode, or transfer between wallets.**
  Those are configured by hand in the Binance UI. An automated leverage change is
  an automated way to get liquidated.
- **No market orders, ever.** Limit only, at a price the risk engine approved. A
  market order is an unbounded-slippage instruction.
- Models produce probabilities only. Deterministic risk code owns sizing and can
  refuse any proposal.
- Every gate fails closed on missing, stale, ambiguous, or crossed input.
- Append-only means append-only. Audit rows are never edited or deleted;
  corrections create a new artifact; halts clear only via an audited event.
- Fixed risk, never Kelly ([ADR-0008](docs/adr/0008-fixed-risk-before-kelly.md)).
- Promotion between operating modes is an explicit human decision.

**Operating modes:** `RESEARCH → BACKTEST → PAPER → SHADOW → SUPERVISED_LIVE →
AUTONOMOUS_LIMITED`, plus `HALTED`. Enforced in `packages/domain/modes.py`; there
is no path from backtest straight to live, and no resuming from `HALTED` directly
into a live mode.

## The two decisions most likely to be got wrong

Both are places where the sibling repo's answer is not merely different but
**actively unsafe** here.

### The risk invariant is not `max_loss = cost + fee`

That holds for a binary contract, which can lose at most its premium. A short
perpetual has **unbounded loss** and can be **liquidated** — closed at a loss you
did not choose, at the worst moment, by the exchange. Delta-neutral hedging does
not prevent this, because the legs sit in **different wallets with different
margin**: a rally can liquidate the perp leg while the spot leg sits there,
profitable and useless.

The replacement invariant: *for every price path within a configured stress band,
no leg may be liquidated, and total loss may not exceed the configured risk unit*
— computed from Binance's real maintenance-margin tiers, not approximated. See
[ADR-0009](docs/adr/0009-liquidation-distance-invariant.md). Its property test
will be the most important test in this repository.

### Count-based promotion gates do not transfer

The sibling's gates (500 predictions, 60 outcome days) work as safety *because
its history accrues one day per day* — they are time gates in count costumes.
With five years of free archive, any count gate here is clearable in an
afternoon. Gates are therefore **prospective wall-clock time on unseen data**
(≥ 90 consecutive paper days), and a backtest contributes zero days. See
[ADR-0012](docs/adr/0012-prospective-only-promotion-gates.md).

## What the live venue said

Phase 1 reconciled public assumptions against real responses from
`fapi.binance.com` — the follow-up ADR-0003 made mandatory. Seven contradicted
what we had written down; all are recorded in
[ADR-0015](docs/adr/0015-binance-live-reconciliation-findings.md) with the
payloads committed under `tests/fixtures/binance/recorded/`. Phase 2's signed
first contact is recorded separately in
[ADR-0018](docs/adr/0018-authenticated-leverage-bracket-reconciliation.md). The
three findings that most shaped the design:

- **4-hourly funding is the majority**, not the exception. The founding spec had
  this backwards. `DEFAULT_MAX_FUNDING_AGE` dropped from 8 h to 1 h so a caller
  who forgets a symbol's real cadence is refused rather than over-permitted.
- **`leverageBracket` is signed and account-specific.** The live read returned
  993 schedules with 4–12 tiers; BTC's real first tier differed materially from
  the synthetic fixture. Those values are immutable catalog data, never
  constants, and any changed catalog hash returns sizing to `PENDING_REVIEW`.
- **Testnet and production prices are *plausibly similar*** — 65100.70 against
  65102.39 at the same instant, not the "orders of magnitude" ADR-0010 originally
  claimed. That correction strengthens the case for environment scoping: an
  obviously-wrong price gets caught, a plausible one never does.

Also: 153 of 854 symbols are tokenised equities and metals (AAPLUSDT, TSLAUSDT,
XAUUSDT) that are indistinguishable from crypto perpetuals by shape alone, and
one recorded settlement landed 5 ms past its interval boundary — enough to make
an equality join silently drop rows from the primary evidence series.

## Quickstart

```bash
cp .env.example .env          # defaults work as-is for local development
docker compose up -d          # Postgres :5433 + Redis :6380
uv sync                       # install dependencies
uv run alembic upgrade head   # create the schema

uv run python apps/cli/main.py dashboard

# Binance market data needs no API key:
BINANCE_ENV=production uv run python apps/cli/main.py binance-status

# After configuring a read-only, IP-restricted key in the ignored `.env`:
uv run python apps/cli/main.py sync-instruments
uv run python apps/cli/main.py instrument-status

# Plan first, then ingest only an explicit completed-month range:
uv run python apps/cli/main.py backfill --dataset funding --market usdm \
  --symbol BTCUSDT --start 2026-07-01 --end 2026-08-01 --dry-run
uv run python apps/cli/main.py record-funding --symbol BTCUSDT
uv run python apps/cli/main.py record-prices --symbol BTCUSDT
uv run python apps/cli/main.py market-data-status
```

Ports are offset from the sibling project's (5432/6379) so both stacks run side
by side. `uv` lives at `~/.local/bin/uv`; the system Python is 3.11 with pydantic
v1 and will break in confusing ways.

### Verification — all four must pass before any commit

```bash
uv run pytest -q          # 535 tests
uv run ruff check .
uv run mypy .             # strict
uv run alembic check      # must report no new upgrade operations
```

## What exists today

```
packages/domain/            Pure logic. No framework, DB, or venue imports.
  modes.py                  Mode ladder (ported verbatim)
  safety.py                 8 kill-switch scopes + crypto decision context
  operational_health.py     Watchdog policy (ported verbatim)
  promotion.py              Prospective gates: accrual, wall-clock, ceiling
  calibration.py            Brier / ECE / reliability / PIT / CRPS / skill
  precision.py              Decimal discipline and symbol-filter quantization
  instrument.py             Identity + canonical filters/funding/margin catalogs
  market_data.py            Mark price, funding, book ticker, kline observations
packages/venue_binance/     Read-only venue adapter; no order path exists
  endpoints/auth/errors     One place each to correct routing, signing, statuses
  schemas.py + mapping.py   Tolerant wire models; the only place fields matter
  client.py                 REST; public data + one signed read-only margin path
  archive.py                Checksum-verified monthly funding/kline archives
  ws_client.py              Combined streams, reconnect, sequence-gap detection
packages/config/            Settings + SecretProvider (env and file-backed)
packages/storage/           Immutable content-addressed raw-payload store
packages/observability/     JSON logging with credential redaction
packages/db/                Audit spine + catalog and source-aware market history
apps/cli/main.py            Every command; owns the transaction
```

Commands: `dashboard`, `status`, `safety-status`, `safety-halt`, `safety-clear`,
`health-status`, `health-check`, `promotion-status`, `binance-status`,
`binance-snapshot`, `sync-instruments`, `instrument-status`, and
`instrument-review`, `backfill`, `record-funding`, `record-prices`, and
`market-data-status`. Full table and the planned commands in
[`docs/STATUS.md`](docs/STATUS.md).

## Conventions that apply to every future change

- Pure logic in `packages/domain/`; infrastructure behind Protocol *ports* so
  services test with fakes and no I/O.
- **`Decimal` everywhere, never float**, quantized to the symbol's filters, and
  **always rounding down when capping**. Every threshold in **basis points**.
  ([ADR-0011](docs/adr/0011-decimal-precision-and-quantization.md))
- **Every market-keyed table carries `environment`** and every symbol-resolving
  read filters on it — Binance reuses symbols across testnet and production.
  ([ADR-0010](docs/adr/0010-venue-environment-scoping.md))
- Repositories flush but never commit; the CLI command owns the transaction.
- Every CLI command writes a durable run row before it starts work, so a crash
  leaves RUNNING residue for the watchdog to find.
- All timestamps timezone-aware **UTC**; funding settles on UTC boundaries.
- Every Binance specific is unverified until diffed against a recorded response.
  Raw bytes are retained *before* parsing, so the payload that breaks the parser
  is the one you still have.
  ([ADR-0003](docs/adr/0003-binance-schemas-synthetic-until-recorded.md))
- **Never assume a funding interval** — it is per symbol, read from the venue.
  ([ADR-0016](docs/adr/0016-instrument-universe-and-funding-cadence.md))

## Documentation map

| Document | What it is for |
|---|---|
| [`docs/STATUS.md`](docs/STATUS.md) | **Start here.** Current state, phases, commands, known limitations, backlog |
| [`CLAUDE.md`](CLAUDE.md) | Working agreement for AI sessions picking this up cold |
| [`docs/adr/`](docs/adr/) | 21 ADRs — why the system is shaped this way. **ADR-0015**, **ADR-0018**, **ADR-0019**, and **ADR-0020** record REST, authenticated, archive, and research-history first contact |
| [`tests/fixtures/binance/`](tests/fixtures/binance/) | Recorded venue responses, and an honest list of what is still unverified |
| [`docs/founding-readme.md`](docs/founding-readme.md) | The original specification, archived verbatim |

## Operational notes

- **Run one scheduler at a time.** Two hosts writing the same database both
  append evidence and double-count accrual.
- Start from `scripts/crontab.example`; replace its absolute path and install it
  only on that scheduler host.
- **Set the host clock to UTC.**
- **Brazilian record-keeping.** Trading on Binance.com from Brazil carries
  reporting obligations to the Receita Federal. Design the ledger to export
  per-trade records with timestamps, quantities, and BRL valuation at trade time —
  retrofitting that into an append-only audit chain is painful. Confirm specifics
  with a contador; this is a data-model requirement, not tax advice.
- **Exchange counterparty risk is a real position.** The sibling project's capital
  was never at a venue. Here it is. Total capital at the exchange is itself a risk
  limit.

## Disclaimer

This system makes no promise of profitability. It is a research and trading tool
operated under human oversight. Funding-rate carry is a real strategy with real,
enumerable risks — liquidation, exchange counterparty failure, funding regime
change, and basis divergence among them. The gates in this repository exist
because they were taken seriously; weakening one because it is inconvenient is
the failure mode this entire architecture exists to prevent.
