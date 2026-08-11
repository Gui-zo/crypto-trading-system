# 15. Binance live reconciliation findings (2026-08-09)

- Status: Accepted
- Date: 2026-08-09
- Mirrors: sibling ADR-0005, the most load-bearing document in that repo
- Discharges: the mandatory follow-up in
  [ADR-0003](0003-binance-schemas-synthetic-until-recorded.md), partially

## Context

[ADR-0003](0003-binance-schemas-synthetic-until-recorded.md) recorded that every
Binance specific in this project came from documentation and memory, and required
a reconciliation against recorded responses before Phase 1 could be called done.

That reconciliation happened on 2026-08-09, against production
(`fapi.binance.com`, `api.binance.com`) and testnet
(`testnet.binancefuture.com`), over public unauthenticated market-data endpoints.
The recorded payloads are committed under `tests/fixtures/binance/recorded/` and
the contract tests parse **those**, not hand-written guesses.

The schemas in `packages/venue_binance/schemas.py` were written *after* the
capture, against reality. That is a deliberate deviation from ADR-0003's implied
order (guess, then diff): where live verification is available up front, writing
the guess first only manufactures a diff nobody needs.

## Findings

Ordered by how much damage each would have done if it had gone unnoticed.

### 1. Funding is 4-hourly for most of the venue — not 8-hourly

The founding README said funding settles *"typically every 8 hours … some pairs
are 4h"*. The capture says the opposite:

| Interval | Symbols |
|---|---|
| 4 hours | **442** |
| 8 hours | 296 |
| 1 hour | 4 |

BTCUSDT and ETHUSDT are 8-hourly, which is why an 8-hour assumption looks correct
for exactly as long as the universe is two symbols. It breaks silently the moment
it widens: carry accrues at the wrong cadence and every persistence forecast is
horizon-mismatched.

`fundingInfo` is the **only** source of the interval — `exchangeInfo` does not
carry it. Consequences are in [ADR-0016](0016-instrument-universe-and-funding-cadence.md).

### 2. `leverageBracket` is not a public endpoint

`GET /fapi/v1/leverageBracket?symbol=BTCUSDT` returns **HTTP 401**
`{"code":-2014,"msg":"API-key format invalid."}` unauthenticated.

Maintenance-margin tiers are the input to the liquidation-distance invariant
([ADR-0009](0009-liquidation-distance-invariant.md)), which is the most important
safety property in this project. **Phase 2 cannot start without a read-only API
key.** That was a scheduling fact rather than a surprise. The signed production
path was subsequently live-verified on 2026-08-11; see
[ADR-0018](0018-authenticated-leverage-bracket-reconciliation.md).

### 3. Testnet and production prices are *plausibly similar*

[ADR-0010](0010-venue-environment-scoping.md) argued for environment scoping on
the grounds that testnet prices "can differ by orders of magnitude". Measured at
the same instant, BTCUSDT was **65100.70** on production and **65102.39** on
testnet — a 0.003% difference.

**The original argument was wrong, and the correction strengthens the decision.**
An obviously-wrong price would be caught by the first person to look at a chart.
A plausibly-similar one never is: a mixed series looks completely normal and
every statistic computed from it is quietly wrong. Scoping cannot rely on anyone
noticing.

The environments also differ in ways that matter for portability:

| | Production | Testnet |
|---|---|---|
| Symbols in `exchangeInfo` | 854 | 731 |
| Carry candidates (TRADING + PERPETUAL + USDT) | 526 | 528 |
| `REQUEST_WEIGHT` limit / minute | **2400** | **6000** |

A strategy tuned against testnet's universe and rate budget will reference
symbols that do not exist in production and will assume 2.5× the request headroom
it actually has.

### 4. Settlement timestamps are not exactly on the boundary

Recorded BTCUSDT settlements: `00:00:00.000`, `08:00:00.**005**`, `16:00:00.000`.

Five milliseconds. A point-in-time join that tests equality against a computed
interval boundary silently drops that row — and silently dropping rows from the
primary evidence series is the failure that produces a confident, wrong backtest.
**Join on an interval window, never on boundary equality.**

### 5. `updateTime` is `null` for exactly the symbols we care about most

In `fundingInfo`, `updateTime` is an integer for most symbols and **`null` for
BTCUSDT and ETHUSDT**. A strict `int` field crashes on the two symbols this
project is built around. Tolerant parsing (ADR-0003) earned its keep on the first
contact.

### 6. Spot and futures disagree about the same endpoint

`GET /ticker/bookTicker` returns `time` and `lastUpdateId` on USDⓈ-M and
**neither** on spot. The two legs of a carry therefore have different provenance
metadata under one endpoint name. Requiring `time` would have made the spot hedge
leg unparseable.

The WebSocket form shares **no field names at all** with the REST form
(`b`/`B`/`a`/`A` versus `bidPrice`/`bidQty`/`askPrice`/`askQty`), so the two need
separate models and separate mappers.

### 7. Smaller corrections

- `MIN_NOTIONAL` carries its value under **`notional`**, not `minNotional`.
- `fundingRate` rows carry an undocumented **`rateType`** field (`"Regular"`).
- Funding rate **caps are per symbol**: BTCUSDT ±0.30%, GTCUSDT ±2.00%. Maximum
  harvestable carry is symbol-specific, not a venue constant.
- 153 of 854 symbols are `TRADIFI_PERPETUAL` — tokenised equities and metals
  (AAPLUSDT, TSLAUSDT, XAUUSDT). See
  [ADR-0016](0016-instrument-universe-and-funding-cadence.md).
- Symbol statuses observed: `TRADING` (726), `SETTLING` (127),
  `PENDING_TRADING` (1).
- The used-weight header arrives **lower-cased** over HTTP/2
  (`x-mbx-used-weight-1m`). Spot sends both the suffixed and unsuffixed forms;
  USDⓈ-M sends only the suffixed one. Case-sensitive lookup of the documented
  name finds nothing on futures, and the budget then never updates.
- Errors carry both an HTTP status and a negative `code`: HTTP 400 `-1121`
  invalid symbol, HTTP 401 `-2014` bad key.

## Decision

Accept the findings, encode each as a test against the recorded payload, and
correct the code and the documents they contradict:

1. `DEFAULT_MAX_FUNDING_AGE` drops from 8 h to **1 h** — the shortest cadence the
   venue runs — so a caller who forgets to pass a symbol's real schedule is
   refused rather than over-permitted. `max_funding_age_for(interval_hours)`
   derives the correct tolerance, with slack for late settlement (finding 4).
2. [ADR-0010](0010-venue-environment-scoping.md)'s "orders of magnitude"
   justification is corrected in place to the stronger, measured one.
3. Every finding above has a named test in
   `tests/contract/test_binance_recorded.py`, each marked **Finding**.

## What is still unverified

Honesty about the boundary of this reconciliation matters more than its length:

- **WebSocket `markPrice` and `kline` payloads.** Only `bookTicker` was captured
  before the probe connections began timing out. The combined-stream envelope is
  confirmed; those two payload shapes are not, and `ws_client` deliberately
  handles only the frame it has seen.
- **Other authenticated surfaces**: account state and user-data streams. Signing
  and `leverageBracket` were subsequently verified in ADR-0018.
- **Rate-limit failure behaviour**: 429, 418, `Retry-After`, ban duration. The
  success-path headers are recorded; the failure path is inferred.
- The raw (`/ws/<stream>`) WebSocket endpoint was unreachable during the probe
  while the combined form worked. Cause unknown; only the combined form is used.

Phase 1 is done for the read path that was verifiable without credentials. The
remaining items carry forward as explicit limitations in `docs/STATUS.md`.

## Consequences

- The founding README's funding-cadence claim is wrong and is corrected wherever
  it appears.
- Phase 2 initially depended on a read-only API key (finding 2); that dependency
  was satisfied and reconciled in ADR-0018.
- Contract tests now fail if Binance changes any of these shapes, which is the
  point.
- Re-record with `binance-snapshot` and diff. The command exists so that the next
  reconciliation is a routine operation rather than another investigation.
