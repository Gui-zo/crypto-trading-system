"""Platform CLI — the operational entrypoint for every command.

    uv run python apps/cli/main.py status
    uv run python apps/cli/main.py safety-status
    uv run python apps/cli/main.py promotion-status

Two rules hold for every command here and are the reason this file owns the
session rather than the repositories:

* **The command owns the transaction.** Repositories flush; exactly one commit
  happens at the end of a successful command, so a crash halfway through leaves
  no partial audit trail.
* **Every command records a durable run.** The ``operational_job_runs`` row is
  written *before* the work starts and closed after, so a process that dies
  leaves RUNNING residue for the watchdog to find. That is the Phase-0 audit
  control, and it is why there is no "just run it quickly" path.

Phase 0 exposes the governance surface only. There is no venue client, no market
data, and no code path that can submit an order — see docs/STATUS.md for what
each later phase adds.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import pathlib
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from datetime import UTC, datetime

# Make the `packages/` code importable when run as a plain script.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "packages"))

from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from config.settings import Settings, load_settings  # noqa: E402
from db.engine import create_engine, create_session_factory  # noqa: E402
from db.operational_health_repo import OperationalHealthRepository  # noqa: E402
from db.safety_repo import SafetyControlRepository  # noqa: E402
from domain.modes import TradingMode, is_live, permits_new_orders  # noqa: E402
from domain.operational_health import OperationalJobStatus  # noqa: E402
from domain.promotion import (  # noqa: E402
    MAX_LIQUIDATION_INVARIANT_VIOLATIONS,
    MAX_RECONCILIATION_DISCREPANCIES,
    REQUIRED_BRIER_SKILL_VS_NAIVE,
    REQUIRED_NET_CARRY_VS_BENCHMARK_BPS,
    REQUIRED_PROSPECTIVE_PAPER_DAYS,
    REQUIRED_PROSPECTIVE_SETTLEMENTS,
    AccrualGate,
    CeilingGate,
    Gate,
    GateStatus,
    binding_gate,
    wall_clock_gate,
)
from domain.safety import (  # noqa: E402
    SafetyControlAction,
    SafetyScope,
)
from observability.logging import configure_logging  # noqa: E402

log = logging.getLogger("cli")

#: Scheduled producers whose health the watchdog evaluates. Empty in Phase 0 —
#: no collector exists yet — and deliberately not stubbed: a fabricated signal
#: that always passes is worse than an honest "nothing to watch". Phase 1
#: registers `record-funding` and `record-prices` here.
SCHEDULED_PRODUCERS: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Session / run plumbing
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _session(settings: Settings) -> AsyncIterator[AsyncSession]:
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


async def _run_audited(
    settings: Settings,
    *,
    job_name: str,
    argv: list[str],
    body: Callable[[AsyncSession], Awaitable[int]],
) -> int:
    """Execute ``body`` inside one transaction, bracketed by a durable run row.

    The run row is committed separately from the body's work. That is deliberate:
    if the body raises, its changes roll back but the FAILED run row must survive,
    because a failure that leaves no trace is the failure mode this table exists
    to prevent.
    """
    started_at = datetime.now(UTC)
    async with _session(settings) as session:
        health = OperationalHealthRepository(session, environment=settings.binance_env.value)
        run = await health.start_run(
            job_name=job_name,
            command=argv,
            source=settings.app_env.value.upper(),
            started_at=started_at,
        )
        await session.commit()
        run_id: uuid.UUID = run.run_id

    exit_code = 1
    error_type: str | None = None
    error_message: str | None = None
    try:
        async with _session(settings) as session:
            exit_code = await body(session)
            await session.commit()
    except Exception as exc:
        error_type = type(exc).__name__
        error_message = str(exc)
        log.exception("command failed", extra={"job_name": job_name})
    finally:
        async with _session(settings) as session:
            health = OperationalHealthRepository(session, environment=settings.binance_env.value)
            await health.finish_run(
                run_id,
                status=(
                    OperationalJobStatus.SUCCEEDED
                    if exit_code == 0 and error_type is None
                    else OperationalJobStatus.FAILED
                ),
                exit_code=exit_code if error_type is None else 1,
                finished_at=datetime.now(UTC),
                error_type=error_type,
                error_message=error_message,
            )
            await session.commit()
    return exit_code if error_type is None else 1


def _print(payload: object, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, default=str, indent=2))
    else:
        print(payload)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


async def cmd_status(settings: Settings, args: argparse.Namespace) -> int:
    async with _session(settings) as session:
        try:
            await session.execute(text("SELECT 1"))
            db_reachable = True
            migration = (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
        except Exception as exc:
            db_reachable = False
            migration = None
            log.warning("database unreachable", extra={"error": str(exc)})

    report = {
        "app_env": settings.app_env.value,
        "trading_mode": settings.trading_mode.value,
        "binance_env": settings.binance_env.value,
        "database_reachable": db_reachable,
        "migration_head": migration,
        "raw_store_backend": settings.raw_store_backend.value,
        "permits_new_orders": permits_new_orders(settings.trading_mode),
        "submits_real_orders": is_live(settings.trading_mode),
        "order_path_exists": False,
        "utc_now": datetime.now(UTC).isoformat(),
    }
    if args.json:
        _print(report, as_json=True)
    else:
        for key, value in report.items():
            print(f"{key:24} {value}")
        if not db_reachable:
            print("\nhint: docker compose up -d && uv run alembic upgrade head")
    return 0 if db_reachable else 1


# ---------------------------------------------------------------------------
# safety-*
# ---------------------------------------------------------------------------


async def cmd_safety_status(settings: Settings, args: argparse.Namespace) -> int:
    async with _session(settings) as session:
        repo = SafetyControlRepository(session, environment=settings.binance_env.value)
        states = await repo.current_states(active_only=args.active_only)
        total = await repo.event_count()

    rows = [
        {
            "scope": state.scope.value,
            "key": state.scope_key,
            "action": state.action.value,
            "reason": state.reason,
            "sequence": state.sequence_number,
        }
        for state in states
    ]
    if args.json:
        _print({"environment": settings.binance_env.value, "events": total, "controls": rows},
               as_json=True)
    else:
        print(f"environment {settings.binance_env.value}   events {total}")
        if not rows:
            print("no safety controls recorded")
        for row in rows:
            flag = "HALTED " if row["action"] == "ACTIVATE" else "clear  "
            print(f"{flag} {row['scope']}:{row['key']}  seq={row['sequence']}  {row['reason']}")
    return 0


def _safety_mutation(action: SafetyControlAction) -> Callable[..., Awaitable[int]]:
    async def run(settings: Settings, args: argparse.Namespace) -> int:
        scope = SafetyScope(args.scope.upper())

        async def body(session: AsyncSession) -> int:
            repo = SafetyControlRepository(session, environment=settings.binance_env.value)
            state = await repo.set_control(
                scope=scope,
                scope_key=args.key,
                action=action,
                reason=args.reason,
                actor=args.actor,
                source="CLI",
                automatic=False,
            )
            print(
                f"{action.value} {state.scope.value}:{state.scope_key} "
                f"seq={state.sequence_number} event={state.event_id}"
            )
            return 0

        return await _run_audited(
            settings,
            job_name=f"safety-{action.value.lower()}",
            argv=sys.argv[1:],
            body=body,
        )

    return run


# ---------------------------------------------------------------------------
# health-*
# ---------------------------------------------------------------------------


async def cmd_health_check(settings: Settings, args: argparse.Namespace) -> int:
    if not SCHEDULED_PRODUCERS:
        # Honest UNAVAILABLE rather than a fabricated always-passing signal.
        print("UNAVAILABLE  no scheduled producers exist yet (Phase 0); nothing to watch")
        print("             the run history behind this command is live — see health-status")
        return 0

    async def body(session: AsyncSession) -> int:  # pragma: no cover - Phase 1
        raise NotImplementedError("register producers in SCHEDULED_PRODUCERS first")

    return await _run_audited(settings, job_name="health-check", argv=sys.argv[1:], body=body)


async def cmd_health_status(settings: Settings, args: argparse.Namespace) -> int:
    async with _session(settings) as session:
        repo = OperationalHealthRepository(session, environment=settings.binance_env.value)
        summary = await repo.summary()
        runs = await repo.recent_runs(limit=args.limit)

    if args.json:
        _print(
            {
                "environment": settings.binance_env.value,
                "job_runs": summary.job_runs,
                "running": summary.running,
                "succeeded": summary.succeeded,
                "failed": summary.failed,
                "assessments": summary.assessments,
                "latest_status": (
                    summary.latest_status.value if summary.latest_status is not None else None
                ),
                "recent": [
                    {
                        "job": run.job_name,
                        "status": run.status.value,
                        "started_at": run.started_at,
                        "finished_at": run.finished_at,
                        "exit_code": run.exit_code,
                        "error": run.error_message,
                    }
                    for run in runs
                ],
            },
            as_json=True,
        )
        return 0

    print(
        f"runs {summary.job_runs}  succeeded {summary.succeeded}  "
        f"failed {summary.failed}  running {summary.running}"
    )
    print(f"assessments {summary.assessments}  latest {summary.latest_status or 'none'}")
    for run in runs:
        stamp = run.started_at.isoformat(timespec="seconds")
        detail = f"  {run.error_type}: {run.error_message}" if run.error_message else ""
        print(f"  {stamp}  {run.job_name:20} {run.status.value:10}{detail}")
    return 0


# ---------------------------------------------------------------------------
# promotion-status
# ---------------------------------------------------------------------------


def _phase0_gates() -> list[Gate]:
    """The promotion gates with the evidence that exists today — which is none.

    Rendered from zero rather than hidden until Phase 7 on purpose: the shape of
    these gates (prospective wall-clock, backtests worth nothing) is the decision
    most likely to be quietly softened later, so it is visible from commit one.

    Every gate is constructed with ``has_evidence=False``, so every one reports
    UNAVAILABLE. That is the correct Phase-0 reading and it is not cosmetic: with
    the naive construction, "net carry 0 bps >= threshold 0" and "zero
    liquidation violations <= limit 0" both render as PASS on a system that has
    never traded.
    """
    no_evidence_yet = False
    return [
        wall_clock_gate(
            "prospective_paper_days",
            "consecutive prospective paper days",
            observed_days=0,
            required_days=REQUIRED_PROSPECTIVE_PAPER_DAYS,
            campaign_running=no_evidence_yet,
        ),
        AccrualGate(
            key="prospective_settlements",
            label="funding settlements observed prospectively",
            observed=0.0,
            required=float(REQUIRED_PROSPECTIVE_SETTLEMENTS),
            daily_rate=None,
            unit="settlements",
            has_evidence=no_evidence_yet,
        ),
        AccrualGate(
            key="net_carry_vs_benchmark",
            label="net carry vs USDT-hold benchmark",
            observed=0.0,
            required=float(REQUIRED_NET_CARRY_VS_BENCHMARK_BPS),
            daily_rate=None,
            unit="bps",
            has_evidence=no_evidence_yet,
            strict=True,
        ),
        AccrualGate(
            key="brier_skill_vs_naive",
            label="funding-persistence Brier skill vs naive",
            observed=0.0,
            required=float(REQUIRED_BRIER_SKILL_VS_NAIVE),
            daily_rate=None,
            unit="skill",
            has_evidence=no_evidence_yet,
            strict=True,
        ),
        CeilingGate(
            key="reconciliation_discrepancies",
            label="unexplained reconciliation discrepancies",
            observed=0.0,
            limit=float(MAX_RECONCILIATION_DISCREPANCIES),
            has_evidence=no_evidence_yet,
        ),
        CeilingGate(
            key="liquidation_invariant_violations",
            label="liquidation-distance invariant violations",
            observed=0.0,
            limit=float(MAX_LIQUIDATION_INVARIANT_VIOLATIONS),
            has_evidence=no_evidence_yet,
        ),
    ]


async def cmd_promotion_status(settings: Settings, args: argparse.Namespace) -> int:
    gates = _phase0_gates()
    binding = binding_gate(gates)

    if args.json:
        _print(
            {
                "mode": settings.trading_mode.value,
                "gates": [
                    {
                        "key": gate.key,
                        "label": gate.label,
                        "observed": gate.observed,
                        "threshold": (
                            gate.required if isinstance(gate, AccrualGate) else gate.limit
                        ),
                        "status": gate.status.value,
                        "projected_days": gate.projected_days,
                    }
                    for gate in gates
                ],
                "binding_constraint": binding.key if binding is not None else None,
            },
            as_json=True,
        )
        return 0

    print(f"mode {settings.trading_mode.value}   (promotion is always a human decision)")
    for gate in gates:
        if isinstance(gate, AccrualGate):
            threshold, comparator = gate.required, (">" if gate.strict else ">=")
        else:
            threshold, comparator = gate.limit, "<="
        marker = {
            GateStatus.PASS: "PASS   ",
            GateStatus.ACCRUING: "ACCRUE ",
            GateStatus.STALLED: "STALLED",
            GateStatus.FAILED: "FAILED ",
            GateStatus.UNAVAILABLE: "UNAVAIL",
        }[gate.status]
        print(f"{marker} {gate.label:48} {gate.observed:g} {comparator} {threshold:g}")
    print(f"\nbinding constraint: {binding.key if binding is not None else 'none'}")
    print("backtests contribute zero days to any gate here — see docs/adr/0012")
    return 0


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------


async def cmd_dashboard(settings: Settings, args: argparse.Namespace) -> int:
    """The operator view. Always prefer this to a number written in a document."""
    print("=" * 72)
    print(f"crypto-trading-system   mode={settings.trading_mode.value}   "
          f"env={settings.binance_env.value}")
    print("=" * 72)
    print("\n[status]")
    await cmd_status(settings, argparse.Namespace(json=False))
    print("\n[safety]")
    await cmd_safety_status(settings, argparse.Namespace(json=False, active_only=False))
    print("\n[operational health]")
    await cmd_health_status(settings, argparse.Namespace(json=False, limit=5))
    print("\n[promotion gates]")
    await cmd_promotion_status(settings, argparse.Namespace(json=False))
    return 0


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crypto-trading-system",
        description="Operational CLI. Phase 0: governance surface only.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="settings, connectivity, and mode")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    safety_status = sub.add_parser("safety-status", help="current scoped kill switches")
    safety_status.add_argument("--json", action="store_true")
    safety_status.add_argument("--active-only", action="store_true")
    safety_status.set_defaults(func=cmd_safety_status)

    for name, action in (
        ("safety-halt", SafetyControlAction.ACTIVATE),
        ("safety-clear", SafetyControlAction.CLEAR),
    ):
        cmd = sub.add_parser(name, help=f"append a {action.value} control event")
        cmd.add_argument(
            "--scope",
            required=True,
            choices=[scope.value for scope in SafetyScope],
        )
        cmd.add_argument("--key", required=True, help="scope key ('*' for GLOBAL)")
        cmd.add_argument("--reason", required=True)
        cmd.add_argument("--actor", required=True, help="who is accountable for this")
        cmd.set_defaults(func=_safety_mutation(action))

    health_check = sub.add_parser("health-check", help="evaluate scheduled-producer health")
    health_check.set_defaults(func=cmd_health_check)

    health_status = sub.add_parser("health-status", help="durable run history and verdicts")
    health_status.add_argument("--json", action="store_true")
    health_status.add_argument("--limit", type=int, default=20)
    health_status.set_defaults(func=cmd_health_status)

    promotion = sub.add_parser("promotion-status", help="promotion gates and binding constraint")
    promotion.add_argument("--json", action="store_true")
    promotion.set_defaults(func=cmd_promotion_status)

    dashboard = sub.add_parser("dashboard", help="the operator view; read this first")
    dashboard.set_defaults(func=cmd_dashboard)

    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    settings = load_settings()
    configure_logging(level=settings.log_level, environment=settings.binance_env.value)

    args = build_parser().parse_args(argv)

    if settings.trading_mode is TradingMode.HALTED:
        log.warning("platform is HALTED; read-only commands only")

    handler: Callable[[Settings, argparse.Namespace], Coroutine[None, None, int]] = args.func
    return asyncio.run(handler(settings, args))


if __name__ == "__main__":
    raise SystemExit(main())
