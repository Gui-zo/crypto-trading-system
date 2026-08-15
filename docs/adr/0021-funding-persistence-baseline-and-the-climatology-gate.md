# 21. The funding-persistence baseline, and why naive alone is not a gate

- Status: Accepted
- Date: 2026-08-15
- Amends: [ADR-0004](0004-funding-carry-edge-thesis.md) kill criterion 2
- Related: [ADR-0012](0012-prospective-only-promotion-gates.md),
  [ADR-0016](0016-instrument-universe-and-funding-cadence.md),
  [ADR-0020](0020-research-backfill-and-funding-cadence-mutability.md)

## Context

ADR-0004 fixed the forecasting target — `P(funding stays above the cost
threshold over the next N settlements)` — and made kill criterion 2 the
requirement that the model beat a naive *"funding will be what it was last
period"* baseline on Brier score. Phase 3 produced the history to test it:
48,905 archive settlements across 19 symbols and two years.

Running the first baseline model against that history produced a pooled Brier
skill of **+0.294** against naive, with all 19 symbols winning. That number is
misleading, and the way it misleads matters more than the number.

The model conditions on exactly one bit — did the previous settlement clear the
threshold — which is the *same* bit the naive rule uses. A forecaster with no
additional information should not gain 0.29 of skill. The gain is a scoring
artifact: naive emits a hard **0/1** label, and Brier punishes a confident error
far more than a hedged one, so almost any calibrated probability beats it.

Measured directly, on the same cases under the same leakage rule:

| threshold | naive (0/1) | climatology | model | model vs naive | model vs climatology |
|---|---|---|---|---|---|
| 0 bps | 0.22497 | 0.19360 | 0.15874 | +0.294 | +0.180 |
| 0.5 bps | 0.24792 | 0.23378 | 0.18221 | +0.265 | +0.221 |
| 1 bps | 0.12816 | 0.15476 | 0.10151 | +0.208 | +0.344 |

Climatology — the expanding base rate, which ignores the previous settlement
entirely — scores **+0.139 against naive at a 0 bps threshold while using no
information at all**. Roughly half the headline skill was available to a
forecaster that had learned nothing.

A gate that a zero-information forecaster clears is the same failure ADR-0012
was written to prevent: a gate with nothing behind it must never read as passed.

## Decision

1. **The target** is `P(every one of the next N observed settlements pays at
   least the threshold)`. The threshold is in basis points and is carried on the
   target, not hardcoded, because the cost-aware value is Phase 5's to compute.
   Predictions are comparable only within an identical target.

2. **Cases are built from settlements that were observed**, never from the
   boundaries a schedule implies. A window spanning a venue hole is kept and
   reports its largest step, because discarding those windows would discard
   exactly the unusual regimes carry cares about (ADR-0020). Each case records
   the funding interval **in force at that settlement**.

3. **The leakage rule is resolution time, not decision time.** A case resolves at
   the last settlement of its target window, and may inform another forecast only
   if it resolved strictly before that forecast's decision. With a one-settlement
   horizon this makes the newest usable case two decisions back, not one.

4. **Two baselines, both gated.** `REQUIRED_BRIER_SKILL_VS_NAIVE` stays, and
   `REQUIRED_BRIER_SKILL_VS_CLIMATOLOGY` joins it. A model must be calibrated
   *and* informative. This amends ADR-0004 kill criterion 2: beating naive alone
   is necessary, not sufficient.

5. **A case is scored only when both forecasters can produce a number**, so skill
   is always computed over an identical sample. Below the minimum resolved
   history the model declines to predict and the case is recorded as skipped with
   a reason, never silently dropped and never guessed.

6. **Slices are recorded; the gate reads the pooled number.** Per-symbol and
   per-interval skill is durable evidence and is surfaced, but gating on the
   thinnest slice would block promotion on noise. The hourly regime carries 354
   cases against 34,055 for the eight-hourly one.

7. **Estimates are Laplace-smoothed.** An unsmoothed frequency would claim
   certainty after an unbroken run and take an unbounded Brier penalty on the
   first surprise.

## What the research history says

The model does carry information: it beats climatology at every threshold
tested, pooled and for all 19 symbols, with ECE between 0.024 and 0.028.

It is not uniformly good, and the failures are concentrated where they are least
convenient:

| slice | n | vs naive | vs climatology |
|---|---|---|---|
| 8h @ 0 bps | 34,055 | +0.303 | +0.160 |
| 4h @ 0 bps | 13,869 | +0.291 | +0.198 |
| **1h @ 0 bps** | 354 | **−1.255** | +0.602 |
| **4h @ 1 bps** | 13,859 | **−0.079** | +0.655 |

The hourly regime is where the model loses hardest to naive, and an hourly
cadence is the venue reacting to extreme funding — the regime a carry strategy
most wants to be right about. The sample is small enough that this is a flag for
Phase 7, not a conclusion.

## Consequences

- Kill criterion 2 became harder to pass, deliberately, and the model still
  passes it on research data. That is the only order in which tightening a gate
  is honest.
- **None of this is promotion evidence.** Every number here comes from archive
  replay, which contributes zero under ADR-0012. The gates stay `UNAVAILABLE`
  until prospective paper evidence exists. Phase 4 answers "is the thesis worth
  continuing", not "may we trade".
- A second conditioning bit (funding magnitude, basis, open interest) now has a
  meaningful bar to clear: skill against climatology, not against naive.
- The 1h and 4h-at-1bps slices are recorded as the first places to look when
  prospective evidence starts arriving.
