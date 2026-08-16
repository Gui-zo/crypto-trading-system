"""Persistence for carry proposals — every sizing decision, approved or refused.

Refusals are first-class here. A store that only records what was traded cannot
answer "why were we flat through that window", and under ADR-0009 a refusal is
the risk engine working rather than idling. The binding constraint and every
limit's permitted quantity are kept so a decision can be re-argued later without
re-running the engine against inputs that have since moved.

Repository conventions as everywhere else: flush but never commit, and every
symbol-keyed read filters on ``environment`` (ADR-0010).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import CarryProposalRecord
from domain.carry import CarryEstimate
from domain.risk import RiskLimits, SizingDecision


@dataclass(frozen=True, slots=True)
class ProposalSummary:
    symbol: str
    approved: bool
    quantity: Decimal
    notional: Decimal
    net_carry_bps_on_capital: Decimal
    stress_band: Decimal
    binding_constraint: str
    explanation: str
    evaluated_at: datetime


class CarryProposalRepository:
    def __init__(self, session: AsyncSession, *, environment: str) -> None:
        self._session = session
        self._environment = environment

    async def record(
        self,
        *,
        symbol: str,
        catalog_sha256: str,
        mark_price: Decimal,
        forecast_volatility: Decimal,
        expected_funding_rate: Decimal,
        settlements: int,
        estimate: CarryEstimate,
        breakeven_funding_bps: Decimal | None,
        decision: SizingDecision,
        limits: RiskLimits,
        evaluated_at: datetime | None = None,
    ) -> CarryProposalRecord:
        record = CarryProposalRecord(
            environment=self._environment,
            symbol=symbol.strip().upper(),
            evaluated_at=evaluated_at or datetime.now(UTC),
            catalog_sha256=catalog_sha256,
            mark_price=mark_price,
            forecast_volatility=forecast_volatility,
            expected_funding_rate=expected_funding_rate,
            settlements=settlements,
            gross_funding_bps=estimate.gross_funding_bps,
            total_cost_bps=estimate.total_cost_bps,
            net_carry_bps_on_capital=estimate.net_bps_on_capital,
            breakeven_funding_bps=breakeven_funding_bps,
            approved=decision.approved,
            quantity=decision.quantity,
            notional=decision.notional,
            perp_margin=decision.perp_margin,
            margin_buffer=decision.margin_buffer,
            capital_required=decision.capital_required,
            stress_band=decision.stress_band,
            binding_constraint=decision.binding.value,
            explanation=decision.explanation,
            limits_json={
                "max_effective_leverage": str(limits.max_effective_leverage),
                "stress_sigma_multiple": str(limits.stress_sigma_multiple),
                "stress_band_floor": str(limits.stress_band_floor),
                "margin_buffer_fraction": str(limits.margin_buffer_fraction),
                "max_instrument_notional": str(limits.max_instrument_notional),
                "max_total_notional": str(limits.max_total_notional),
                "max_group_notional": str(limits.max_group_notional),
            },
            outcomes_json=[
                {
                    "constraint": item.constraint.value,
                    "permitted_quantity": str(item.permitted_quantity),
                    "detail": item.detail,
                }
                for item in decision.outcomes
            ],
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def latest(self, limit: int = 20) -> tuple[ProposalSummary, ...]:
        rows = (
            (
                await self._session.execute(
                    select(CarryProposalRecord)
                    .where(CarryProposalRecord.environment == self._environment)
                    .order_by(CarryProposalRecord.evaluated_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return tuple(
            ProposalSummary(
                symbol=row.symbol,
                approved=row.approved,
                quantity=row.quantity,
                notional=row.notional,
                net_carry_bps_on_capital=row.net_carry_bps_on_capital,
                stress_band=row.stress_band,
                binding_constraint=row.binding_constraint,
                explanation=row.explanation,
                evaluated_at=row.evaluated_at,
            )
            for row in rows
        )

    async def counts(self) -> dict[str, int]:
        async def scalar(statement: Select[tuple[int]]) -> int:
            return int((await self._session.execute(statement)).scalar_one())

        scoped = CarryProposalRecord.environment == self._environment
        return {
            "proposals": await scalar(select(func.count(CarryProposalRecord.id)).where(scoped)),
            "approved": await scalar(
                select(func.count(CarryProposalRecord.id)).where(
                    scoped, CarryProposalRecord.approved.is_(True)
                )
            ),
        }

    async def binding_constraint_counts(self) -> Sequence[tuple[str, int]]:
        """Why the engine said what it said, aggregated. Refusals included."""
        rows = (
            await self._session.execute(
                select(
                    CarryProposalRecord.binding_constraint,
                    func.count(CarryProposalRecord.id),
                )
                .where(CarryProposalRecord.environment == self._environment)
                .group_by(CarryProposalRecord.binding_constraint)
                .order_by(func.count(CarryProposalRecord.id).desc())
            )
        ).all()
        return [(str(name), int(count)) for name, count in rows]
