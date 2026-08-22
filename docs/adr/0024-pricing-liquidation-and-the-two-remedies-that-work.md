# 24. Pricing liquidation, and the two remedies that survive it

- Status: Accepted
- Date: 2026-08-16
- **Corrects the P&L figures in [ADR-0022](0022-the-carry-horizon-conflict.md)**
  and in [ADR-0023](0023-tail-selection-does-not-save-the-carry-trade.md)
- Related: [ADR-0009](0009-liquidation-distance-invariant.md),
  [ADR-0003](0003-binance-schemas-synthetic-until-recorded.md)

## Context

ADR-0022 reported the 30-day replay as **+1,801.40 with two liquidations**, and
ADR-0023 reported the tail-aware variant as **+3,065.78 with two liquidations**.

Both numbers were wrong, in the flattering direction. The replay recorded that a
position had been liquidated but went on scoring it as though it had run to its
intended exit: funding accrued through the whole window, the legs were marked at
the exit price, and the forfeited margin was never charged. Every configuration
that liquidates was therefore credited with income it could not have collected.

That defect mattered most precisely where the decision was hardest, so it is
corrected first and the conclusions are restated against corrected numbers.

## The correction

A liquidated trade now stops accruing funding at the bar the venue closed it,
marks both legs at the liquidation price, and forfeits the perp margin plus the
symbol's liquidation fee. The hold is walked bar by bar rather than compared
against the window's highest high, because a margin top-up changes the
liquidation price partway through and one maximum cannot represent that.

| hold | parametric net | liq. | tail-aware net | liq. |
|---|---|---|---|---|
| 7 days | −1,565.70 | 0 | −637.68 | 0 |
| 14 days | **−28,297.33** | 1 | **−28,149.34** | 1 |
| 30 days | **−57,150.39** | 2 | **−55,886.00** | 2 |

**No horizon pays.** ADR-0022's framing — "the horizon that pays is the horizon
that gets liquidated" — was too kind. Once liquidation is priced, the horizons
that liquidate are catastrophically negative and the horizon that survives still
loses money. Two liquidations cost 58,571 against 4,544 of funding collected.

ADR-0023's rejection of tail selection stands unchanged: tail-aware sizing is
still the better sizer (−55,886 against −57,150, on 8 trades instead of 28) and
still prevents no liquidation.

## The two remedies, measured

Both were modelled in the replay at a 30-day hold with tail-aware sizing:

| configuration | net | liquidations | top-ups | extra capital |
|---|---|---|---|---|
| Isolated legs (today) | **−55,886.00** | 2 | 0 | 0 |
| Margin top-ups, reserve 0.5× | −27,943 | 1 | 4 | 100,000 |
| Margin top-ups, reserve 1.0× | **+3,065.78** | 0 | 7 | 185,714 |
| Unified collateral | **+3,065.78** | 0 | 0 | 0 |

1. **Margin top-ups work, and cost a great deal of capital.** A reserve of 1.0×
   the position capital eliminates both liquidations, but 185,714 of it is
   actually drawn — the trade consumes roughly 2.9× the capital it appeared to
   need. Half a reserve is not enough; it converts one liquidation, not both.
2. **Unified collateral works cleanly.** With one wallet behind both legs the
   spot gain offsets the perp loss and equity is nearly constant in price, so the
   pair simply stops being fragile. This is ADR-0009's missing mechanism, stated
   there as a warning in 2026-08 and now modelled.

## Decision

1. **Liquidation is priced in every replay from now on.** A backtester that
   records a death without charging for it is worse than no backtester, because
   it produces confident wrong numbers. Pinned by tests.
2. **The corrected figures supersede those in ADR-0022 and ADR-0023.** Those
   documents stand as the record of what was believed when written; this one
   carries the numbers to quote.
3. **`UNIFIED_COLLATERAL` is a modelled hypothesis and is labelled as one**
   everywhere it appears, including a warning printed on every run that uses it.
   Nothing in `tests/fixtures/binance/recorded/` establishes that the venue
   behaves this way for this account, and under ADR-0003 that makes it
   unverified. It must not be quoted as an achievable result.
4. **Phase 7 stays blocked.** Neither remedy is available today: one needs an
   operator procedure and roughly triple the capital, the other needs an account
   configuration nobody has confirmed exists here.

## What is worth noticing about the size of the prize

Even the working configurations return **+3,065.78 over two years on 100,000 of
base capital** — about 1.5% a year before the top-up drag, across 8 trades. That
is barely distinguishable from holding USDT and is plainly below any cash yield.

The remedies fix the catastrophe. They do not, on this evidence, produce a trade
worth doing. ADR-0004 said a negative result recorded honestly is a successful
project, and this is closer to that than to a strategy.

## Consequences

- The immediate next question is factual, not analytical: **can this account use
  portfolio margin at all?** That is a read-only venue question and it decides
  whether option 2 exists. Nothing should be built on it until it is answered.
- The top-up path is available without any venue change, and is measurable —
  but at 2.9× capital for ~1.5% a year it is hard to justify.
- Every P&L number published before this ADR is suspect wherever liquidations
  were non-zero. The 7-day figures are unaffected; they never liquidated.
