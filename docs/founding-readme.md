> **Archived 2026-08-09.** This is the founding specification exactly as it was
> written, before any code existed. It is kept verbatim for provenance: the ADRs
> in `docs/adr/` are the living record, and where the two disagree, the ADRs win.
>
> Three things in here are known to be wrong and are corrected in the live docs:
> the `uv` path (it is `~/.local/bin/uv`), the Postgres/Redis ports (5433/6380,
> offset from the sibling project), and §4's claim that `domain/safety.py` ports
> verbatim (its decision context does not — see ADR-0014). The ADR list in §12 was
> also extended from 13 to 14.

---

# crypto-trading-system

An autonomous crypto trading platform. First strategy domain: **delta-neutral
funding-rate carry on Binance USDⓈ-M perpetual futures**, decided on the 8-hour
funding cadence.

Guiding principle, carried unchanged from its predecessor: **safety before
autonomy.** Models may *propose*; a deterministic risk engine, separate from any
model, *decides*. Promotion toward live capital is stepwise and gated by explicit,
testable criteria. When unsure, **fail closed** — reject on missing, stale, or
ambiguous input rather than guessing.

> **Status: nothing is built.** This document is the founding specification. Once
> code exists, `docs/STATUS.md` becomes the orientation document and this README
> shrinks to "what exists and how to run it", exactly as in the predecessor repo.

---

## 1. Why this project exists, and the one thing it must not skip

This is a sibling of [`automated-trading-system`](../automated-trading-system)
(Kalshi weather prediction markets). That project works: the analytical loop is
complete, 281 tests pass, and the model genuinely beats the market on its domain
(mean Brier 0.1068 vs a 0.2530 market baseline). It cannot be traded because
Kalshi is not legally available here.

**It is critical to understand *why* that project has an edge, because copying its
architecture without copying its edge structure produces a beautiful machine that
never trades.**

That project's edge is structural, not statistical. Kalshi weather markets are
priced by retail participants who mostly do not run numerical weather models. The
platform consumes a *published, exogenous, authoritative* input — NWS/ECMWF
supercomputer ensembles — that the market prices lazily. The model is simple
(pooled ensemble → interval probability). The edge comes from the input, not the
cleverness.

**BTC/USDT price is the opposite situation.** It is among the most efficiently
priced instruments on earth, and the abundance of free historical data is exactly
*why*: everyone has it. A directional model trained on OHLCV will be well
calibrated and will never clear an edge gate. That is not a hypothetical — the
predecessor's current binding constraint is already `BELOW_DYNAMIC_EDGE` (the
model agreeing with the market) on a *weak* market.

So this project does **not** predict price direction. It is built around the
nearest honest crypto analogue to a weather ensemble: a published, exogenous,
mechanically-determined number on a fixed schedule that produces a cash flow.

### The edge thesis, stated falsifiably

**Perpetual futures funding is a scheduled, published, exogenous cash flow.**
Binance USDⓈ-M perps have no expiry, so the contract is tethered to spot by a
funding payment exchanged directly between longs and shorts (typically every 8
hours at 00:00 / 08:00 / 16:00 UTC — verify per symbol, some pairs are 4h). When
funding is positive, longs pay shorts.

A **delta-neutral carry** — short perp + long the equivalent spot — has
approximately zero directional exposure and harvests funding as long as it stays
positive. You are not predicting price. You are being paid a documented rate to
provide the leverage that leveraged longs demand.

This is a real, well-understood, and **crowded** trade. It is not a secret. Its
returns are modest in calm regimes and large in manias. That is fine: the goal
here is a *defensible* edge, not an undiscovered one.

The model layer forecasts **funding persistence** — P(funding remains above cost
threshold over the next N settlements) — which is a genuine probabilistic
forecasting problem, scoreable with Brier/ECE/reliability/CRPS, and therefore
reuses the predecessor's entire calibration and evidence apparatus unchanged.

### Kill criteria — write these down now, before any code

The thesis is **dead**, and the project stops, if any of these hold after the
prospective paper window:

1. Net harvested funding, after fees, slippage, spot–perp basis drift, and the
   cost of capital on both legs, does **not** beat simply holding USDT over the
   same window.
2. The funding-persistence model does not beat a naive "funding will be what it
   was last period" baseline on Brier score.
3. Any paper decision violates the liquidation-distance invariant (§6) even once.
4. Realized drawdown exceeds the configured limit during the paper window.

Record the result either way. A negative result recorded honestly is a successful
project; a positive result reached by weakening a gate is not.

### Explicitly out of scope for v1

Directional price prediction. Intraday/HFT. Market making. Options. Altcoin
rotation. Anything on a venue other than Binance.com. Leverage above the
configured hard cap. These may become later domains behind the same ports; none
of them is v1.

---

## 2. Why 8-hourly and not intraday

This is the single highest-leverage architectural decision, so it is recorded
here rather than discovered later.

The predecessor's ADR-0002 justifies local-first Docker Compose, cron scheduling,
no message queue, and a 90-minute quote-freshness tolerance with one sentence:
*"it is not latency-sensitive."* That premise is load-bearing for the entire
operations layer.

**Funding carry preserves that premise.** Funding settles every 8 hours. The
decision to open, hold, or unwind a carry position is made on that cadence.
Nothing about it is latency-sensitive; a decision made 90 seconds later is
essentially the same decision.

**Intraday would destroy it.** Seconds-scale freshness, a persistent WebSocket
consumer as a supervised daemon, real reconnect/sequence-gap handling, queue
position, adverse selection — all the infrastructure debt ADR-0002 deliberately
deferred comes due at once, *and* you would be competing with firms colocated in
the same datacenter. It is a worse market and a much larger build.

Consequence: cron remains viable, local-first remains viable, and roughly the
whole operations layer of the predecessor ports across. Execution freshness
tolerances still tighten dramatically (§6) — an 8-hour decision cadence does not
excuse a stale price at the moment of order submission.

---

## 3. Non-negotiable safety boundary

Carried from the predecessor, plus three that are new and specific to crypto.

- **There is no code path that can submit a real order.** Do not add one. When
  one is eventually added it lands behind the mode ladder, in its own phase, with
  reconciliation built before submission.
- **No code path may change leverage, margin mode, or transfer between wallets.**
  These are configured once, by hand, in the Binance UI. An automated leverage
  change is an automated way to get liquidated.
- **No code path may place a market order.** Limit orders only, with an explicit
  price the risk engine approved. A market order is an unbounded-slippage
  instruction.
- Models produce probabilities only. Deterministic risk code owns sizing and can
  refuse any proposal.
- Every gate fails closed on missing, stale, ambiguous, or crossed input.
- Append-only means append-only. Audit rows are never edited or deleted;
  corrections create a new artifact; halts clear only via an audited event.
- Fixed risk, never Kelly, until calibration is demonstrated. (Kelly on an
  unbounded-loss instrument is a different and worse mistake than Kelly on a
  binary — see §6.)
- Promotion between operating modes is an explicit human decision.

**Operating modes** (carried verbatim): `RESEARCH → BACKTEST → PAPER → SHADOW →
SUPERVISED_LIVE → AUTONOMOUS_LIMITED`, plus `HALTED`. Enforced in
`packages/domain/modes.py`; there is no path from backtest straight to live.

---

## 4. What ports from the predecessor, and what must be rewritten

Do not port by copying whole packages. Port the modules listed as *verbatim*
first, get them green, then write the rest against them.

### Port verbatim (~1,000 lines, no domain coupling — verified by grep)

| Module | Lines | Notes |
|---|---|---|
| `domain/modes.py` | 142 | Mode ladder. Venue-neutral. |
| `domain/safety.py` | 351 | All 8 kill-switch scopes (GLOBAL, VENUE, STRATEGY, CATEGORY, MARKET, DATA_PROVIDER, MODEL_VERSION, ACCOUNT), append-only monotonic event log. |
| `domain/operational_health.py` | 205 | Watchdog policy. Retune thresholds only. |
| `domain/promotion.py` | 170 | Gate arithmetic + accrual projection. **Thresholds must be re-derived — see §8.** |
| `domain/calibration.py` | 111 | Brier, ECE, reliability, PIT, CRPS. Applies to any probabilistic forecast. |

Also port near-verbatim: `packages/storage/` (raw payload store, local/S3 behind
`create_raw_store(settings)`), `packages/config/` (settings + `SecretProvider`),
the `db/` repository conventions, Alembic setup, `scripts/cron-run.sh`, the CLI
command shape, and the `dashboard` command.

### Port the *pattern*, rewrite the code

- Content-addressed `configuration_versions` (ADR-0009).
- Immutable `model_versions` with source-hash + git-commit provenance (ADR-0015).
- Immutable evaluation runs + durable `PROPOSED`/`EXCLUDED` assessment funnel with
  DB-enforced consistency (ADR-0012). **This is the most valuable pattern in the
  predecessor** — it is what makes "why did nothing trade today" answerable.
- Append-only decision ledger linking config → model → prediction → opportunity →
  decision → fill (ADR-0009/0010/0011).
- Versioned calibration evidence artifacts with exact input lineage (ADR-0016/0017).
- Point-in-time discipline everywhere: a case may only see data whose timestamp
  precedes its decision time (ADR-0014).

### Rewrite completely — the binary-contract assumption runs deep

| Predecessor | Why it does not port |
|---|---|
| `domain/opportunity.py` | `net = win_prob * 100 - price - fee`. The entire edge calculation assumes a contract paying 100¢. There is no crypto analogue. |
| `domain/paper.py` | `payout = 100 * contracts if won`. Crypto positions do not settle; they are marked to market and exited by decision. |
| `domain/risk.py` | Sizing invariant is "maximum loss = cost + fee", true only because a binary can lose at most its premium. **False here.** `kelly_fraction()` is binary-odds Kelly, also wrong. |
| `domain/contract.py`, `market_parser/` | Settlement-rules parsing. No crypto analogue. Delete. |
| `weather/` | Delete. |
| `venue_kalshi/` | Replace with `venue_binance/`. |
| The 8-cent spread gate | Conceptually right, numerically meaningless. 8¢ on a $1 binary is 8%; BTC perp spreads are ~1bp. Re-express every threshold in **basis points**. |

Realistic budget: ~40% direct reuse, ~25% same-shape rewrite, ~35% new.

---

## 5. Architecture

Pure logic in `packages/domain/` — no framework, DB, or venue imports.
Infrastructure sits behind Protocol *ports* so every service tests with fakes and
no I/O. Repositories **flush but never commit**; the CLI command owns the
transaction.

```
packages/
  domain/            Pure value objects + logic. No framework/DB/venue deps.
    modes.py           (ported) operating-mode ladder
    safety.py          (ported) scoped kill switches
    operational_health.py (ported) watchdog policy
    promotion.py       (ported) gate arithmetic
    calibration.py     (ported) forecast scoring metrics
    instrument.py      symbol identity, tick/lot/notional filters, contract specs
    funding.py         funding rate, interval, settlement schedule, accrual math
    basis.py           spot-perp basis, mark vs index, convergence
    carry.py           carry economics: expected funding net of all costs
    liquidation.py     maintenance-margin tiers -> liquidation price
    risk.py            (rewritten) deterministic sizing under unbounded loss
    position.py        (rewritten) two-leg position, mark-to-market, unwind
    edge.py            (rewritten) opportunity economics in basis points
  venue_binance/     Auth (HMAC-SHA256 baseline), REST clients (spot + USDⓈ-M),
                     WebSocket market + user data streams, tolerant wire schemas,
                     wire->domain mapping, rate-limit budget tracking
  archive/           Bulk historical backfill from data.binance.vision
  market_data/       Live klines, mark/index price, funding, book ticker -> Postgres
  funding_history/   Funding-rate time series + forecast inputs
  model/             Funding-persistence model + provenance registration
  opportunity/       Eligible instruments x model -> ranked carry edge
  paper/             Paper broker: two-leg fills, mark-to-market, liquidation sim
  reconciliation/    Venue account state vs local ledger; discrepancy halt
  calibration/       Scores matured forecasts against realized funding
  backtest/          Pure point-in-time replay engine
  db/                SQLAlchemy 2.0 async (psycopg3) models + repositories
  storage/           Immutable raw-payload store
  config/            Settings + secret-provider abstraction
apps/cli/main.py     Operational entrypoint for every command
migrations/          Alembic
scripts/cron-run.sh  Scheduler entry point
tests/               unit / integration / contract
docs/                STATUS.md + adr/
```

`reconciliation/` is new and non-optional. The predecessor could defer it because
it had no order path and one leg. This has two legs, real margin, and eventually a
real order path — local ledger drift is the failure mode that ends accounts.

---

## 6. The risk engine — read this section twice

This is the largest and most dangerous delta from the predecessor.

**The old invariant is false.** A binary contract can lose at most its premium, so
`max_loss = cost + fee` and sizing is a division. A short perpetual has
**theoretically unbounded loss**, and a leveraged position can be **liquidated**
— closed at a loss you did not choose, at the worst possible moment, by the
exchange. Delta-neutral hedging reduces directional P&L but does *not* prevent
liquidation of the short leg, because the two legs sit in different wallets with
different margin.

### The replacement invariant

> **For every price path within a configured stress band, no leg may be
> liquidated, and total loss may not exceed the configured risk unit.**

Concretely, sizing must:

1. Compute the **liquidation price** of the perp leg from Binance's maintenance-
   margin tier table for that symbol and notional. Do not approximate it. Read
   the tiers from the API and version them; they change.
2. Require `liquidation_distance >= stress_band`, where the stress band is a
   configured multiple of forecast volatility over the intended holding horizon
   (start conservative: survive a 3σ 24-hour move, floored at some absolute
   percentage regardless of what the vol model says).
3. Cap **effective leverage** at a hard, low, versioned value. Start at ≤ 2×.
   The gate is on effective leverage after sizing, not on the leverage setting.
4. Reserve **unencumbered margin buffer** on the futures wallet — cash that
   sizing may never allocate — so an adverse move triggers a top-up decision, not
   a liquidation.
5. Then apply the predecessor's caps, unchanged in shape: per-instrument notional,
   total notional, correlated-group exposure, available cash, daily realized loss,
   rolling drawdown.

Property-test the invariant across symbols, notionals, leverage settings, and
price paths, exactly as the predecessor property-tests its maximum-loss invariant.
That test is the single most important test in this repo.

### Other structural differences

- **Correlation group key.** The predecessor uses `WEATHER:<station>:<date>:<variable>`.
  Here it is the **underlying asset**: a BTC perp short and a BTC spot long are
  *one* group, not two positions. Getting this wrong double-counts your capacity.
  Suggested key: `ASSET:<base>` (e.g. `ASSET:BTC`).
- **No settlement boundary.** Markets are 24/7 and positions have no expiry. There
  is no natural daily reset. Define daily-loss windows explicitly in **UTC** and
  make position lifetime decision-driven, not calendar-driven.
- **Financing is continuous.** Funding accrues every 8 hours whether you look or
  not. The ledger must accrue it on schedule, not on decision.
- **Freshness tolerances tighten by orders of magnitude.** The predecessor's
  90-minute quote tolerance is safe for daily weather markets and lethal here.
  Suggested starting points, all versioned and all to be validated:
  - price/mark data used for a *decision*: ≤ 60 seconds
  - price used for *order pricing*: ≤ 5 seconds, with a mandatory pre-submit refresh
  - funding rate: ≤ 1 funding interval
  - account/margin state: ≤ 60 seconds

---

## 7. Data

### Free, no key, and this is the big win over the predecessor

Every gate in the predecessor is calendar-bound — its calibration accrues one
outcome day per day, and an entire ADR (0025) exists solely to work around that.
Binance publishes years of history for free. Backfill in a day, not seven weeks.

- **Bulk archive: `data.binance.vision`.** Monthly and daily ZIPs of klines,
  aggTrades, trades, book ticker, mark-price klines, index-price klines,
  premium-index klines, and more, for spot and USDⓈ-M futures. *Verify exactly
  which datasets and date ranges exist for your symbols before planning around
  them* — do not assume.
- **Funding history: `GET /fapi/v1/fundingRate`**, paginated by time. This is the
  primary evidence series for this project. Backfill it fully and treat it as the
  crown jewels.
- **REST market data** (`/fapi/v1/*`, `/api/v3/*`): klines, `premiumIndex` (mark
  price + current funding), `exchangeInfo` (filters, tick/lot sizes),
  `leverageBracket` (maintenance-margin tiers). No key needed for market data.
- **WebSocket**: mark price stream, kline stream, book ticker. User-data stream
  requires a listen key and an authenticated account.

### Requires keys

Account state, balances, positions, and (eventually) orders. **Create API keys
with trading disabled and IP-restricted until the phase that needs them.** Read-only
keys for everything up to SHADOW.

### Testnet

`testnet.binancefuture.com` (futures) and `testnet.binance.vision` (spot) offer a
real order path with real order semantics. This is materially better than the
predecessor's situation, where Kalshi demo books are empty (ADR-0005 finding 9)
and no fill evidence is obtainable at all. **But testnet liquidity and fills are
not production evidence** — carry the predecessor's discipline of marking every
non-production fill `promotion_eligible = false` in the database.

### Wire-schema discipline — carry ADR-0003 verbatim

Every number in this document about Binance's API is from documentation and
memory, **not from recorded responses.** Therefore, from day one:

1. Tolerant parsing — wire models allow extra fields, most fields optional.
2. **Retain every raw response byte-for-byte** in the raw store before mapping, so
   parser corrections replay against history.
3. Single points of correction: base URLs, signing, and status mapping each live
   in exactly one file.
4. Label synthetic fixtures as synthetic. Replace with recorded responses at first
   contact and diff them.
5. Fee schedules, rate-limit weights, funding intervals, and margin tiers are
   **all** to be read from the API and versioned, never hardcoded from this
   document.

### Precision

The predecessor uses integer cents and integer hundredths-of-a-contract, and
warns loudly against floats. Same rule, harder: crypto quantities have 8+ decimals
and per-symbol tick/step sizes. **Use `Decimal` end to end, quantize to the
symbol's filters, and always round *down* when capping.** A float rounding error
that pushes an order below `minNotional` is an annoyance; one that pushes leverage
above the cap is a liquidation.

---

## 8. Promotion gates — and the trap that abundant data sets

**The predecessor's count-based gates do not transfer, and copying them would be
actively dangerous.**

Its gates (500 resolved predictions, 500 matched forecasts, 60 outcome days) work
as safety mechanisms *because history accrues one day per day*. They are, in
effect, time gates wearing count costumes. Here, you can satisfy any count gate
instantly by backtesting five years of archive. A gate you can clear in an
afternoon is not a gate.

**Therefore: gates must be prospective wall-clock time on data the model has never
seen.** Proposed starting set — tune the numbers, keep the shape:

| Gate | Threshold | Why |
|---|---|---|
| Prospective paper days | ≥ 90 consecutive | Forward-only. A backtest cannot contribute. |
| Realized funding settlements observed prospectively | ≥ 250 | ~3 months at 3/day |
| Net carry vs USDT-hold benchmark | positive over the window | The kill criterion, as a gate |
| Funding-persistence Brier vs naive-persistence baseline | model better | Model adds value |
| Reconciliation discrepancies | zero unexplained | The account-ending failure mode |
| Max observed drawdown | within configured limit | |
| Liquidation-distance invariant violations | zero, ever | Not a statistic; a hard stop |

Keep the predecessor's honesty machinery around these: gates are cleared by an
observed value reaching a threshold, never by a projected date arriving;
projections are naive linear extrapolation and are planning aids only.

Backtests remain enormously valuable — for *rejecting* ideas cheaply, sizing the
opportunity, and finding bugs. They are simply not promotion evidence.

---

## 9. Phase roadmap

Mirrors the predecessor's Phase 0–11 structure so ADRs and vocabulary carry over.
`🟡 Partial` means components exist but the phase's exit criteria do not all hold;
no later phase is inferred from an earlier component existing.

| Phase | Destination |
|---|---|
| 0 — Governance and repository foundation | Reproducible env, CI, structured logging, audit controls, mode ladder, ported safety spine |
| 1 — Read-only Binance integration | REST + WebSocket ingestion, tolerant schemas, raw retention, rate-limit budget, reconnect/gap tests, live-verified against production read-only |
| 2 — Instrument and margin specification | `exchangeInfo` filters, `leverageBracket` tiers, funding schedule per symbol, all versioned and fail-closed on change |
| 3 — Historical archive + live funding series | Full `data.binance.vision` backfill, complete funding history, point-in-time integrity checks, quality monitoring |
| 4 — Funding-persistence model | Baseline + provenance, immutable predictions, calibration, naive-baseline skill, champion registry |
| 5 — Carry economics and risk engine | Edge in basis points net of all costs, liquidation-distance invariant, leverage cap, margin buffer, kill switches, explainable proposals |
| 6 — Historical backtester | Leakage-free replay, realistic fill and slippage models, funding accrual, benchmark comparison |
| 7 — Paper trading | Live-data two-leg simulation, mark-to-market accounting, reconciliation against a read-only real account, dashboards, prospective evidence |
| 8 — Testnet execution | Idempotent orders, cancellation, partial fills, restart recovery, order/fill streaming |
| 9 — Shadow production | Production read-only, exact would-be orders, real cost/slippage/latency measurement |
| 10 — Supervised live | Tiny allocation, per-order human approval, strict halts, daily reconciliation |
| 11 — Limited autonomy | Narrow approved symbols/sizes/windows, anomaly fallback, independent review, rollback proof |

**Do phases 0–3 in order and do not skip 2.** Instrument filters and margin tiers
are this project's analogue of the predecessor's contract parser: the place where
a wrong assumption silently produces a wrong position size. The predecessor's
ADR-0005 (live reconciliation findings) is the single most load-bearing document
in that repo precisely because it is where documentation met reality. Expect the
same here and budget for it.

---

## 10. Toolchain and bootstrap

Identical to the predecessor — deliberately, so context switching costs nothing.

- **Always run via `uv`** at `~/snap/code/250/.local/bin/uv`. System Python is 3.11
  with pydantic v1 and will break in confusing ways. Never use it.
- PostgreSQL + Redis via `docker compose up -d`.
- Commands: `uv run python apps/cli/main.py <command>`.
- Cron drives collection via `scripts/cron-run.sh`; logs in `data/logs/`.

```bash
cp .env.example .env          # fill in as needed
docker compose up -d          # Postgres + Redis
uv sync                       # install dependencies
uv run alembic upgrade head   # create the schema
```

### Verification — all four must pass before any commit

```bash
uv run pytest -q
uv run ruff check .
uv run mypy .                 # strict
uv run alembic check          # must report no new upgrade operations
```

Integration tests roll back their transaction but **read committed rows**. Never
assert on a global "latest"/"count" query — assert on before/after deltas or scope
the query to a run you created. This bit the predecessor repeatedly.

### `.env` shape (starting point)

```
APP_ENV=local
TRADING_MODE=RESEARCH
DATABASE_URL=postgresql+psycopg://platform:platform@localhost:5432/platform
RAW_STORE_BACKEND=local
BINANCE_ENV=testnet            # testnet | production
BINANCE_API_KEY_ID=            # read-only key; leave empty until Phase 1
BINANCE_API_SECRET_PATH=       # file path, never inline, never committed
```

Carry the predecessor's **venue-environment scoping (ADR-0027)** from day one, not
as a retrofit. Binance reuses symbols across testnet and production; every
market-keyed table needs an `environment` column and every symbol-resolving read
needs a scope, or testnet and production prices interleave into one series and
silently corrupt every downstream artifact.

---

## 11. Planned command reference

Shape only; each lands in its phase.

| Command | Purpose |
|---|---|
| `status` | Binance connectivity, server time drift, rate-limit budget |
| `sync-instruments` | `exchangeInfo` filters + `leverageBracket` tiers, versioned |
| `backfill` | Bulk historical klines/funding from the archive; explicit `--start`/`--end`, `--dry-run` |
| `record-funding` | Append point-in-time funding rate + mark/index snapshots |
| `record-prices` | Append point-in-time price/book-ticker snapshots |
| `daily-sync` | **Cron entry**: instruments + funding + prices + health |
| `carry-scan` | Persist a full prediction/assessment run; rank carry opportunities net of costs |
| `paper-trade` | Deterministic risk decisions; evidence-only fills |
| `paper-cycle` | **Cron entry**: reconcile → health → accrue funding → mark → evaluate → score → report |
| `paper-report` / `paper-status` | Cumulative counters **and** current-run funnel with binding constraint |
| `reconcile` | Venue account state vs local ledger; latch a halt on discrepancy |
| `safety-halt` / `safety-clear` / `safety-status` | Append-only scoped kill switches |
| `health-check` / `health-status` | Watchdog verdicts + durable job runs |
| `calibration` | Persist model-linked calibration artifact with sample gates |
| `backtest` | Point-in-time replay: strategy P&L + calibration |
| `dashboard` | Operator view: freshness, evidence, gate progress, binding constraint |

Carry the predecessor's `paper-report` distinction exactly: `execution
[cumulative]` counters freeze when nothing reaches sizing and can make a
months-old blocker look current — the `current-funnel` line with its named binding
constraint is the one that answers "what is blocking us now".

---

## 12. ADRs to write first

Numbered to mirror the predecessor where the decision is the same, so the two
repos stay mutually legible.

| # | Decision |
|---|---|
| 0001 | Record architecture decisions (copy verbatim) |
| 0002 | Local-first stack — **restate the latency premise explicitly for the 8h cadence** |
| 0003 | Binance wire schemas synthetic until validated against recorded responses |
| 0004 | Funding carry as the edge thesis, with the kill criteria from §1 |
| 0005 | 8-hour decision cadence; intraday explicitly rejected and why |
| 0006 | Local cron scheduling |
| 0007 | Fail-closed instrument lifecycle and price freshness (tiered by use) |
| 0008 | Fixed risk before Kelly — and why Kelly is *worse* here than on a binary |
| 0009 | **Liquidation-distance invariant and leverage cap** — the most important one |
| 0010 | Venue-environment scoping from day one (testnet/production) |
| 0011 | Decimal precision and symbol-filter quantization |
| 0012 | Prospective-only promotion gates; why count gates do not transfer |
| 0013 | Reconciliation before any order path |

Then continue with the predecessor's numbering for the audit-chain decisions
(0010–0012, 0015–0019, 0021–0023 there) as those patterns land here.

---

## 13. Operational notes

- **Brazilian record-keeping.** Trading on Binance.com from Brazil carries
  reporting obligations to the Receita Federal. Design the ledger from the start
  to export per-trade records with timestamps, quantities, and BRL valuation at
  trade time — retrofitting that into an append-only audit chain is painful.
  Confirm the specifics with a contador; this is a data-model requirement, not
  tax advice.
- **Exchange counterparty risk is now a real position.** The predecessor's capital
  was never at a venue. Here it is. That is not a code concern, but it belongs in
  the sizing conversation: total capital at the exchange is itself a risk limit.
- **Run one scheduler at a time.** Two hosts writing the same database both append
  evidence and double-count accrual rates.
- **Set the host clock to UTC.** Funding settles on UTC boundaries.

---

## 14. Relationship to the predecessor

Keep [`automated-trading-system`](../automated-trading-system) running. Its cron
keeps accruing evidence for free, its promotion gates keep advancing, and it stays
the reference implementation for every pattern reused here. If prediction markets
become tradeable here later, that project is ready and this one will not have
damaged it.

The two repos share no code at runtime. If the ported safety spine starts drifting
between them in a way that matters, extract it to a shared package **then** — not
speculatively now.

## Disclaimer

This system makes no promise of profitability. It is a research and trading tool
operated under human oversight. Funding-rate carry is a real strategy with real,
enumerable risks — liquidation, exchange counterparty failure, funding regime
change, and basis divergence among them. The gates in this document exist because
the author of the predecessor repo took them seriously; weakening one because it
is inconvenient is the failure mode this entire architecture exists to prevent.
