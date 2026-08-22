# 25. Hedgeability caps the carry return, and the research universe hid it

- Status: Accepted
- Date: 2026-08-16
- **Corrects the return estimate in [ADR-0024](0024-pricing-liquidation-and-the-two-remedies-that-work.md)**
- Related: [ADR-0004](0004-funding-carry-edge-thesis.md),
  [ADR-0020](0020-research-backfill-and-funding-cadence-mutability.md),
  [ADR-0009](0009-liquidation-distance-invariant.md)

## Context

ADR-0024 concluded that even the working configurations returned about 1.5% a
year and were "not obviously a trade worth doing". That figure was challenged on
the grounds that it is implausibly low for a well-known trade, and the challenge
was correct: the number came from a research universe that cannot support it.

## What the research universe actually contained

Measured over `[2024-08-01, 2026-08-01)`, gross annualised funding for the 19
research symbols runs from **+6.50% (ORDI) to −15.30% (ACE)**, with six of the
nineteen negative. Buy-and-hold over the same window: ACE −97.9%, ALICE −89.8%,
AVAX −75.3%, ADA −56.9%. **The window is a deep bear market**, which is when
funding is thin, because funding is paid by crowded longs.

The universe was also selected on *data completeness* — every symbol needed two
full years of spot and USD-M history (ADR-0020). That is a proxy for age and
size, so it selected the oldest, largest, most efficiently arbitraged names and
excluded the entire high-funding cohort. STATUS limitations 5 and 6 already said
this dataset could not support venue-wide claims. ADR-0024 quoted a return figure
as though it could. That was the error.

## The structural finding

Surveying the live venue splits 826 USDT perpetuals by whether a spot market
exists to hedge against:

| cohort | n | median annualised | max | >20% | >50% |
|---|---|---|---|---|---|
| **Hedgeable** (spot exists) | 367 | **10.95%** | 79.7% | **5** | **1** |
| **Not hedgeable** (no spot) | 459 | 0.00% | 102.4% | **40** | **14** |

Of the top 40 symbols by funding, **only four have a spot pair at all**.

This is not a sampling accident. **A delta-neutral carry position requires a spot
leg, and the existence of that spot leg is exactly what allows everyone else to
arbitrage the funding down.** Where the trade is possible it is crowded; where
funding is rich, no spot market exists and the position could only be run as a
naked short — unbounded loss, which ADR-0009 forbids as the founding constraint.

Rich funding and hedgeability are close to mutually exclusive, and the mechanism
is the arbitrage itself working.

## Decision

1. **The realistic ceiling for this strategy is the hedgeable median, not the
   venue maximum.** At 10.95% gross on notional and a 1.75x capital multiple at
   2x leverage, that is roughly **6% a year on capital before costs** — with a
   handful of symbols currently offering 15–48%. It is not 1.5%, and it is not
   50%.
2. **The 1.5% figure is withdrawn.** It reflected a bear-market window and a
   universe selected against the opportunity. It should not be quoted.
3. **No conclusion about the strategy's viability may be drawn from the current
   research dataset.** It has the wrong symbols for the question.
4. **`funding-survey` makes the split reproducible** from the CLI rather than
   living in this document, so the numbers can be re-measured rather than
   believed.

## What this does and does not change

It does **not** rescue the trade from ADR-0022. The liquidation problem is
untouched: the hedgeable names are still shorted at 2x against a stress band that
a rally can exceed, and ADR-0024's remedies are still the only two that work.

It does mean the *size of the prize* was understated. Whether ~6–15% a year on
capital, with liquidation risk and an operator margin procedure, is worth
pursuing is a judgement about opportunity cost, not a measurement — but it is a
different judgement from the one 1.5% invited.

A return target of 50% a year is **not reachable** by this strategy as specified.
Reaching it would require naked shorts on the unhedgeable cohort (forbidden by
ADR-0009), materially higher leverage (the ADR-0022 liquidation), or the
unverified unified-collateral configuration — and even then only on the few
symbols where funding is briefly extreme.

## Consequences

- The research backfill should be **re-selected on funding richness among
  hedgeable symbols**, accepting shorter histories, before any further judgement.
  That is a Phase-3 revisit driven by a Phase-6 finding.
- A single funding reading is not a durable return. The survey is a snapshot, and
  high funding is frequently transient and mean-reverting; the current top of the
  hedgeable list is not a portfolio.
- ADR-0020's universe rationale was sound for *proving the pipeline* and wrong
  for *measuring the edge*. Those are different jobs and were conflated.
