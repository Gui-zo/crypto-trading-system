# 5. An 8-hour decision cadence; intraday explicitly rejected

- Status: Accepted
- Date: 2026-08-09
- Related: [ADR-0002](0002-simplified-local-first-stack.md) (the premise this
  protects), [ADR-0007](0007-fail-closed-instrument-lifecycle-and-freshness.md)

## Context

This is the single highest-leverage architectural decision in the project, so it
is recorded before any code rather than discovered later.

[ADR-0002](0002-simplified-local-first-stack.md) rests entirely on the premise
that the domain is not latency-sensitive. Whether that premise survives is
decided here, by choosing a decision cadence.

## Decision

**Decisions are made on the funding cadence** — commonly every 8 hours at
00:00 / 08:00 / 16:00 UTC on Binance USDⓈ-M, read per symbol from the venue and
never assumed ([ADR-0003](0003-binance-schemas-synthetic-until-recorded.md)).

**Intraday and HFT are rejected**, for two independent reasons, either of which
alone would be sufficient:

1. **It destroys the operations premise.** Seconds-scale freshness, a persistent
   WebSocket consumer running as a supervised daemon, real reconnect and
   sequence-gap handling, queue position, adverse selection — all the
   infrastructure debt ADR-0002 deliberately deferred comes due at once.
2. **It is a worse market.** At intraday horizons we would be competing with
   firms colocated in Binance's datacenter. It is a much larger build for a
   structurally weaker position.

Everything is anchored to **UTC**: the host clock, the container timezone, the
daily loss window, and every stored timestamp. Funding settles on UTC boundaries,
and a host-local daily window would silently straddle two funding days.

## Consequences

- Cron remains viable. Local-first remains viable. No queue is needed. Roughly
  the whole operations layer of the sibling repo ports across.
- **Freshness tolerances still tighten by orders of magnitude.** An 8-hour
  decision cadence does not excuse a stale price at the moment of decision, and
  it certainly does not excuse one at the moment of order submission. The
  cadence governs *how often we decide*, not *how old the inputs may be*. See
  [ADR-0007](0007-fail-closed-instrument-lifecycle-and-freshness.md).
- Position lifetime is decision-driven, not calendar-driven: markets are 24/7 and
  perpetuals have no expiry, so there is no natural daily reset to lean on.
- Funding accrues every interval whether or not anyone is looking, so the ledger
  must accrue on schedule rather than on decision.
- Revisiting this decision means revisiting ADR-0002 and every freshness
  tolerance with it.
