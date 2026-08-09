# 4. Delta-neutral funding carry is the edge thesis, with kill criteria

- Status: Accepted
- Date: 2026-08-09
- Related: [ADR-0005](0005-eight-hour-decision-cadence.md),
  [ADR-0009](0009-liquidation-distance-invariant.md),
  [ADR-0012](0012-prospective-only-promotion-gates.md)

## Context

The sibling repo's edge is **structural, not statistical**: it consumes a
published, exogenous, authoritative input (NWS/ECMWF supercomputer ensembles)
that a retail-priced market does not price. The model is simple. The edge comes
from the input.

BTC/USDT price is the opposite situation, and the abundance of free historical
data is exactly *why*: everyone has it. A directional model trained on OHLCV will
be well calibrated and will never clear an edge gate. That is not speculation —
the sibling's current binding constraint is already `BELOW_DYNAMIC_EDGE` (the
model agreeing with the market) on a *much weaker* market than BTC.

**Copying an architecture without copying its edge structure produces a beautiful
machine that never trades.** So the edge structure has to be chosen deliberately,
before any code, and written down in a form that can be proven wrong.

## Decision

The first strategy domain is **delta-neutral funding-rate carry on Binance
USDⓈ-M perpetual futures**: short the perpetual, hold the equivalent spot, and
harvest the funding payment that longs pay shorts.

The reasoning: perpetual funding is the nearest honest crypto analogue to a
weather ensemble — a *published, exogenous, mechanically-determined* number on a
fixed schedule that produces a cash flow. There is no directional prediction
anywhere in the position. We are paid a documented rate to provide the leverage
that leveraged longs demand.

The model layer forecasts **funding persistence** — `P(funding stays above the
cost threshold over the next N settlements)` — which is a genuine probabilistic
forecasting problem, scoreable with Brier/ECE/reliability/CRPS, so the sibling's
entire calibration apparatus ports unchanged (`packages/domain/calibration.py`).

This is a real, well-understood, and **crowded** trade. It is not a secret. Its
returns are modest in calm regimes and large in manias. That is acceptable: the
goal is a *defensible* edge, not an undiscovered one.

### Kill criteria

The thesis is dead, and the project stops, if any of these hold after the
prospective paper window:

1. Net harvested funding — after fees, slippage, spot-perp basis drift, and the
   cost of capital on both legs — does **not** beat simply holding USDT over the
   same window.
2. The funding-persistence model does not beat a naive "funding will be what it
   was last period" baseline on Brier score.
3. Any paper decision violates the liquidation-distance invariant
   ([ADR-0009](0009-liquidation-distance-invariant.md)) even once.
4. Realized drawdown exceeds the configured limit during the paper window.

Criteria 1, 2, and 3 are encoded directly as promotion gates in
`packages/domain/promotion.py`, so failing one is visible in `promotion-status`
rather than requiring someone to remember this document.

### Out of scope for v1

Directional price prediction. Intraday/HFT. Market making. Options. Altcoin
rotation. Any venue other than Binance.com. Leverage above the configured hard
cap.

## Consequences

- The whole system is built around a cash flow, not a forecast of price, which is
  why the risk engine (not the model) is the hard part.
- A negative result recorded honestly is a **successful** project. A positive
  result reached by weakening a gate is not.
- Because the trade is crowded, funding compression is a live risk. Kill
  criterion 1 is the detector, and it compares against a benchmark rather than
  against zero for exactly that reason.
- If this thesis dies, the architecture survives: the ports, the safety spine,
  and the audit chain are strategy-agnostic. A second domain lands behind the
  same interfaces.
