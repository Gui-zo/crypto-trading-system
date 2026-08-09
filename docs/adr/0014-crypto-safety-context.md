# 14. The safety spine ports; its decision context does not

- Status: Accepted
- Date: 2026-08-09
- **New**; records a finding from the actual port
- Related: [ADR-0007](0007-fail-closed-instrument-lifecycle-and-freshness.md),
  [ADR-0010](0010-venue-environment-scoping.md),
  [ADR-0013](0013-reconciliation-before-any-order-path.md)

## Context

The founding README lists `domain/safety.py` (351 lines) under **"port verbatim —
no domain coupling, verified by grep"**.

Reading the file rather than grepping it shows that is not quite true. The
*machinery* is genuinely venue-neutral: the eight kill-switch scopes, the
append-only monotonic control-event log, the never-short-circuit check list, the
automatic-halt latching, and `normalize_scope_key`. All of that ported unchanged.

But `SafetyContext` — the dataclass describing what a decision reads — is built
entirely from prediction-market facts:

| Sibling field | Why it does not port |
|---|---|
| `contract_review_status` | Settlement-rules review; no crypto analogue |
| `forecast_issue_time` / `max_forecast_age` | Weather ensemble issue time |
| `quote_recorded_at` / `max_quote_age` | One quote, one tolerance ([ADR-0007](0007-fail-closed-instrument-lifecycle-and-freshness.md)) |
| `opportunity_expires_at` | Markets here are 24/7 and perpetuals never expire |
| `market_status == "OPEN"` | Binance's vocabulary is `TRADING`, and the states differ |
| `available_cash_cents`, `daily_*_cents` | Integer cents ([ADR-0011](0011-decimal-precision-and-quantization.md)) |

A "verbatim" port would therefore have produced a file that compiles, passes a
lightly-edited test suite, and gates on the wrong facts — the worst possible
outcome for a safety module, because it looks finished.

This ADR exists because a future reader comparing the two repos will notice the
divergence and should find the reason recorded rather than have to reconstruct it.

## Decision

Port the machinery verbatim; rewrite `SafetyContext` and `evaluate_safety`'s
check list for this domain.

The crypto context reads:

- **Venue and instrument** — `venue_enabled`, `instrument_status` (`TRADING`),
  `instrument_spec_status` (`APPROVED`: filters and margin tiers reviewed), and
  `trading_environment` vs `data_environment`
  ([ADR-0010](0010-venue-environment-scoping.md)).
- **Three independent freshness tiers** — mark price, funding rate, account state
  — each with its own tolerance, each producing a *drift* check and a *staleness*
  check ([ADR-0007](0007-fail-closed-instrument-lifecycle-and-freshness.md)).
- **Lineage and ledger integrity** — `provider_keys`, and
  `reconciliation_status`, where `UNKNOWN` blocks
  ([ADR-0013](0013-reconciliation-before-any-order-path.md)).
- **Account limits** in `Decimal` — available margin, daily realized P&L against
  a daily loss limit, rolling drawdown against its limit.

Automatic halts latch for: negative available margin, the daily loss limit, the
drawdown limit, and a proven reconciliation discrepancy.

`domain/operational_health.py` and `domain/modes.py` **did** port verbatim; they
carry no domain assumptions. `domain/calibration.py` ported verbatim with one
addition (`brier_skill_score`) for the naive-persistence comparison in
[ADR-0012](0012-prospective-only-promotion-gates.md).

**What the safety gate deliberately does not do:** it does not size positions and
does not know what a liquidation price is. It answers "may a decision be made
from these inputs at all"; the risk engine
([ADR-0009](0009-liquidation-distance-invariant.md)) answers "how big". Keeping
them apart is what lets the safety gate stay exhaustively testable with no I/O.

## Consequences

- The README's "port verbatim" table overstates `safety.py`. That row is
  corrected in the README, and this ADR records why.
- The sibling's `tests/unit/test_safety.py` does not port either; the crypto
  suite is written fresh against the new context.
- If the two safety spines ever drift in a way that matters, extracting a shared
  package becomes worth considering — but only then, not speculatively.
