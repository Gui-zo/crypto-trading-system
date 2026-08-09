"""Persistence for append-only, scoped safety-control events.

Adapted from the sibling repo. The one structural change: every control is scoped
to a venue environment (ADR-0010), so halting testnet does not silence production
and vice versa. Current state is resolved per
``(environment, scope_type, scope_key)``.

Nothing here updates or deletes a row. Clearing a halt appends a CLEAR event
carrying ``supersedes_event_id``, which is what makes the history reconstructable.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import SafetyControlEventRecord
from domain.safety import (
    SafetyControlAction,
    SafetyControlState,
    SafetyScope,
    SafetyScopeRef,
    normalize_scope_key,
)


class SafetyControlRepository:
    """Resolve current kill-switch state from a monotonic append-only event log."""

    def __init__(self, session: AsyncSession, *, environment: str) -> None:
        self._session = session
        self._environment = environment

    @staticmethod
    def _state(row: SafetyControlEventRecord) -> SafetyControlState:
        return SafetyControlState(
            event_id=row.id,
            sequence_number=row.sequence_number,
            scope=SafetyScope(row.scope_type),
            scope_key=row.scope_key,
            action=SafetyControlAction(row.action),
            reason=row.reason,
        )

    async def set_control(
        self,
        *,
        scope: SafetyScope,
        scope_key: str,
        action: SafetyControlAction,
        reason: str,
        actor: str,
        source: str,
        automatic: bool = False,
    ) -> SafetyControlState:
        """Append a control event, unless it would be an exact duplicate.

        The duplicate check keeps a watchdog that re-latches the same automatic
        halt every ten minutes from burying the human events in the log. A change
        in *any* field — including the reason text — is a new event, because a
        halt for a new reason is a new fact.
        """
        normalized_key = normalize_scope_key(scope, scope_key)
        normalized_reason = reason.strip()
        normalized_actor = actor.strip()
        normalized_source = source.strip().upper()
        if not normalized_reason:
            raise ValueError("safety-control reason cannot be empty")
        if not normalized_actor:
            raise ValueError("safety-control actor cannot be empty")
        if not normalized_source:
            raise ValueError("safety-control source cannot be empty")

        latest = (
            await self._session.execute(
                select(SafetyControlEventRecord)
                .where(
                    SafetyControlEventRecord.environment == self._environment,
                    SafetyControlEventRecord.scope_type == scope.value,
                    SafetyControlEventRecord.scope_key == normalized_key,
                )
                .order_by(SafetyControlEventRecord.sequence_number.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if (
            latest is not None
            and latest.action == action.value
            and latest.reason == normalized_reason
            and latest.actor == normalized_actor
            and latest.source == normalized_source
            and latest.automatic is automatic
        ):
            return self._state(latest)

        row = SafetyControlEventRecord(
            environment=self._environment,
            scope_type=scope.value,
            scope_key=normalized_key,
            action=action.value,
            reason=normalized_reason,
            actor=normalized_actor,
            source=normalized_source,
            automatic=automatic,
            supersedes_event_id=latest.id if latest is not None else None,
        )
        self._session.add(row)
        await self._session.flush()
        return self._state(row)

    async def current_states(self, *, active_only: bool = False) -> list[SafetyControlState]:
        latest = (
            select(
                SafetyControlEventRecord.scope_type,
                SafetyControlEventRecord.scope_key,
                func.max(SafetyControlEventRecord.sequence_number).label("sequence_number"),
            )
            .where(SafetyControlEventRecord.environment == self._environment)
            .group_by(
                SafetyControlEventRecord.scope_type,
                SafetyControlEventRecord.scope_key,
            )
            .subquery()
        )
        stmt = select(SafetyControlEventRecord).join(
            latest,
            and_(
                SafetyControlEventRecord.scope_type == latest.c.scope_type,
                SafetyControlEventRecord.scope_key == latest.c.scope_key,
                SafetyControlEventRecord.sequence_number == latest.c.sequence_number,
            ),
        )
        if active_only:
            stmt = stmt.where(SafetyControlEventRecord.action == SafetyControlAction.ACTIVATE.value)
        rows = (
            (
                await self._session.execute(
                    stmt.order_by(
                        SafetyControlEventRecord.scope_type,
                        SafetyControlEventRecord.scope_key,
                    )
                )
            )
            .scalars()
            .all()
        )
        return [self._state(row) for row in rows]

    async def states_for(self, scopes: Sequence[SafetyScopeRef]) -> tuple[SafetyControlState, ...]:
        wanted = {(scope.scope, scope.key) for scope in scopes}
        current = await self.current_states()
        return tuple(state for state in current if (state.scope, state.scope_key) in wanted)

    async def history(self, *, scope: SafetyScope, scope_key: str) -> list[SafetyControlState]:
        normalized_key = normalize_scope_key(scope, scope_key)
        rows = (
            (
                await self._session.execute(
                    select(SafetyControlEventRecord)
                    .where(
                        SafetyControlEventRecord.environment == self._environment,
                        SafetyControlEventRecord.scope_type == scope.value,
                        SafetyControlEventRecord.scope_key == normalized_key,
                    )
                    .order_by(SafetyControlEventRecord.sequence_number)
                )
            )
            .scalars()
            .all()
        )
        return [self._state(row) for row in rows]

    async def event_count(self) -> int:
        return int(
            (
                await self._session.execute(
                    select(func.count(SafetyControlEventRecord.id)).where(
                        SafetyControlEventRecord.environment == self._environment
                    )
                )
            ).scalar_one()
        )
