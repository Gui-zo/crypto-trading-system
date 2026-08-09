# 10. Venue-environment scoping from day one

- Status: Accepted
- Date: 2026-08-09
- Mirrors: sibling ADR-0027, adopted at commit one instead of as a retrofit
- **Amended 2026-08-09** after live measurement; see "Correction" below

## Context

Binance reuses symbols across environments. `BTCUSDT` exists on testnet and on
production, with the same name and independently determined prices.

An unscoped row keyed by symbol is therefore **ambiguous**, and a series that
mixes environments is **silently corrupt**: it looks like a price series, it
parses, it charts, and every statistic computed from it is meaningless. Nothing
about it fails loudly.

### Correction

This ADR originally justified scoping on the grounds that testnet prices "can
differ from production by orders of magnitude". **That was wrong.** Measured at
the same instant on 2026-08-09, BTCUSDT was 65100.70 on production and 65102.39
on testnet — a 0.003% difference
([ADR-0015](0015-binance-live-reconciliation-findings.md), finding 3).

The correction strengthens the decision rather than weakening it. A wildly wrong
price is caught by the first person who looks at a chart. A *plausibly similar*
one never is — the mixed series looks entirely normal, and every number derived
from it is quietly wrong. **Scoping cannot rely on anyone noticing, which is
exactly why it has to be structural.**

The same measurement found the environments differ in ways that do not show up in
a price at all: production listed 854 symbols to testnet's 731, and production's
request-weight ceiling is 2400/minute against testnet's 6000. A strategy tuned on
testnet assumes symbols that do not exist and 2.5× the request headroom it has.

The sibling repo hit exactly this corruption with Kalshi's demo and production
environments and had to retrofit an `environment` column across every market-keyed
table, backfill it, and re-scope every read. That retrofit is the single most
avoidable piece of work in that repo's history — it was cheap to do on day one
and expensive to do on day two hundred.

## Decision

**Every market-keyed row carries an `environment` column, and every
symbol-resolving read filters on it.** From the first migration. This includes
rows that are not obviously market data:

- **Safety controls.** A testnet halt must not silence production, and vice
  versa. `safety_control_events` carries `environment`, and current kill-switch
  state resolves per `(environment, scope_type, scope_key)`.
- **Operational health assessments**, for the same reason.
- **Raw-store keys**, which embed the environment in the key path, so a testnet
  payload can never be replayed as a production one.

The set of legal values is enforced by a database `CHECK` constraint
(`'testnet' | 'production'`), not only by application code, so a typo fails at
write time rather than at analysis time.

At the decision layer, `domain/safety.py` carries a `VENUE_ENVIRONMENT_SCOPE`
check that blocks when the environment the inputs came from differs from the
environment being traded. A blank trading environment blocks too, rather than
matching a blank data environment.

Credentials are strictly separate per environment and are never reused.

## Consequences

- Every table added in any later phase must carry `environment`. This is a
  standing rule, not a per-table decision, and reviews should treat a missing
  `environment` column as a defect.
- Queries are slightly more verbose. That is the entire cost.
- Switching environments is a configuration change (`BINANCE_ENV`) that
  automatically re-scopes reads, rather than a data migration.
- **Testnet evidence is never production evidence.** Environment scoping is what
  makes that distinction mechanical rather than a matter of remembering; see
  [ADR-0012](0012-prospective-only-promotion-gates.md).
