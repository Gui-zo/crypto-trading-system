# 6. Local cron scheduling with durable run records

- Status: Accepted
- Date: 2026-08-09
- Mirrors: sibling ADR-0006
- Related: [ADR-0002](0002-simplified-local-first-stack.md),
  [ADR-0005](0005-eight-hour-decision-cadence.md)

## Context

At three decisions a day, a scheduler framework (Celery, Dramatiq, APScheduler as
a daemon) is overhead with its own failure modes. Plain cron is enough. But plain
cron is also famously silent: a job that stops running produces nothing, and
nothing is indistinguishable from "no news is good news".

## Decision

Cron invokes `scripts/cron-run.sh <command>`, which:

- resolves the repo root from its own location and `cd`s there, because cron
  gives a bare environment and no working directory;
- uses `.venv/bin/python` directly (no `uv` on cron's PATH, and the system Python
  is 3.11 with an incompatible pydantic);
- exports `TZ=UTC` ([ADR-0005](0005-eight-hour-decision-cadence.md));
- exports `PLATFORM_RUN_SOURCE=CRON`, which the JSON log formatter stamps on
  every record, so a scheduled run is distinguishable from an interactive one;
- takes a **per-command `flock`** so a slow run never overlaps the next tick;
- appends timestamped output to `data/logs/<command>.log`.

Separately, and this is the part that makes silence detectable: **every CLI
command writes a durable `operational_job_runs` row before it starts work and
closes it after.** The row is committed independently of the command's own
transaction, so a command that fails still leaves a FAILED row behind. A process
that is killed leaves a RUNNING row behind, and the watchdog
(`domain/operational_health.py`) reads stale RUNNING rows as evidence of a crash.

The watchdog requires **both** a healthy run history **and** a fresh collected
artifact. Neither a green process with no new data, nor fresh legacy data with a
failing process, can pass.

## Consequences

- Cron's silence is converted into positive evidence in a queryable table.
- Exactly one scheduler may run against a database. Two hosts writing the same
  database both append evidence and double-count accrual. This is an operational
  rule with no code enforcement yet; it is listed as a known limitation.
- Job-run rows accumulate. They are audit history and are never pruned by code;
  if the table ever needs trimming, that is a deliberate, documented action.
- Moving to a hosted runtime later is a change of invoker, not of instrumentation
  — the run table and watchdog policy are unaffected.
