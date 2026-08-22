# 22. The horizon that pays is the horizon that gets liquidated

- Status: Accepted
- Date: 2026-08-16
- **Confirms** [ADR-0009](0009-liquidation-distance-invariant.md) empirically, and
  puts [ADR-0004](0004-funding-carry-edge-thesis.md) kill criteria 1 and 3 in
  direct conflict
- Related: [ADR-0012](0012-prospective-only-promotion-gates.md),
  [ADR-0020](0020-research-backfill-and-funding-cadence-mutability.md),
  [ADR-0021](0021-funding-persistence-baseline-and-the-climatology-gate.md)

> **Corrected by [ADR-0024](0024-pricing-liquidation-and-the-two-remedies-that-work.md).**
> The P&L figures below did not charge for liquidation: a liquidated trade
> kept accruing funding to its intended exit and never forfeited its margin.
> Corrected, every horizon loses money — 14 days is −28,297 and 30 days is
> −57,150. The conflict is real and worse than described; quote ADR-0024's
> numbers, not these.

## Context

Phase 5 built the risk engine and Phase 6 replayed it over the two-year research
history: 19 symbols, real settlements, measured basis, and the highest perp price
reached inside each holding window checked against the position's liquidation
price.

The replay was expected to answer "does carry pay after costs". It answered a
sharper question instead.

## What the replay found

Sizing at 2× effective leverage with a 3σ trailing-volatility stress band, one
position at a time, 100k of capital per symbol:

| hold | trades | net P&L | funding | basis | costs | liquidations |
|---|---|---|---|---|---|---|
| 7 days | 22 | **−1,565.70** | 2,123.74 | 84.74 | 3,774.17 | 0 |
| 14 days | 20 | +1,088.79 | 5,236.96 | −34.97 | 4,113.20 | **2** |
| 30 days | 28 | +1,801.40 | 7,778.67 | −218.95 | 5,758.32 | **2** |

**The horizon that pays is the horizon that gets liquidated.** A seven-day hold
never dies and loses money — 22 trades collect 2,124 of funding against 3,774 of
cost. Extending to fourteen days turns the P&L positive and immediately
introduces liquidations. Nothing in between was found to be both safe and
profitable.

### The two liquidations are not a defect

Both opened on 2024-10-30 and died in the November 2024 rally:

| symbol | entry | liquidation price | worst high | move | distance the engine gave it |
|---|---|---|---|---|---|
| DOTUSDT | 4.18 | 6.20 | 10.51 | **+151.5%** | 48.3% |
| XRPUSDT | 0.5254 | 0.7836 | 1.635 | **+211.2%** | 49.1% |

The invariant held: each position had the liquidation distance it was promised,
and the property tests that guarantee it still pass. What failed is the **stress
band's calibration**. A 3σ band on trailing realized volatility came to roughly
28% for a 30-day hold, and these assets moved five to seven times that.

### The ceiling on any fix by sizing

At 2× effective leverage the maximum achievable liquidation distance is about
49%; at 1× it is about 99%. **No position size survives a 151% move**, because
the distance is bounded by the leverage, not by the size. Sizing smaller does not
help — a smaller short at the same leverage has the same percentage distance.

And the delta-neutral hedge does not rescue it, for exactly the reason ADR-0009
gave before any code existed: the spot leg gains during the rally, but it sits in
a different wallet and cannot post margin to the futures wallet. The hedge was
profitable and useless, which is the sentence ADR-0009 used, now measured.

## Decision

1. **Record the conflict rather than tune it away.** ADR-0004 kill criterion 1
   (beat holding USDT) and criterion 3 (never violate the liquidation invariant)
   are not simultaneously satisfied by any horizon tested. Widening the stress
   band until the liquidations disappear would push every trade into
   `STRESS_BAND` refusal and produce criterion 1's failure instead; that is
   moving the number until the answer is pleasant, which ADR-0004 explicitly
   calls an unsuccessful outcome.
2. **The stress band stays floored and the leverage cap stays at 2×.** They are
   not the thing that failed. Loosening either makes liquidation more likely, not
   less.
3. **A replay liquidation is not promotion evidence of failure**, because a
   backtest is not promotion evidence of anything (ADR-0012). The `CeilingGate`
   counts prospective paper violations only. This finding blocks the strategy on
   judgement, not on a gate reading `FAILED`.
4. **Phase 7 does not start on this configuration.** Running a 90-day paper
   campaign on a variant known to liquidate in replay would spend three months to
   learn what one afternoon already showed.

## What could still make the thesis work

None of these is chosen here; they are the live options, in rough order of how
much they change the project:

- **Margin top-ups.** The mechanically correct answer, and currently forbidden:
  no code path may transfer between wallets. Making this work means an operator
  procedure and an alerting path, not an automated transfer.
- **Cross-margin with the spot leg as collateral**, which changes the venue
  configuration rather than the code, and needs its own reconciliation.
- **Selecting against tail risk** — excluding symbols in a rallying regime, or
  requiring the funding forecast to be strong enough to justify the tail. The
  Phase-4 champion beats climatology, so there is signal to work with.
- **Accepting bounded liquidation losses** as a modelled cost rather than a
  prohibition, which would mean rewriting ADR-0009's invariant and is the option
  most likely to end the project honestly rather than profitably.

## Consequences

- The Phase-6 backtester earned its cost on its first real run, which is the
  argument for building it before Phase 7 rather than after.
- ADR-0009's central claim — that hedging delta does not prevent liquidation
  because the legs sit in different wallets — is now an observation, not a
  warning.
- A negative result recorded honestly is a successful project (ADR-0004). This
  is that, unless one of the options above changes it.
