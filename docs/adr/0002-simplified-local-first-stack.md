# 2. Simplified, local-first stack — and the latency premise it rests on

- Status: Accepted
- Date: 2026-08-09
- Mirrors: sibling ADR-0002, with the load-bearing premise restated explicitly
- Related: [ADR-0005](0005-eight-hour-decision-cadence.md) (why the premise holds)

## Context

The sibling repo justifies local-first Docker Compose, cron scheduling, no
message queue, and generous data-freshness tolerances with one sentence: *"it is
not latency-sensitive."* That single premise carries the entire operations layer.

Copying the stack without re-establishing the premise would be the most expensive
kind of mistake: everything would look reasonable and nothing would be justified.
So the premise is restated here, for this domain, as a first-class decision.

**The premise holds for funding carry.** Funding settles on a fixed schedule
(commonly every 8 hours on Binance USDⓈ-M, though the interval is per symbol and
must be read from the venue). The decision to open, hold, or unwind a carry
position is made on that cadence. A decision made 90 seconds later is essentially
the same decision.

**The premise would not hold for intraday.** That case is argued and rejected in
[ADR-0005](0005-eight-hour-decision-cadence.md).

Two further properties of this domain shape the decision:

1. **The hard problems are correctness problems** — instrument filters,
   maintenance-margin tiers, point-in-time funding history, two-leg ledger
   integrity — not operational infrastructure.
2. **The capital is at a venue.** Unlike the sibling project, whose capital was
   never anywhere, exchange counterparty risk is a real position here. That is a
   sizing concern, not a plumbing concern, but it argues against spending early
   effort on infrastructure instead of on the risk engine.

## Decision

1. **Local-first.** Development and early runtime use Docker Compose
   (Postgres 16 + Redis 7) on the host. Ports are offset from the sibling
   project's (5433/6380) so both stacks run side by side. Cloud infrastructure is
   deferred until 24/7 paper/shadow runtime is genuinely needed, and even then a
   single small VM is the first step.
2. **Cron, not a task queue.** At three decisions a day, a scheduler framework
   would be pure overhead. `scripts/cron-run.sh` takes a per-command lock and
   forces UTC.
3. **Free data first.** `data.binance.vision` bulk archive plus unauthenticated
   REST market data. Account state needs keys; those arrive with the phase that
   needs them, read-only and IP-restricted.
4. **Secrets behind an abstraction.** All secret access goes through
   `SecretProvider`. Binance HMAC secrets additionally go through
   `FileSecretProvider`, which reads a path rather than a value and refuses a
   group- or world-readable file.
5. **Packaging:** a single uv-managed repo. Domain code lives under `packages/*`
   as top-level importable packages, resolved via pytest `pythonpath` rather than
   an editable install, so new packages need no build-config changes.

**Freshness tolerances do *not* carry over.** The sibling's 90-minute quote
tolerance is safe for daily weather markets and lethal here. See
[ADR-0007](0007-fail-closed-instrument-lifecycle-and-freshness.md).

## Consequences

- Fastest credible path to paper trading; effort goes to the risk engine and data
  correctness rather than cloud plumbing.
- A later productionization phase must reintroduce the deferred infrastructure
  before real capital. This is explicit, planned debt.
- **If the cadence premise is ever revisited, this ADR and every tolerance
  downstream of it must be revisited together.** They are one decision, not
  several.
