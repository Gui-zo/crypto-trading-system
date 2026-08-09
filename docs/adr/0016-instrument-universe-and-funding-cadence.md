# 16. The instrument universe is filtered, and funding cadence is per symbol

- Status: Accepted
- Date: 2026-08-09
- Follows from: [ADR-0015](0015-binance-live-reconciliation-findings.md)
  findings 1 and 7
- Amends: [ADR-0007](0007-fail-closed-instrument-lifecycle-and-freshness.md)'s
  funding-freshness tolerance

## Context

Two findings from the 2026-08-09 reconciliation invalidate assumptions that were
baked into Phase 0 defaults.

**Funding cadence is not a venue constant.** 442 of 742 symbols settle every
4 hours, 296 every 8, and 4 every hour. The founding README had this backwards,
describing 8-hourly as typical and 4-hourly as the exception. `fundingInfo` is
the only endpoint that carries the interval.

**Not everything on Binance USDⓈ-M is crypto.** 153 of 854 symbols are
`TRADIFI_PERPETUAL` — tokenised equities and metals: AAPLUSDT, TSLAUSDT, IBMUSDT,
XAUUSDT, XAGUSDT. They are perpetual contracts on the same venue with the same
wire shape, the same filters, and their own funding rates. Nothing about their
payload distinguishes them; only `contractType` does.

That matters beyond taxonomy. A tokenised equity perpetual has an underlying that
stops trading at a closing bell, has dividends and corporate actions, and has
regulatory characteristics that a Brazilian tax filing
([README, operational notes](../../README.md)) treats entirely differently from
crypto. `AAPLUSDT` also funds **hourly**, so it would drag the whole universe's
cadence assumption to the tightest case.

## Decision

### The carry universe is explicitly filtered

`venue_binance.mapping.is_carry_candidate` admits a symbol only if **all** hold:

1. `status == "TRADING"` — 128 of 854 symbols were `SETTLING` or
   `PENDING_TRADING`;
2. `contractType == "PERPETUAL"` — excludes `TRADIFI_PERPETUAL`, the quarterlies,
   and anything Binance adds later;
3. `quoteAsset == "USDT"` — so the spot hedge leg exists in the same unit, which
   is what makes the position delta-neutral rather than merely hedged-ish.

Unrecognised values of `status` or `contractType` map to `UNKNOWN` and are
**excluded**. Fail closed: a contract type introduced next quarter must not enter
the universe because our enum has not heard of it.

This yields 526 candidates on production. Widening the universe beyond these
three rules is an ADR-worthy decision, not a configuration change.

### Funding cadence is read per symbol, never assumed

`FundingSchedule` carries `interval_hours` from `fundingInfo`, and rejects any
interval that does not divide 24 evenly — every observed value (1, 4, 8) does,
and one that did not would make settlement times drift across UTC days, which
every point-in-time join assumes cannot happen.

`FundingSchedule` also carries the per-symbol rate **cap and floor**, because
those differ by nearly an order of magnitude (BTCUSDT ±0.30%, GTCUSDT ±2.00%) and
they bound the maximum harvestable carry. A universe-wide carry ceiling would be
wrong for every symbol at once.

### The funding-freshness default is tightened, not loosened

`DEFAULT_MAX_FUNDING_AGE` drops from **8 hours to 1 hour**.

The reasoning is the fail-closed one. An 8-hour tolerance applied to a 4-hourly
symbol accepts a funding rate a *whole interval* out of date — precisely the
silent mis-valuation the gate exists to prevent. Defaulting to the shortest
cadence the venue runs means a caller who forgets to pass the symbol's real
schedule is **refused**, which is recoverable, rather than **over-permitted**,
which is not.

`domain.safety.max_funding_age_for(interval_hours)` derives the correct tolerance
as one interval plus five minutes of slack — the slack because settlement is not
punctual (a recorded payment landed 5 ms late), and a tolerance of exactly one
interval would reject a rate that had only just been superseded.

## Consequences

- Every carry calculation is parameterised by the symbol's own cadence and caps.
  There is no "the funding interval" anywhere in the codebase, and there should
  never be.
- A 4-hourly symbol accrues twice as many settlements per day as an 8-hourly one,
  so the ≥ 250 prospective-settlement promotion gate
  ([ADR-0012](0012-prospective-only-promotion-gates.md)) fills at a rate that
  depends on the universe. The 90-day wall-clock gate does not, which is another
  argument for time being the binding gate rather than counts.
- Excluding tokenised equities forgoes a set of instruments that may well carry
  attractive funding. That is deliberate: they are a different asset class with
  different risk, and admitting them should be an explicit decision with its own
  analysis, not a side effect of not having filtered.
- If Binance renames or splits `TRADIFI_PERPETUAL`, the universe silently
  *shrinks* (unknown → excluded) rather than silently growing. That is the right
  direction to fail, and `binance-status` reports the candidate count so a sudden
  change is visible.
