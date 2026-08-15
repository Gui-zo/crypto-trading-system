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

Phases 1-2 add read-only venue access and immutable instrument catalogs. There is
still no market-data persistence and no code path that can submit an order — see
docs/STATUS.md for the current boundary.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import pathlib
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

# Make the `packages/` code importable when run as a plain script.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "packages"))

from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from config.secrets import EnvSecretProvider, FileSecretProvider  # noqa: E402
from config.settings import Settings, load_settings  # noqa: E402
from db.engine import create_engine, create_session_factory  # noqa: E402
from db.instrument_repo import (  # noqa: E402
    CatalogSourceArtifact,
    InstrumentCatalogRepository,
)
from db.market_data_repo import (  # noqa: E402
    DataQualityStatus,
    MarketDataArtifact,
    MarketDataRepository,
)
from db.model_repo import ModelRepository  # noqa: E402
from db.operational_health_repo import OperationalHealthRepository  # noqa: E402
from db.safety_repo import SafetyControlRepository  # noqa: E402
from domain.funding_model import (  # noqa: E402
    ExpandingPersistenceModel,
    FundingTarget,
    ResolvedCase,
    ScoredCase,
    Settlement,
    SkipReason,
    WalkForward,
    build_cases,
    score,
    score_by_interval,
    score_by_symbol,
    walk_forward,
)
from domain.instrument import InstrumentReviewAction, VenueEnvironment  # noqa: E402
from domain.market_data import MarketDataSource  # noqa: E402
from domain.modes import TradingMode, is_live, permits_new_orders  # noqa: E402
from domain.operational_health import OperationalJobStatus  # noqa: E402
from domain.precision import parse_decimal  # noqa: E402
from domain.promotion import (  # noqa: E402
    MAX_LIQUIDATION_INVARIANT_VIOLATIONS,
    MAX_RECONCILIATION_DISCREPANCIES,
    REQUIRED_BRIER_SKILL_VS_CLIMATOLOGY,
    REQUIRED_BRIER_SKILL_VS_NAIVE,
    REQUIRED_NET_CARRY_VS_BENCHMARK_BPS,
    REQUIRED_PROSPECTIVE_PAPER_DAYS,
    REQUIRED_PROSPECTIVE_SETTLEMENTS,
    AccrualGate,
    CeilingGate,
    EvidenceSource,
    Gate,
    GateStatus,
    binding_gate,
    wall_clock_gate,
)
from domain.safety import (  # noqa: E402
    SafetyControlAction,
    SafetyGateStatus,
    SafetyScope,
    SafetyScopeRef,
)
from modeling.provenance import capture, snapshot_observations  # noqa: E402
from observability.logging import configure_logging  # noqa: E402
from storage.raw_store import create_raw_store  # noqa: E402
from venue_binance.archive import (  # noqa: E402
    ArchiveDataset,
    ArchivePayload,
    BinanceArchiveClient,
    monthly_requests,
    parse_funding_csv,
    parse_kline_csv,
)
from venue_binance.auth import BinanceSigner  # noqa: E402
from venue_binance.client import BinanceRestClient, create_http_client  # noqa: E402
from venue_binance.endpoints import Market  # noqa: E402
from venue_binance.errors import BinanceError  # noqa: E402
from venue_binance.mapping import is_carry_candidate, to_instrument_catalog  # noqa: E402

log = logging.getLogger("cli")

#: Phase-3 persistent collectors evaluated by the watchdog.
SCHEDULED_PRODUCERS: tuple[str, ...] = ("record-funding", "record-prices")


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
        _print(
            {"environment": settings.binance_env.value, "events": total, "controls": rows},
            as_json=True,
        )
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

    async def body(session: AsyncSession) -> int:
        evaluated_at = datetime.now(UTC)
        market_data = MarketDataRepository(session, environment=settings.binance_env.value)
        health = OperationalHealthRepository(session, environment=settings.binance_env.value)
        funding = await health.build_signal(
            name="funding-series",
            job_name="record-funding",
            scope_ref=SafetyScopeRef(SafetyScope.DATA_PROVIDER, "BINANCE_FUNDING"),
            evaluated_at=evaluated_at,
            artifact_at=await market_data.latest_funding_at(),
            maximum_age=timedelta(hours=1),
            failure_threshold=3,
            maximum_runtime=timedelta(minutes=20),
        )
        prices = await health.build_signal(
            name="price-series",
            job_name="record-prices",
            scope_ref=SafetyScopeRef(SafetyScope.DATA_PROVIDER, "BINANCE_PRICES"),
            evaluated_at=evaluated_at,
            artifact_at=await market_data.latest_prices_at(),
            maximum_age=timedelta(minutes=5),
            failure_threshold=3,
            maximum_runtime=timedelta(minutes=20),
        )
        assessment = await health.evaluate_and_record(
            signals=(funding, prices),
            evaluated_at=evaluated_at,
            trigger_job_run_id=None,
        )
        print(f"{assessment.status.value}  checks={len(assessment.checks)}")
        for check in assessment.checks:
            print(f"  {check.status.value:5} {check.name}  {check.detail}")
        return 0 if assessment.status is SafetyGateStatus.PASS else 1

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
# binance-*
# ---------------------------------------------------------------------------


def _venue_environment(settings: Settings) -> VenueEnvironment:
    return VenueEnvironment(settings.binance_env.value)


async def cmd_binance_status(settings: Settings, args: argparse.Namespace) -> int:
    """Connectivity, clock drift, and rate-limit budget. Read-only, no key needed.

    Clock drift is reported because it is load-bearing twice over: a signed
    request outside ``recvWindow`` is rejected outright, and funding settles on
    UTC boundaries, so a drifting host mis-attributes settlements.
    """
    environment = _venue_environment(settings)
    async with create_http_client() as http:
        api = BinanceRestClient(environment=environment, http=http)
        try:
            drift_seconds, server_time = await api.clock_drift()
            info = await api.exchange_info()
            reachable = True
            error: str | None = None
        except BinanceError as exc:
            drift_seconds, server_time, info = 0.0, None, None
            reachable = False
            error = str(exc)

    tradeable = (
        sum(1 for symbol in info.value.symbols if is_carry_candidate(symbol))
        if info is not None
        else 0
    )
    report = {
        "environment": environment.value,
        "reachable": reachable,
        "error": error,
        "server_time": server_time.isoformat() if server_time else None,
        "clock_drift_seconds": round(drift_seconds, 3),
        "weight_limit_per_minute": api.budget.limit,
        "weight_used": api.budget.snapshot().used,
        "symbols_total": len(info.value.symbols) if info is not None else 0,
        "carry_candidates": tradeable,
    }
    if args.json:
        _print(report, as_json=True)
    else:
        for key, value in report.items():
            print(f"{key:26} {value}")
        if abs(drift_seconds) > 1.0:
            print("\nWARNING: clock drift above 1s — funding settles on UTC boundaries")
    return 0 if reachable else 1


#: Public, unauthenticated endpoints the snapshot records. Deliberately a fixed
#: list rather than a free-form argument: this command exists to refresh the
#: fixture corpus reproducibly, and an operator-supplied endpoint would make the
#: recording unreproducible.
_SNAPSHOT_SYMBOL = "BTCUSDT"


async def cmd_binance_snapshot(settings: Settings, args: argparse.Namespace) -> int:
    """Fetch public market data and retain every raw byte (ADR-0003).

    This is how the recorded corpus in `tests/fixtures/binance/recorded/` is
    refreshed, and how a schema drift is detected: fetch, retain, diff.
    """
    environment = _venue_environment(settings)
    settings_for_store = settings
    if args.out:
        settings_for_store = settings.model_copy(update={"raw_store_local_dir": args.out})
    store = create_raw_store(settings_for_store)

    async def body(session: AsyncSession) -> int:
        async with create_http_client() as http:
            api = BinanceRestClient(environment=environment, http=http, raw_store=store)
            records = []
            for label, coroutine in (
                ("exchangeInfo", api.exchange_info()),
                ("fundingInfo", api.funding_info()),
                ("premiumIndex", api.mark_price(_SNAPSHOT_SYMBOL)),
                ("fundingRate", api.funding_history(_SNAPSHOT_SYMBOL, limit=10)),
                ("bookTicker", api.book_ticker(_SNAPSHOT_SYMBOL, Market.USDM)),
                ("klines", api.klines(_SNAPSHOT_SYMBOL, interval="8h", limit=3)),
            ):
                response = await coroutine
                if response.raw is not None:
                    records.append((label, response.raw))
                    print(f"{label:16} {response.raw.size:>8} bytes  {response.raw.key}")
            weight = api.budget.snapshot()
            print(f"\nweight used {weight.used}/{weight.limit} ({weight.utilisation:.0%})")
            log.info(
                "binance snapshot complete",
                extra={"environment": environment.value, "objects": len(records)},
            )
        return 0

    return await _run_audited(settings, job_name="binance-snapshot", argv=sys.argv[1:], body=body)


# ---------------------------------------------------------------------------
# instrument catalog
# ---------------------------------------------------------------------------


def _read_only_binance_signer() -> BinanceSigner:
    api_key = EnvSecretProvider().require_secret("BINANCE_API_KEY_ID")
    secret = FileSecretProvider().require_secret("BINANCE_API_SECRET_PATH")
    return BinanceSigner(api_key, secret)


def _catalog_source(endpoint: str, response: object) -> CatalogSourceArtifact:
    raw = getattr(response, "raw", None)
    if raw is None:
        raise RuntimeError(f"{endpoint}: instrument sync requires raw-payload retention")
    return CatalogSourceArtifact(
        endpoint=endpoint,
        key=raw.key,
        sha256=raw.sha256,
        size=raw.size,
        fetched_at=raw.fetched_at,
    )


async def cmd_sync_instruments(settings: Settings, args: argparse.Namespace) -> int:
    """Fetch and version every input needed to size a futures instrument."""
    environment = _venue_environment(settings)

    async def body(session: AsyncSession) -> int:
        store = create_raw_store(settings)
        signer = _read_only_binance_signer()
        async with create_http_client() as http:
            api = BinanceRestClient(
                environment=environment,
                http=http,
                raw_store=store,
                signer=signer,
            )
            exchange = await api.exchange_info()
            funding = await api.funding_info()
            brackets = await api.margin_brackets()

        catalog = to_instrument_catalog(
            exchange.value,
            funding.value,
            brackets.value,
            scope=api.scope,
        )
        sources = (
            _catalog_source("exchangeInfo", exchange),
            _catalog_source("fundingInfo", funding),
            _catalog_source("leverageBracket", brackets),
        )
        observed_at = max(source.fetched_at for source in sources)
        repo = InstrumentCatalogRepository(session, environment=environment.value)
        result = await repo.record_catalog(
            catalog,
            sources=sources,
            observed_at=observed_at,
        )

        change = "CHANGED" if result.changed_from_previous else "unchanged"
        novelty = "new version" if result.created_new_version else "known version"
        print(f"environment       {environment.value}")
        print(f"catalog_sha256    {result.status.content_sha256}")
        print(f"review_status     {result.status.review_status.value}")
        print(f"version           {novelty}; {change}")
        print(f"venue_symbols     {result.status.total_symbols}")
        print(f"carry_candidates  {result.status.candidate_symbols}")
        print(f"complete_specs    {result.status.instrument_count}")
        print(f"excluded          {result.status.excluded_count}")
        if result.status.review_status.value == "PENDING_REVIEW":
            print("\nPENDING_REVIEW: sizing must remain blocked until this exact hash is reviewed")
        return 0

    return await _run_audited(
        settings,
        job_name="sync-instruments",
        argv=sys.argv[1:],
        body=body,
    )


def _exclusion_summary(catalog: dict[str, object]) -> dict[str, int]:
    summary: dict[str, int] = {}
    exclusions = catalog.get("exclusions", [])
    if not isinstance(exclusions, list):
        return summary
    for exclusion in exclusions:
        if not isinstance(exclusion, dict):
            continue
        reasons = exclusion.get("reasons", [])
        if not isinstance(reasons, list):
            continue
        for reason in reasons:
            name = str(reason)
            summary[name] = summary.get(name, 0) + 1
    return dict(sorted(summary.items()))


async def cmd_instrument_status(settings: Settings, args: argparse.Namespace) -> int:
    async with _session(settings) as session:
        repo = InstrumentCatalogRepository(session, environment=settings.binance_env.value)
        status = await repo.current_status()
        versions, observations, reviews = await repo.counts()
    report: dict[str, object]
    if status is None:
        report = {
            "environment": settings.binance_env.value,
            "status": "UNAVAILABLE",
            "versions": versions,
            "observations": observations,
            "reviews": reviews,
        }
        if args.json:
            _print(report, as_json=True)
        else:
            print("UNAVAILABLE  no instrument catalog has been synchronized")
        return 0

    report = {
        "environment": settings.binance_env.value,
        "status": status.review_status.value,
        "catalog_sha256": status.content_sha256,
        "observed_at": status.observed_at,
        "venue_symbols": status.total_symbols,
        "carry_candidates": status.candidate_symbols,
        "complete_specs": status.instrument_count,
        "excluded": status.excluded_count,
        "exclusion_reasons": _exclusion_summary(status.catalog),
        "versions": versions,
        "observations": observations,
        "reviews": reviews,
    }
    if args.json:
        _print(report, as_json=True)
    else:
        for key, value in report.items():
            print(f"{key:20} {value}")
        if status.review_status.value != "APPROVED":
            print("\nBLOCKED: current instrument specifications are not approved")
    return 0


async def cmd_instrument_review(settings: Settings, args: argparse.Namespace) -> int:
    action = InstrumentReviewAction(args.action.upper())

    async def body(session: AsyncSession) -> int:
        repo = InstrumentCatalogRepository(session, environment=settings.binance_env.value)
        status = await repo.review_current(
            content_sha256=args.hash,
            action=action,
            actor=args.actor,
            reason=args.reason,
        )
        print(f"{action.value} catalog={status.content_sha256} status={status.review_status.value}")
        return 0

    return await _run_audited(
        settings,
        job_name="instrument-review",
        argv=sys.argv[1:],
        body=body,
    )


# ---------------------------------------------------------------------------
# Phase-3 market-data archive and live series
# ---------------------------------------------------------------------------


async def _approved_specifications(
    session: AsyncSession, settings: Settings
) -> dict[str, dict[str, object]]:
    catalog = InstrumentCatalogRepository(session, environment=settings.binance_env.value)
    status = await catalog.current_status()
    if status is None or status.review_status.value != "APPROVED":
        raise RuntimeError("current exact instrument catalog must be APPROVED")
    raw = status.catalog.get("specifications")
    if not isinstance(raw, list):
        raise RuntimeError("approved instrument catalog has no specification list")
    specifications: dict[str, dict[str, object]] = {}
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("symbol"), str):
            raise RuntimeError("approved instrument catalog contains an invalid specification")
        symbol = item["symbol"].strip().upper()
        specifications[symbol] = item
    return specifications


def _selected_symbols(
    args: argparse.Namespace, specifications: dict[str, dict[str, object]]
) -> tuple[str, ...]:
    selected = tuple(sorted(specifications)) if args.all_approved else tuple(args.symbol or ())
    normalized = tuple(
        dict.fromkeys(symbol.strip().upper() for symbol in selected if symbol.strip())
    )
    if not normalized:
        raise ValueError("at least one symbol is required")
    outside = sorted(set(normalized) - set(specifications))
    if outside:
        raise ValueError(
            "symbols are outside the current approved carry universe: " + ", ".join(outside)
        )
    return normalized


def _rest_artifact(
    response: object,
    *,
    dataset: str,
    market: Market,
    symbol: str,
    endpoint: str,
    interval: str | None = None,
) -> MarketDataArtifact:
    raw = getattr(response, "raw", None)
    if raw is None:
        raise RuntimeError(f"{endpoint}: market-data persistence requires raw retention")
    prefix = "/fapi/v1" if market is Market.USDM else "/api/v3"
    return MarketDataArtifact(
        source_type=MarketDataSource.REST,
        dataset=dataset,
        market=market.value,
        symbol=symbol,
        interval=interval,
        source_url=f"{prefix}/{endpoint}",
        raw_key=raw.key,
        raw_sha256=raw.sha256,
        raw_size=raw.size,
        fetched_at=raw.fetched_at,
        metadata={"endpoint": endpoint},
    )


def _archive_artifact(payload: ArchivePayload) -> MarketDataArtifact:
    artifact = payload.artifact
    request = artifact.request
    dataset = "funding_rate" if request.dataset is ArchiveDataset.FUNDING_RATE else "kline"
    return MarketDataArtifact(
        source_type=MarketDataSource.ARCHIVE,
        dataset=dataset,
        market=request.market.value,
        symbol=request.symbol,
        interval=request.interval,
        source_url=request.url,
        period_start=request.period_start,
        period_end=request.period_end,
        raw_key=artifact.payload.key,
        raw_sha256=artifact.payload.sha256,
        raw_size=artifact.payload.size,
        checksum_key=artifact.checksum.key,
        checksum_sha256=artifact.checksum.sha256,
        expected_payload_sha256=artifact.expected_payload_sha256,
        fetched_at=artifact.fetched_at,
        metadata={
            "checksum_url": request.checksum_url,
            "csv_sha256": hashlib.sha256(payload.csv_bytes).hexdigest(),
        },
    )


def _date_argument(value: str, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date (YYYY-MM-DD)") from exc


def _utc_day(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=UTC)


async def cmd_backfill(settings: Settings, args: argparse.Namespace) -> int:
    """Checksum-verified monthly archive backfill over an explicit half-open range."""

    if settings.binance_env.value != VenueEnvironment.PRODUCTION.value:
        raise ValueError("data.binance.vision backfill requires BINANCE_ENV=production")
    start = _date_argument(args.start, "--start")
    end = _date_argument(args.end, "--end")
    current_month = datetime.now(UTC).date().replace(day=1)
    if end > current_month:
        raise ValueError(
            "monthly archive backfill must end on or before the current month; "
            "use live collectors for the unpublished tail"
        )
    dataset = ArchiveDataset.FUNDING_RATE if args.dataset == "funding" else ArchiveDataset.KLINES
    market = Market(args.market)
    if dataset is ArchiveDataset.FUNDING_RATE and market is not Market.USDM:
        raise ValueError("funding archives exist only for --market usdm")

    async def body(session: AsyncSession) -> int:
        specifications = await _approved_specifications(session, settings)
        symbols = _selected_symbols(args, specifications)
        requests = tuple(
            request
            for symbol in symbols
            for request in monthly_requests(
                dataset=dataset,
                market=market,
                symbol=symbol,
                start=start,
                end=end,
                interval=args.interval if dataset is ArchiveDataset.KLINES else None,
            )
        )
        if len(requests) > args.max_files:
            raise ValueError(
                f"backfill plan contains {len(requests)} files, above --max-files "
                f"{args.max_files}; split the date/symbol range or raise the explicit limit"
            )
        print(
            f"plan dataset={args.dataset} market={market.value} symbols={len(symbols)} "
            f"files={len(requests)} range=[{start},{end})"
        )
        if args.dry_run:
            for request in requests:
                print(request.url)
            return 0

        store = create_raw_store(settings)
        repository = MarketDataRepository(session, environment=settings.binance_env.value)
        start_at, end_at = _utc_day(start), _utc_day(end)
        totals = {"rows": 0, "inserted": 0, "existing": 0, "blocked": 0}
        async with create_http_client() as http:
            archive = BinanceArchiveClient(
                environment=VenueEnvironment.PRODUCTION,
                http=http,
                raw_store=store,
            )
            for request in requests:
                payload = await archive.fetch(request)
                source = _archive_artifact(payload)
                if dataset is ArchiveDataset.FUNDING_RATE:
                    parsed = parse_funding_csv(
                        payload.csv_bytes,
                        symbol=request.symbol,
                        collected_at=payload.artifact.fetched_at,
                    )
                    funding_rows = tuple(
                        row for row in parsed if start_at <= row.funding_time < end_at
                    )
                    result = await repository.ingest_funding(funding_rows, artifact=source)
                else:
                    parsed_klines = parse_kline_csv(
                        payload.csv_bytes,
                        symbol=request.symbol,
                        market=market,
                        collected_at=payload.artifact.fetched_at,
                    )
                    kline_rows = tuple(
                        row for row in parsed_klines if start_at <= row.open_time < end_at
                    )
                    result = await repository.ingest_klines(
                        kline_rows,
                        interval=args.interval,
                        artifact=source,
                    )
                totals["rows"] += result.rows
                totals["inserted"] += result.inserted
                totals["existing"] += result.existing
                totals["blocked"] += int(result.status is DataQualityStatus.BLOCKED)
                print(
                    f"{request.symbol:16} {request.period_label} "
                    f"{result.status.value:7} rows={result.rows} inserted={result.inserted} "
                    f"existing={result.existing} gaps={result.gaps} "
                    f"conflicts={result.conflicts}"
                )
        print("totals " + " ".join(f"{name}={value}" for name, value in totals.items()))
        return 1 if totals["blocked"] else 0

    return await _run_audited(settings, job_name="backfill", argv=sys.argv[1:], body=body)


def _funding_interval(specification: dict[str, object]) -> int:
    schedule = specification.get("funding_schedule")
    if not isinstance(schedule, dict):
        raise RuntimeError("approved specification has no funding interval")
    interval = schedule.get("interval_hours")
    if not isinstance(interval, int):
        raise RuntimeError("approved specification has no funding interval")
    return interval


async def cmd_record_funding(settings: Settings, args: argparse.Namespace) -> int:
    async def body(session: AsyncSession) -> int:
        specifications = await _approved_specifications(session, settings)
        symbols = _selected_symbols(args, specifications)
        store = create_raw_store(settings)
        repository = MarketDataRepository(session, environment=settings.binance_env.value)
        blocked = 0
        async with create_http_client() as http:
            api = BinanceRestClient(
                environment=_venue_environment(settings), http=http, raw_store=store
            )
            for symbol in symbols:
                history = await api.funding_history(symbol, limit=args.limit)
                funding_result = await repository.ingest_funding(
                    tuple(history.value),
                    artifact=_rest_artifact(
                        history,
                        dataset="funding_rate",
                        market=Market.USDM,
                        symbol=symbol,
                        endpoint="fundingRate",
                    ),
                    expected_interval_hours=_funding_interval(specifications[symbol]),
                )
                mark = await api.mark_price(symbol)
                mark_inserted = await repository.record_mark_price(
                    mark.value,
                    artifact=_rest_artifact(
                        mark,
                        dataset="mark_price",
                        market=Market.USDM,
                        symbol=symbol,
                        endpoint="premiumIndex",
                    ),
                )
                blocked += int(funding_result.status is DataQualityStatus.BLOCKED)
                print(
                    f"{symbol:16} {funding_result.status.value:7} "
                    f"funding_inserted={funding_result.inserted} "
                    f"funding_existing={funding_result.existing} "
                    f"mark_inserted={mark_inserted}"
                )
        return 1 if blocked else 0

    return await _run_audited(settings, job_name="record-funding", argv=sys.argv[1:], body=body)


async def cmd_record_prices(settings: Settings, args: argparse.Namespace) -> int:
    async def body(session: AsyncSession) -> int:
        specifications = await _approved_specifications(session, settings)
        symbols = _selected_symbols(args, specifications)
        store = create_raw_store(settings)
        repository = MarketDataRepository(session, environment=settings.binance_env.value)
        inserted = 0
        async with create_http_client() as http:
            api = BinanceRestClient(
                environment=_venue_environment(settings), http=http, raw_store=store
            )
            for symbol in symbols:
                mark = await api.mark_price(symbol)
                inserted += int(
                    await repository.record_mark_price(
                        mark.value,
                        artifact=_rest_artifact(
                            mark,
                            dataset="mark_price",
                            market=Market.USDM,
                            symbol=symbol,
                            endpoint="premiumIndex",
                        ),
                    )
                )
                for market in (Market.USDM, Market.SPOT):
                    book = await api.book_ticker(symbol, market)
                    inserted += int(
                        await repository.record_book_ticker(
                            book.value,
                            artifact=_rest_artifact(
                                book,
                                dataset="book_ticker",
                                market=market,
                                symbol=symbol,
                                endpoint="ticker/bookTicker",
                            ),
                        )
                    )
                print(f"{symbol:16} recorded mark + USD-M book + spot book")
        print(f"inserted={inserted} symbols={len(symbols)}")
        return 0

    return await _run_audited(settings, job_name="record-prices", argv=sys.argv[1:], body=body)


async def cmd_market_data_status(settings: Settings, args: argparse.Namespace) -> int:
    async with _session(settings) as session:
        status = await MarketDataRepository(
            session, environment=settings.binance_env.value
        ).status()
    report = {
        "environment": settings.binance_env.value,
        "artifacts": status.artifacts,
        "funding_rows": status.funding_rows,
        "kline_rows": status.kline_rows,
        "mark_snapshots": status.mark_snapshots,
        "book_snapshots": status.book_snapshots,
        "quality_assessments": status.quality_assessments,
        "blocked_assessments": status.blocked_assessments,
        "superseded_blocked_assessments": status.superseded_blocked_assessments,
        "latest_funding_at": status.latest_funding_at,
        "latest_prices_at": status.latest_prices_at,
    }
    if args.json:
        _print(report, as_json=True)
    else:
        for key, value in report.items():
            print(f"{key:24} {value}")
    return 1 if status.blocked_assessments else 0


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
        AccrualGate(
            key="brier_skill_vs_climatology",
            label="funding-persistence Brier skill vs climatology",
            observed=0.0,
            required=float(REQUIRED_BRIER_SKILL_VS_CLIMATOLOGY),
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
# Phase-4 funding-persistence model
# ---------------------------------------------------------------------------


def _funding_target(args: argparse.Namespace) -> FundingTarget:
    return FundingTarget(threshold_bps=parse_decimal(args.threshold_bps), horizon=int(args.horizon))


async def cmd_model_baseline(settings: Settings, args: argparse.Namespace) -> int:
    """Walk the funding-persistence baseline over history and record the evidence.

    Registration fails closed when a model-relevant source file is uncommitted:
    a digest over uncommitted bytes names code that exists on one machine, so a
    prediction carrying it could never be reproduced (ADR-0021).
    """
    target = _funding_target(args)

    async def body(session: AsyncSession) -> int:
        specifications = await _approved_specifications(session, settings)
        symbols = _selected_symbols(args, specifications)
        market_data = MarketDataRepository(session, environment=settings.binance_env.value)

        series: dict[str, tuple[Settlement, ...]] = {}
        snapshot_rows: list[tuple[str, datetime, Decimal]] = []
        for symbol in symbols:
            observations = await market_data.funding_series(symbol)
            if not observations:
                continue
            series[symbol] = tuple(
                Settlement(
                    funding_time=item.funding_time,
                    funding_rate=item.funding_rate,
                    interval_hours=item.interval_hours,
                )
                for item in observations
            )
            snapshot_rows.extend(
                (symbol, item.funding_time, item.funding_rate) for item in observations
            )
        if not snapshot_rows:
            raise ValueError("no funding history for the selected symbols; backfill first")

        snapshot = snapshot_observations(
            snapshot_rows,
            range_start=min(row[1] for row in snapshot_rows),
            range_end=max(row[1] for row in snapshot_rows),
        )
        model = ExpandingPersistenceModel(
            minimum_prior_cases=args.minimum_prior_cases,
            minimum_matched_cases=args.minimum_matched_cases,
        )
        provenance = capture(
            _REPO_ROOT,
            semantic_version=args.semantic_version,
            data=snapshot,
            parameters={
                "threshold_bps": str(target.threshold_bps),
                "horizon": str(target.horizon),
                "minimum_prior_cases": str(model.minimum_prior_cases),
                "minimum_matched_cases": str(model.minimum_matched_cases),
                "smoothing": "laplace-add-one",
            },
        )

        repository = ModelRepository(session, environment=settings.binance_env.value)
        version = await repository.register_version(provenance)

        scored: list[ScoredCase] = []
        skipped: list[tuple[ResolvedCase, SkipReason]] = []
        for symbol, settlements in sorted(series.items()):
            # Each symbol gets its own expanding history: pooling one symbol's
            # outcomes into another's estimate would be a modelling claim nobody
            # has made or tested.
            result = walk_forward(build_cases(symbol, settlements, target), model)
            scored.extend(result.scored)
            skipped.extend(result.skipped)
        walk = WalkForward(scored=tuple(scored), skipped=tuple(skipped))

        written = await repository.persist_predictions(
            walk.scored, model_version_id=version.id, target=target
        )
        pooled = score(walk.scored)
        evaluation = await repository.record_evaluation(
            pooled,
            model_version_id=version.id,
            target=target,
            evidence_source=EvidenceSource(args.evidence_source),
            data_snapshot_id=snapshot.snapshot_id,
            walk=walk,
            by_symbol=score_by_symbol(walk.scored),
            by_interval=score_by_interval(walk.scored),
        )

        print(f"model            {version.semantic_version} {version.content_sha256}")
        print(f"commit           {version.code_commit}")
        print(f"data snapshot    {snapshot.snapshot_id} rows={snapshot.row_count}")
        print(f"target           threshold={target.threshold_bps}bps horizon={target.horizon}")
        print(
            f"predictions      scored={written.scored} inserted={written.inserted} "
            f"existing={written.existing} skipped={len(walk.skipped)}"
        )
        print(
            f"skill            vs_naive={pooled.brier_skill_vs_naive:+.5f} "
            f"vs_climatology={pooled.brier_skill_vs_climatology:+.5f} "
            f"ece={pooled.model.ece:.4f}"
        )
        print(f"evaluation       {evaluation.id} {evaluation.eligible_status}")
        if evaluation.eligible_status != "PROMOTION_ELIGIBLE":
            print(
                "NOTE: archive replay is worth zero toward any promotion gate (ADR-0012); "
                "this is research evidence only"
            )
        return 0

    return await _run_audited(settings, job_name="model-baseline", argv=sys.argv[1:], body=body)


async def cmd_model_status(settings: Settings, args: argparse.Namespace) -> int:
    async with _session(settings) as session:
        repository = ModelRepository(session, environment=settings.binance_env.value)
        counts = await repository.counts()
        champion = await repository.champion()
    report: dict[str, object] = {
        "environment": settings.binance_env.value,
        **counts,
        "champion": champion.semantic_version or "NONE",
        "champion_sha256": champion.content_sha256 or "",
        "champion_actor": champion.actor or "",
        "champion_recorded_at": champion.recorded_at,
    }
    if args.json:
        _print(report, as_json=True)
    else:
        for key, value in report.items():
            print(f"{key:24} {value}")
        if not champion.has_champion:
            print("\nNo champion model. Sizing must never consult a model that has none.")
    return 0


async def cmd_model_promote(settings: Settings, args: argparse.Namespace) -> int:
    """Append a champion decision. The gates are re-checked against stored evidence."""

    async def body(session: AsyncSession) -> int:
        repository = ModelRepository(session, environment=settings.binance_env.value)
        version = await repository.version_by_digest(args.hash)
        if version is None:
            raise ValueError(f"no model version with content hash {args.hash}")
        if args.action == "RETIRE":
            event = await repository.retire_champion(
                model_version_id=version.id, actor=args.actor, reason=args.reason
            )
        else:
            evaluation = await repository.latest_evaluation(version.id)
            if evaluation is None:
                raise ValueError("model version has no evaluation in this environment")
            event = await repository.promote_champion(
                model_version_id=version.id,
                evaluation_id=evaluation.id,
                actor=args.actor,
                reason=args.reason,
            )
        print(f"{event.action} model={version.semantic_version} {version.content_sha256}")
        return 0

    return await _run_audited(settings, job_name="model-promote", argv=sys.argv[1:], body=body)


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------


async def cmd_dashboard(settings: Settings, args: argparse.Namespace) -> int:
    """The operator view. Always prefer this to a number written in a document."""
    print("=" * 72)
    print(
        f"crypto-trading-system   mode={settings.trading_mode.value}   "
        f"env={settings.binance_env.value}"
    )
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
        description="Operational CLI: governance and read-only venue/catalog surface.",
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

    binance_status = sub.add_parser(
        "binance-status", help="venue connectivity, clock drift, rate-limit budget"
    )
    binance_status.add_argument("--json", action="store_true")
    binance_status.set_defaults(func=cmd_binance_status)

    snapshot = sub.add_parser(
        "binance-snapshot", help="fetch public market data and retain every raw byte"
    )
    snapshot.add_argument("--out", help="raw-store directory override")
    snapshot.set_defaults(func=cmd_binance_snapshot)

    sync_instruments = sub.add_parser(
        "sync-instruments",
        help="version exchange filters, funding schedules, and margin tiers",
    )
    sync_instruments.set_defaults(func=cmd_sync_instruments)

    instrument_status = sub.add_parser(
        "instrument-status", help="current catalog hash and human-review state"
    )
    instrument_status.add_argument("--json", action="store_true")
    instrument_status.set_defaults(func=cmd_instrument_status)

    instrument_review = sub.add_parser(
        "instrument-review", help="approve or reject the exact current catalog hash"
    )
    instrument_review.add_argument("--hash", required=True, help="full current SHA-256")
    instrument_review.add_argument(
        "--action",
        required=True,
        choices=[action.value for action in InstrumentReviewAction],
    )
    instrument_review.add_argument("--actor", required=True)
    instrument_review.add_argument("--reason", required=True)
    instrument_review.set_defaults(func=cmd_instrument_review)

    backfill = sub.add_parser("backfill", help="checksum-verified monthly archive ingestion")
    backfill.add_argument("--dataset", required=True, choices=("funding", "klines"))
    backfill.add_argument(
        "--market", choices=[market.value for market in Market], default=Market.USDM.value
    )
    backfill.add_argument("--interval", default="1h")
    backfill.add_argument("--start", required=True, help="inclusive UTC date YYYY-MM-DD")
    backfill.add_argument("--end", required=True, help="exclusive UTC date YYYY-MM-DD")
    backfill.add_argument("--dry-run", action="store_true")
    backfill.add_argument(
        "--max-files",
        type=int,
        default=120,
        help="explicit safety ceiling for one atomic backfill transaction",
    )
    _add_symbol_selector(backfill)
    backfill.set_defaults(func=cmd_backfill)

    record_funding = sub.add_parser(
        "record-funding", help="persist recent settled funding and current mark/index"
    )
    record_funding.add_argument("--limit", type=int, default=10)
    _add_symbol_selector(record_funding)
    record_funding.set_defaults(func=cmd_record_funding)

    record_prices = sub.add_parser(
        "record-prices", help="persist current mark plus spot and USD-M best books"
    )
    _add_symbol_selector(record_prices)
    record_prices.set_defaults(func=cmd_record_prices)

    market_data_status = sub.add_parser(
        "market-data-status", help="persisted market series and quality summary"
    )
    market_data_status.add_argument("--json", action="store_true")
    market_data_status.set_defaults(func=cmd_market_data_status)

    model_baseline = sub.add_parser(
        "model-baseline", help="walk the funding-persistence baseline and record evidence"
    )
    model_baseline.add_argument("--semantic-version", default="funding-persistence-v1")
    model_baseline.add_argument("--threshold-bps", default="0")
    model_baseline.add_argument("--horizon", type=int, default=1)
    model_baseline.add_argument("--minimum-prior-cases", type=int, default=30)
    model_baseline.add_argument("--minimum-matched-cases", type=int, default=5)
    model_baseline.add_argument(
        "--evidence-source",
        default=EvidenceSource.BACKTEST.value,
        choices=[item.value for item in EvidenceSource],
        help="archive replay is BACKTEST and is worth zero toward promotion",
    )
    _add_symbol_selector(model_baseline)
    model_baseline.set_defaults(func=cmd_model_baseline)

    model_status = sub.add_parser("model-status", help="model versions and current champion")
    model_status.add_argument("--json", action="store_true")
    model_status.set_defaults(func=cmd_model_status)

    model_promote = sub.add_parser(
        "model-promote", help="append a champion PROMOTE/RETIRE for an exact model hash"
    )
    model_promote.add_argument("--hash", required=True, help="model version content SHA-256")
    model_promote.add_argument("--action", default="PROMOTE", choices=("PROMOTE", "RETIRE"))
    model_promote.add_argument("--actor", required=True)
    model_promote.add_argument("--reason", required=True)
    model_promote.set_defaults(func=cmd_model_promote)

    promotion = sub.add_parser("promotion-status", help="promotion gates and binding constraint")
    promotion.add_argument("--json", action="store_true")
    promotion.set_defaults(func=cmd_promotion_status)

    dashboard = sub.add_parser("dashboard", help="the operator view; read this first")
    dashboard.set_defaults(func=cmd_dashboard)

    return parser


def _add_symbol_selector(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--symbol",
        action="append",
        help="approved symbol; repeat for more than one",
    )
    group.add_argument(
        "--all-approved",
        action="store_true",
        help="operate on every symbol in the current exact approved catalog",
    )


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
