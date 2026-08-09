# Binance fixtures

## `recorded/` — real responses, captured 2026-08-09

These are **genuine responses** from Binance production (`fapi.binance.com`,
`api.binance.com`) and testnet (`testnet.binancefuture.com`), captured
unauthenticated over public market-data endpoints. They are the corpus ADR-0003
demands: contract tests parse *these*, not hand-written guesses.

| File | Endpoint | Notes |
|---|---|---|
| `fapi_time.json` | `GET /fapi/v1/time` | |
| `fapi_premiumIndex.json` | `GET /fapi/v1/premiumIndex?symbol=BTCUSDT` | |
| `fapi_premiumIndex_testnet.json` | same, on testnet | Same symbol, *plausibly similar* price — see ADR-0015 |
| `fapi_fundingRate.json` | `GET /fapi/v1/fundingRate?symbol=BTCUSDT&limit=3` | One `fundingTime` is 5 ms past the boundary |
| `fapi_fundingInfo.trimmed.json` | `GET /fapi/v1/fundingInfo` | **Trimmed** to 5 of 742 entries. `updateTime` is `null` for BTC/ETH |
| `fapi_exchangeInfo.trimmed.json` | `GET /fapi/v1/exchangeInfo` | **Trimmed** to 5 of 854 symbols; envelope kept whole |
| `fapi_bookTicker.json` | `GET /fapi/v1/ticker/bookTicker?symbol=BTCUSDT` | Has `time` + `lastUpdateId` |
| `spot_bookTicker.json` | `GET /api/v3/ticker/bookTicker?symbol=BTCUSDT` | Same name, **no** `time`/`lastUpdateId` |
| `fapi_klines.json` | `GET /fapi/v1/klines?symbol=BTCUSDT&interval=8h&limit=2` | Array-of-arrays, positional |
| `fapi_error_invalid_symbol.json` | `GET /fapi/v1/premiumIndex?symbol=NOSUCHPAIR` | HTTP 400 |
| `fapi_error_bad_api_key.json` | `GET /fapi/v1/leverageBracket?symbol=BTCUSDT` | HTTP 401 — endpoint is **not** public |

**Two files are trimmed** (`.trimmed.json` in the name) because the originals are
1 MB and 127 KB. The trimming selects entries deliberately — the two symbols we
care about, one of each non-`TRADING` status, and one tokenised-equity contract —
so the parser still meets every shape the full response contains. The envelope
around a trimmed list is unmodified.

Nothing else in this directory has been edited. If a field looks wrong, it is
what the venue actually sent.

## Not yet recorded

Per ADR-0003, anything not in `recorded/` is **unverified**:

- **WebSocket `markPrice` and `kline` frames.** Only `bookTicker`
  (`ws_bookTicker_frame.json`) was captured before the probe connection started
  timing out. The combined-stream envelope `{"stream": ..., "data": ...}` is
  confirmed; the two payload shapes are not.
- **Everything authenticated**: `leverageBracket` (maintenance-margin tiers),
  account state, user-data streams. `leverageBracket` returning 401 is itself a
  recorded finding — it blocks Phase 2 until a read-only key exists.
- **Rate-limit behaviour under pressure**: 429 and 418 responses, `Retry-After`,
  and IP-ban semantics. The success-path headers are recorded; the failure path
  is inferred from documentation.

Re-record with `uv run python apps/cli/main.py binance-snapshot --out <dir>`.
