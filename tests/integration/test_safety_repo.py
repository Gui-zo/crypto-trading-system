"""Safety-control repository tests against the migrated schema.

Every assertion is scoped to a scope key this test created. The session rolls
back, but its queries still read committed rows from real runs, so a global
"latest" or "count" assertion would pass or fail depending on what the operator
did yesterday.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import SafetyControlEventRecord
from db.safety_repo import SafetyControlRepository
from domain.safety import SafetyControlAction, SafetyScope, SafetyScopeRef


def unique_key(prefix: str = "TEST") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12].upper()}"


def repo(session: AsyncSession, *, environment: str = "testnet") -> SafetyControlRepository:
    return SafetyControlRepository(session, environment=environment)


async def test_activating_then_clearing_leaves_two_events(db_session: AsyncSession) -> None:
    key = unique_key("MARKET")
    controls = repo(db_session)

    activated = await controls.set_control(
        scope=SafetyScope.MARKET,
        scope_key=key,
        action=SafetyControlAction.ACTIVATE,
        reason="funding regime broke",
        actor="gcalixto",
        source="CLI",
    )
    cleared = await controls.set_control(
        scope=SafetyScope.MARKET,
        scope_key=key,
        action=SafetyControlAction.CLEAR,
        reason="investigated and resolved",
        actor="gcalixto",
        source="CLI",
    )

    assert cleared.sequence_number > activated.sequence_number
    history = await controls.history(scope=SafetyScope.MARKET, scope_key=key)
    assert [state.action for state in history] == [
        SafetyControlAction.ACTIVATE,
        SafetyControlAction.CLEAR,
    ]


async def test_a_clear_records_the_event_it_supersedes(db_session: AsyncSession) -> None:
    key = unique_key("MARKET")
    controls = repo(db_session)
    activated = await controls.set_control(
        scope=SafetyScope.MARKET,
        scope_key=key,
        action=SafetyControlAction.ACTIVATE,
        reason="halt",
        actor="gcalixto",
        source="CLI",
    )
    cleared = await controls.set_control(
        scope=SafetyScope.MARKET,
        scope_key=key,
        action=SafetyControlAction.CLEAR,
        reason="resolved",
        actor="gcalixto",
        source="CLI",
    )
    row = await db_session.get(SafetyControlEventRecord, cleared.event_id)
    assert row is not None
    assert row.supersedes_event_id == activated.event_id


async def test_an_identical_repeat_does_not_append_a_new_event(
    db_session: AsyncSession,
) -> None:
    """A watchdog re-latching the same halt must not bury the human events."""
    key = unique_key("DATA_PROVIDER")
    controls = repo(db_session)
    kwargs = {
        "scope": SafetyScope.DATA_PROVIDER,
        "scope_key": key,
        "action": SafetyControlAction.ACTIVATE,
        "reason": "artifact stale",
        "actor": "operational-health-watchdog",
        "source": "OPERATIONAL_HEALTH",
        "automatic": True,
    }
    first = await controls.set_control(**kwargs)  # type: ignore[arg-type]
    second = await controls.set_control(**kwargs)  # type: ignore[arg-type]
    assert first.event_id == second.event_id
    assert len(await controls.history(scope=SafetyScope.DATA_PROVIDER, scope_key=key)) == 1


async def test_a_changed_reason_is_a_new_event(db_session: AsyncSession) -> None:
    key = unique_key("DATA_PROVIDER")
    controls = repo(db_session)
    await controls.set_control(
        scope=SafetyScope.DATA_PROVIDER,
        scope_key=key,
        action=SafetyControlAction.ACTIVATE,
        reason="artifact stale",
        actor="watchdog",
        source="OPERATIONAL_HEALTH",
        automatic=True,
    )
    await controls.set_control(
        scope=SafetyScope.DATA_PROVIDER,
        scope_key=key,
        action=SafetyControlAction.ACTIVATE,
        reason="job crashed",
        actor="watchdog",
        source="OPERATIONAL_HEALTH",
        automatic=True,
    )
    history = await controls.history(scope=SafetyScope.DATA_PROVIDER, scope_key=key)
    assert [state.reason for state in history] == ["artifact stale", "job crashed"]


async def test_current_state_is_the_latest_event_for_the_scope(
    db_session: AsyncSession,
) -> None:
    key = unique_key("STRATEGY")
    controls = repo(db_session)
    for action, reason in (
        (SafetyControlAction.ACTIVATE, "one"),
        (SafetyControlAction.CLEAR, "two"),
        (SafetyControlAction.ACTIVATE, "three"),
    ):
        await controls.set_control(
            scope=SafetyScope.STRATEGY,
            scope_key=key,
            action=action,
            reason=reason,
            actor="gcalixto",
            source="CLI",
        )
    (state,) = await controls.states_for((SafetyScopeRef(SafetyScope.STRATEGY, key),))
    assert state.action is SafetyControlAction.ACTIVATE
    assert state.reason == "three"


async def test_a_testnet_halt_does_not_halt_production(db_session: AsyncSession) -> None:
    """ADR-0010, at the one layer where forgetting it silences the wrong venue."""
    key = unique_key("VENUE")
    scope = (SafetyScopeRef(SafetyScope.VENUE, key),)

    await repo(db_session, environment="testnet").set_control(
        scope=SafetyScope.VENUE,
        scope_key=key,
        action=SafetyControlAction.ACTIVATE,
        reason="testnet outage",
        actor="gcalixto",
        source="CLI",
    )

    testnet_states = await repo(db_session, environment="testnet").states_for(scope)
    production_states = await repo(db_session, environment="production").states_for(scope)
    assert len(testnet_states) == 1
    assert production_states == ()


async def test_active_only_filters_cleared_scopes(db_session: AsyncSession) -> None:
    halted_key = unique_key("ACCOUNT_HALT")
    cleared_key = unique_key("ACCOUNT_CLEAR")
    controls = repo(db_session)
    await controls.set_control(
        scope=SafetyScope.ACCOUNT,
        scope_key=halted_key,
        action=SafetyControlAction.ACTIVATE,
        reason="drawdown",
        actor="gcalixto",
        source="CLI",
    )
    await controls.set_control(
        scope=SafetyScope.ACCOUNT,
        scope_key=cleared_key,
        action=SafetyControlAction.ACTIVATE,
        reason="drawdown",
        actor="gcalixto",
        source="CLI",
    )
    await controls.set_control(
        scope=SafetyScope.ACCOUNT,
        scope_key=cleared_key,
        action=SafetyControlAction.CLEAR,
        reason="reset",
        actor="gcalixto",
        source="CLI",
    )

    active_keys = {state.scope_key for state in await controls.current_states(active_only=True)}
    assert halted_key in active_keys
    assert cleared_key not in active_keys


async def test_the_global_scope_normalizes_to_a_star(db_session: AsyncSession) -> None:
    controls = repo(db_session)
    state = await controls.set_control(
        scope=SafetyScope.GLOBAL,
        scope_key="",
        action=SafetyControlAction.CLEAR,
        reason="baseline",
        actor="gcalixto",
        source="CLI",
    )
    assert state.scope_key == "*"


@pytest.mark.parametrize(
    ("field", "value"),
    [("reason", "   "), ("actor", ""), ("source", "  ")],
)
async def test_blank_provenance_is_refused(
    db_session: AsyncSession, field: str, value: str
) -> None:
    kwargs: dict[str, object] = {
        "scope": SafetyScope.MARKET,
        "scope_key": unique_key(),
        "action": SafetyControlAction.ACTIVATE,
        "reason": "reason",
        "actor": "gcalixto",
        "source": "CLI",
        field: value,
    }
    with pytest.raises(ValueError, match="cannot be empty"):
        await repo(db_session).set_control(**kwargs)  # type: ignore[arg-type]


async def test_the_database_refuses_an_unknown_scope_type(db_session: AsyncSession) -> None:
    """Defence in depth: the check constraint holds even if domain code is bypassed."""
    db_session.add(
        SafetyControlEventRecord(
            environment="testnet",
            scope_type="NOT_A_SCOPE",
            scope_key="X",
            action="ACTIVATE",
            reason="bypass attempt",
            actor="test",
            source="TEST",
            automatic=False,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_the_database_refuses_an_unknown_environment(db_session: AsyncSession) -> None:
    db_session.add(
        SafetyControlEventRecord(
            environment="mainnet",
            scope_type="VENUE",
            scope_key="BINANCE",
            action="ACTIVATE",
            reason="typo",
            actor="test",
            source="TEST",
            automatic=False,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
