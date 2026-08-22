# 23. Selecting against tail regimes does not save the carry trade

- Status: Accepted
- Date: 2026-08-16
- **Tests and rejects** option 3 of [ADR-0022](0022-the-carry-horizon-conflict.md)
- Related: [ADR-0009](0009-liquidation-distance-invariant.md),
  [ADR-0004](0004-funding-carry-edge-thesis.md)

> **P&L figures corrected by [ADR-0024](0024-pricing-liquidation-and-the-two-remedies-that-work.md).**
> The comparison below did not charge for liquidation. The *conclusion* is
> unchanged — tail selection still prevents nothing — but the net figures
> for the liquidating rows are overstated.

## Context

ADR-0022 left four options for the horizon conflict. Option 3 — select against
tail risk rather than change the safety boundary — was chosen first because it
was the cheapest to falsify against data already ingested and it touched nothing
in the non-negotiable safety list.

Two measures were implemented, both leakage-free by construction:

- **`worst_drawup`** — the largest rise from a window's opening price to the
  highest price inside it, over any window of the holding length in the history
  available *before* the decision. Non-parametric: no distribution is fitted, no
  sigma is multiplied. It replaces the 3σ band when larger.
- **`extension_above_trailing`** — how far the latest close sits above its
  trailing median, as a regime filter.

The hypothesis behind the second was explicit: that the ADR-0022 liquidations
opened into a rally already underway.

## What was measured

**Tail-aware sizing improves returns and does not prevent liquidation.**

| hold | parametric net | parametric liq. | tail-aware net | tail-aware liq. |
|---|---|---|---|---|
| 7 days | −1,565.70 | 0 | −637.68 | 0 |
| 14 days | +1,088.79 | 2 | +1,236.79 | **2** |
| 30 days | +1,801.40 | 2 | +3,065.78 | **2** |

At 30 days the empirical band nearly doubled net P&L on 8 trades instead of 28 —
it is a materially better sizer. It stopped nothing.

**The regime filter does not fire on the trades that died**, at any threshold:

| max extension | trades | net | liquidations | refused on regime |
|---|---|---|---|---|
| 0.50 | 8 | 3,065.78 | 2 | 4 |
| 0.30 | 8 | 3,065.78 | 2 | 7 |
| 0.20 | 8 | 3,065.78 | 2 | 21 |
| 0.10 | 6 | 2,360.98 | **2** | 43 |

Tightening to 10% refuses 43 entries and still loses the same two.

**The hypothesis was wrong.** Measured at the fatal entries on 2024-10-30:

| symbol | extension at entry | worst prior 30d rise | what followed |
|---|---|---|---|
| DOTUSDT | **+0.1%** | 33.7% | **+151.5%** |
| XRPUSDT | **−1.1%** | 46.3% | **+211.2%** |

XRP was trading slightly *below* its trailing median. Both markets were as flat
as a market gets. There was no rally to filter out, because the rally had not
started.

## Decision

1. **Option 3 is rejected as a defence of the liquidation invariant.** It cannot
   work: the killing move was three to five times larger than anything in the
   symbol's prior history, and it began from a calm market. An empirical tail
   measure cannot price a tail it has never seen, and a regime filter cannot flag
   a regime that has not yet changed.
2. **Tail-aware sizing is kept anyway**, as the default worth using, because it
   is a better sizer on the same evidence — fewer, better-sized positions,
   nearly double the net at 30 days. It is retained on that merit and explicitly
   *not* as a safety mechanism.
3. **The extension filter is kept but defaults to disabled** (`max_extension=None`).
   Entering an established move remains a poor idea; it is simply not the thing
   that failed.
4. **The false premise is corrected in the code**, not just here. The docstrings
   that justified the filter now record that the measurement refuted them, so the
   next reader does not re-derive the same hope from the same functions.

## Consequences

- ADR-0022's conflict stands, undiminished. Phase 7 remains blocked.
- Of the four options, the two that remain plausible both change something
  previously treated as fixed: **margin top-ups** (an operator procedure, since
  automated wallet transfers stay forbidden) or **cross-margin with the spot leg
  as collateral** (a venue configuration change). The fourth — pricing
  liquidation as a modelled cost instead of forbidding it — rewrites ADR-0009.
- This is what a falsifiable kill criterion is for. The hypothesis was stated,
  measured, and refused in one sitting rather than defended for a quarter.
- A caution for whatever is tried next: two liquidations across 19 symbols and
  two years is not a sample. The negative result here is strong because the
  mechanism is understood, not because the count is large.
