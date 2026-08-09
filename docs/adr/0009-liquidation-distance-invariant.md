# 9. The liquidation-distance invariant replaces the maximum-loss invariant

- Status: Accepted
- Date: 2026-08-09
- **Diverges from** sibling ADR-0021 (audited paper portfolio risk); the sibling's
  sizing invariant is *false* in this domain
- Related: [ADR-0008](0008-fixed-risk-before-kelly.md),
  [ADR-0011](0011-decimal-precision-and-quantization.md),
  [ADR-0013](0013-reconciliation-before-any-order-path.md)

## Context

**This is the largest and most dangerous delta from the sibling repository, and
the one most likely to be silently copied wrong.**

The sibling's risk engine sizes against a single invariant:

> `maximum loss = cost + fee`

That holds because a binary contract can lose at most its premium. Sizing is
therefore a division, the invariant is property-tested, and the test is the most
important test in that repo.

**The invariant is false here.** A short perpetual has theoretically unbounded
loss. Worse, and more immediately: a leveraged position can be **liquidated** —
closed at a loss you did not choose, at the worst possible moment, by the
exchange.

Delta-neutral hedging does *not* prevent this. Hedging reduces net directional
P&L, but the short perp leg and the long spot leg sit in **different wallets with
different margin**. A price rally can liquidate the perp leg while the spot leg
sits there, profitable and useless, in a wallet that cannot post margin to save
it. Net exposure being flat is not the same as each leg being safe.

A codebase that ports the sibling's `domain/risk.py` and adjusts the numbers
inherits an invariant that cannot hold and a test suite that proves the wrong
thing.

## Decision

The sizing invariant is replaced, not adjusted:

> **For every price path within a configured stress band, no leg may be
> liquidated, and total loss may not exceed the configured risk unit.**

Sizing must, in order:

1. **Compute the liquidation price of the perp leg** from Binance's
   maintenance-margin tier table for that symbol and notional. Do not
   approximate it. Read the tiers from `leverageBracket` and version them; they
   change ([ADR-0003](0003-binance-schemas-synthetic-until-recorded.md)).
2. **Require `liquidation_distance >= stress_band`**, where the stress band is a
   configured multiple of forecast volatility over the intended holding horizon —
   start conservative (survive a 3σ 24-hour move) and **floor it at an absolute
   percentage regardless of what the vol model says**, because a vol model that
   underestimates is exactly what a stress band exists to survive.
3. **Cap effective leverage** at a hard, low, versioned value. Start at ≤ 2×. The
   gate is on *effective leverage after sizing*, not on the account's leverage
   setting.
4. **Reserve an unencumbered margin buffer** on the futures wallet — cash sizing
   may never allocate — so an adverse move triggers a top-up decision rather than
   a liquidation.
5. Only then apply the sibling's caps, unchanged in shape: per-instrument
   notional, total notional, correlated-group exposure, available cash, daily
   realized loss, rolling drawdown.

**The correlation group key is the underlying asset, not the instrument.** A BTC
perp short and a BTC spot long are **one** group (`ASSET:BTC`), not two
positions. Treating them as two double-counts capacity, which is how a hedged
book turns out to be twice the size anyone intended.

Sizing arithmetic uses `Decimal` and rounds **down** when capping
([ADR-0011](0011-decimal-precision-and-quantization.md)).

**This invariant is property-tested across symbols, notionals, leverage settings,
and price paths. That test is the single most important test in this
repository.** A violation is a permanent promotion failure, not a statistic
([ADR-0012](0012-prospective-only-promotion-gates.md)).

## Status of implementation

The invariant is **stated but not implemented** — it lands with the risk engine
in Phase 5. What exists in Phase 0 is the promotion gate that will fail the
campaign if it is ever violated (`CeilingGate`, limit 0, breach is permanent).
The gate exists before the code deliberately, so the standard is set before there
is any pressure to meet it.

## Consequences

- The risk engine is the hardest component in the project, by a wide margin, and
  it is worth its cost.
- Some intended positions will be un-sizeable — a stress band that cannot be
  cleared at any size means the trade is refused. That is a correct outcome.
- Maintenance-margin tiers become a first-class versioned artifact rather than a
  constant, and a tier change invalidates prior sizing.
- No leverage, margin-mode, or wallet-transfer change is ever automated. Those
  are configured by hand in the Binance UI. An automated leverage change is an
  automated way to get liquidated.
