# Binance fixtures

## `recorded/` — real responses and archive rows, captured 2026-08-09 and 2026-08-11

These are **genuine responses and CSV rows** from Binance production
(`fapi.binance.com`, `api.binance.com`, `data.binance.vision`) and testnet
(`testnet.binancefuture.com`). Most REST responses were captured unauthenticated
over public market-data endpoints on 2026-08-09; the successful signed,
read-only `leverageBracket` response and public-archive first contact were
captured on 2026-08-11. They are the corpus ADR-0003 demands: contract tests
parse *these*, not hand-written guesses.

| File | Endpoint | Notes |
|---|---|---|
| `fapi_time.json` | `GET /fapi/v1/time` | |
| `fapi_premiumIndex.json` | `GET /fapi/v1/premiumIndex?symbol=BTCUSDT` | |
| `fapi_premiumIndex_testnet.json` | same, on testnet | Same symbol, *plausibly similar* price — see ADR-0015 |
| `fapi_fundingRate.json` | `GET /fapi/v1/fundingRate?symbol=BTCUSDT&limit=3` | One `fundingTime` is 5 ms past the boundary |
| `fapi_fundingInfo.trimmed.json` | `GET /fapi/v1/fundingInfo` | **Trimmed** to 5 of 742 entries. `updateTime` is `null` for BTC/ETH |
| `fapi_exchangeInfo.trimmed.json` | `GET /fapi/v1/exchangeInfo` | **Trimmed** to 5 of 854 symbols; envelope kept whole |
| `fapi_leverageBracket.trimmed.json` | signed `GET /fapi/v1/leverageBracket` | **Trimmed** to BTC's 12 tiers and HUSDT's 4 tiers from 993 symbols; no credential material is present in the response |
| `fapi_bookTicker.json` | `GET /fapi/v1/ticker/bookTicker?symbol=BTCUSDT` | Has `time` + `lastUpdateId` |
| `spot_bookTicker.json` | `GET /api/v3/ticker/bookTicker?symbol=BTCUSDT` | Same name, **no** `time`/`lastUpdateId` |
| `fapi_klines.json` | `GET /fapi/v1/klines?symbol=BTCUSDT&interval=8h&limit=2` | Array-of-arrays, positional |
| `fapi_error_invalid_symbol.json` | `GET /fapi/v1/premiumIndex?symbol=NOSUCHPAIR` | HTTP 400 |
| `fapi_error_bad_api_key.json` | `GET /fapi/v1/leverageBracket?symbol=BTCUSDT` | HTTP 401 — endpoint is **not** public |
| `archive_fundingRate_BTCUSDT_2026-07.trimmed.csv` | monthly USD-M `fundingRate` archive | **Trimmed** to 4 of 93 rows; headered; carries historical interval |
| `archive_futures_klines_BTCUSDT_1h_2026-07.trimmed.csv` | monthly USD-M `klines` archive | **Trimmed** to 3 of 744 rows; headered; millisecond timestamps |
| `archive_spot_klines_BTCUSDT_1h_2026-07.trimmed.csv` | monthly spot `klines` archive | **Trimmed** to 3 of 744 rows; headerless; microsecond timestamps |

The `.trimmed.*` files select representative rows from large responses. The
public REST fixtures select the two symbols we care about, one of each
non-`TRADING` status, and one tokenised-equity contract. The authenticated
fixture selects a 12-tier liquid symbol and a 4-tier long-tail symbol, preserving
the observed tier-count range. Archive fixtures preserve rows byte-for-byte but
not the full monthly file; full ZIPs and checksums live in the ignored raw store.

Nothing else in this directory has been edited. If a field looks wrong, it is
what the venue actually sent.

## Not yet recorded

Per ADR-0003, anything not in `recorded/` is **unverified**:

- **WebSocket `markPrice` and `kline` frames.** Only `bookTicker`
  (`ws_bookTicker_frame.json`) was captured before the probe connection started
  timing out. The combined-stream envelope `{"stream": ..., "data": ...}` is
  confirmed; the two payload shapes are not.
- **Other authenticated surfaces**: account state and user-data streams.
  `leverageBracket` signing and parsing are now live-verified in ADR-0018, but no
  broader account payload has been requested or recorded.
- **Rate-limit behaviour under pressure**: 429 and 418 responses, `Retry-After`,
  and IP-ban semantics. The success-path headers are recorded; the failure path
  is inferred from documentation.

Re-record REST fixtures with
`uv run python apps/cli/main.py binance-snapshot --out <dir>` and the signed
catalog inputs with `uv run python apps/cli/main.py sync-instruments`. Re-record
archive evidence through a bounded `backfill --start ... --end ...`; never copy
an unverified ZIP into the corpus.
