# 1. Record architecture decisions

- Status: Accepted
- Date: 2026-08-09

## Context

This is a long-lived, safety-critical system built over many phases, by a single
operator working with AI assistants across sessions that do not share memory.
Decisions about architecture, scope, and deviations from the founding
specification need a durable, reviewable record so future work (and future
readers, human or otherwise) can understand *why* the system is shaped the way it
is, not just *what* it does.

The sibling [`automated-trading-system`](../../../automated-trading-system)
repository proved this out over 27 ADRs. Its ADR-0005 (live reconciliation
findings) turned out to be the single most load-bearing document in that repo,
precisely because it is where documentation met reality.

## Decision

We record architecture decisions using lightweight ADRs (the Michael Nygard
format). Each ADR is an immutable, numbered Markdown file under `docs/adr/`.
Superseding an ADR means adding a new one that references the old, not editing
history.

An ADR captures: **Context** (the forces at play), **Decision** (what we chose),
and **Consequences** (what follows, good and bad).

Where a decision is the *same* decision the sibling repo made, we reuse its
number so the two repos stay mutually legible. Where the decision differs, the
ADR says so explicitly and explains why the sibling's reasoning does not carry.

## Consequences

- Significant choices are discoverable in one place and survive tooling changes,
  context resets, and the gap between sessions.
- There is a small, worthwhile cost to writing an ADR for each material decision.
- The `docs/adr/` directory is the canonical source for "why is it like this?".
- Numbering overlap with the sibling repo is deliberate for shared decisions and
  a hazard for divergent ones, so every ADR here states its relationship.
