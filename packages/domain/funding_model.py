"""Funding-persistence forecasting: targets, cases, the naive baseline, and skill.

ADR-0004 fixes what the model layer forecasts: ``P(funding stays above the cost
threshold over the next N settlements)``. Kill criterion 2 says the thesis is
dead unless that forecast beats a naive "funding will be what it was last
period" baseline on Brier score. This module is the pure implementation of both
sides of that comparison, so the comparison itself is exhaustively testable with
no database and no venue.

Three venue facts from ADR-0020 shape every function here:

* A settlement's funding interval is whatever was in force **at that
  settlement**. It changes within a symbol's history, so nothing may derive a
  cadence from the current catalog and apply it backwards.
* A scheduled settlement may not exist. Cases are therefore built from the
  settlements that were **observed**, never from the boundaries a schedule
  implies, and each case records the largest step it spans so a consumer can
  see when a window crossed a hole.
* Funding timestamps carry millisecond jitter and are never compared for
  equality with a boundary.

Probabilities are ``float`` because :mod:`domain.calibration` is float-based and
ported verbatim. Rates and thresholds are ``Decimal``, as everywhere else.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise

from domain.calibration import ReliabilityReport, brier_skill_score, reliability
from domain.errors import DomainError
from domain.precision import from_bps

#: An expanding-window estimate needs enough resolved history to mean anything.
#: Below this the model declines to predict rather than emitting a number backed
#: by a handful of cases. Fail closed (ADR-0007).
DEFAULT_MINIMUM_PRIOR_CASES = 30

#: ...and enough history in the *same* conditioning state, since the estimate is
#: conditional on what the previous settlement did.
DEFAULT_MINIMUM_MATCHED_CASES = 5


class FundingModelError(DomainError):
    """Invalid target, unusable settlement series, or a leakage violation."""


class SkipReason(StrEnum):
    """Why a case produced no model prediction. Recorded, never silently dropped."""

    #: Too little fully-resolved history before this decision.
    INSUFFICIENT_PRIOR_CASES = "INSUFFICIENT_PRIOR_CASES"
    #: Prior history exists but too little of it shares this case's prior state.
    INSUFFICIENT_MATCHED_CASES = "INSUFFICIENT_MATCHED_CASES"


@dataclass(frozen=True, slots=True)
class Settlement:
    """One observed funding settlement, with the interval that was in force."""

    funding_time: datetime
    funding_rate: Decimal
    interval_hours: int | None

    def __post_init__(self) -> None:
        if self.funding_time.tzinfo is None:
            raise FundingModelError("settlement funding_time must be timezone-aware")
        if self.interval_hours is not None and self.interval_hours <= 0:
            raise FundingModelError("interval_hours must be positive when present")


@dataclass(frozen=True, slots=True)
class FundingTarget:
    """``P(each of the next `horizon` observed settlements pays >= threshold)``.

    The threshold is in **basis points** like every other threshold in this
    codebase. It is carried on the target rather than hardcoded because the
    cost-aware value is Phase 5's to compute; a prediction is only ever
    comparable to another prediction made under the same target.
    """

    threshold_bps: Decimal
    horizon: int = 1

    def __post_init__(self) -> None:
        if self.horizon < 1:
            raise FundingModelError("horizon must be at least 1 settlement")
        if not self.threshold_bps.is_finite():
            raise FundingModelError("threshold_bps must be finite")

    @property
    def threshold_rate(self) -> Decimal:
        """The threshold as a funding rate, matching stored settlement rates."""
        return from_bps(self.threshold_bps)

    def is_above(self, rate: Decimal) -> bool:
        return rate >= self.threshold_rate


@dataclass(frozen=True, slots=True)
class ResolvedCase:
    """One decision point whose outcome is fully observed.

    ``decision_time`` is the settlement that had just paid when the forecast
    would have been made. ``resolved_at`` is the last settlement in the target
    window — the instant the outcome became known, and therefore the instant
    from which this case may inform another forecast.
    """

    symbol: str
    decision_time: datetime
    resolved_at: datetime
    previous_rate: Decimal
    previous_above: bool
    outcome: bool
    interval_hours: int | None
    max_step_hours: Decimal
    window_hours: Decimal


def build_cases(
    symbol: str,
    settlements: Sequence[Settlement],
    target: FundingTarget,
) -> tuple[ResolvedCase, ...]:
    """Turn an observed settlement series into fully-resolved forecasting cases.

    Consumes only settlements that exist. A window that spans a venue hole is
    kept, not discarded, and reports the hole through ``max_step_hours`` — the
    alternative would be to silently drop exactly the unusual regimes a carry
    strategy cares about.
    """
    if not symbol:
        raise FundingModelError("symbol is required")
    ordered = tuple(settlements)
    for earlier, later in pairwise(ordered):
        if later.funding_time <= earlier.funding_time:
            raise FundingModelError(
                f"{symbol}: settlements must be strictly increasing in funding_time"
            )

    cases: list[ResolvedCase] = []
    for index in range(len(ordered) - target.horizon):
        previous = ordered[index]
        window = ordered[index + 1 : index + 1 + target.horizon]
        steps = tuple(
            _hours_between(a.funding_time, b.funding_time) for a, b in pairwise((previous, *window))
        )
        cases.append(
            ResolvedCase(
                symbol=symbol,
                decision_time=previous.funding_time,
                resolved_at=window[-1].funding_time,
                previous_rate=previous.funding_rate,
                previous_above=target.is_above(previous.funding_rate),
                outcome=all(target.is_above(item.funding_rate) for item in window),
                interval_hours=previous.interval_hours,
                max_step_hours=max(steps),
                window_hours=_hours_between(previous.funding_time, window[-1].funding_time),
            )
        )
    return tuple(cases)


def _hours_between(earlier: datetime, later: datetime) -> Decimal:
    seconds = Decimal(str((later - earlier).total_seconds()))
    return (seconds / Decimal(3600)).quantize(Decimal("0.0001"))


def naive_persistence_probability(case: ResolvedCase) -> float:
    """The ADR-0004 baseline: *funding will be what it was last period*.

    A deterministic 0/1 forecast, so its Brier score is exactly its error rate.
    That is a genuinely hard opponent on a persistent series, which is the point
    — beating it is the only justification for a model layer existing at all.
    """
    return 1.0 if case.previous_above else 0.0


@dataclass(frozen=True, slots=True)
class PersistenceEstimate:
    """A model probability with the evidence that produced it."""

    probability: float
    prior_cases: int
    matched_cases: int
    matched_positive: int


@dataclass(frozen=True, slots=True)
class ExpandingPersistenceModel:
    """Conditional persistence frequency over strictly-prior resolved history.

    ``P(target met | the previous settlement was above the threshold)``,
    estimated from cases that had **fully resolved before this decision was
    made**. That is the leakage rule, and it is stricter than "outcome date is
    earlier": a case resolves at the last settlement of its window, so a
    two-settlement window observed around the decision cannot inform it.

    Laplace smoothing keeps the estimate off 0.0 and 1.0. An unsmoothed
    frequency would hand the model an unbounded Brier penalty for one surprise
    and would claim certainty it has not earned.
    """

    minimum_prior_cases: int = DEFAULT_MINIMUM_PRIOR_CASES
    minimum_matched_cases: int = DEFAULT_MINIMUM_MATCHED_CASES

    def __post_init__(self) -> None:
        if self.minimum_prior_cases < 1 or self.minimum_matched_cases < 1:
            raise FundingModelError("minimum case counts must be positive")

    def predict(
        self,
        history: Sequence[ResolvedCase],
        case: ResolvedCase,
    ) -> PersistenceEstimate | SkipReason:
        usable = tuple(item for item in history if item.resolved_at < case.decision_time)
        if len(usable) < self.minimum_prior_cases:
            return SkipReason.INSUFFICIENT_PRIOR_CASES
        matched = tuple(item for item in usable if item.previous_above == case.previous_above)
        if len(matched) < self.minimum_matched_cases:
            return SkipReason.INSUFFICIENT_MATCHED_CASES
        positive = sum(1 for item in matched if item.outcome)
        return PersistenceEstimate(
            probability=(positive + 1) / (len(matched) + 2),
            prior_cases=len(usable),
            matched_cases=len(matched),
            matched_positive=positive,
        )


@dataclass(frozen=True, slots=True)
class ExpandingClimatology:
    """The second baseline: the expanding base rate, ignoring the previous settlement.

    This exists because beating the ADR-0004 naive rule is not by itself evidence
    of information. Naive is a **0/1** forecast, and Brier punishes a confident
    error far harder than a hedged one, so any roughly-calibrated probability
    beats it — measured on the research history, climatology alone scores +0.139
    against naive at a 0 bps threshold while using no information whatsoever.

    Climatology consumes exactly the same resolved history under exactly the same
    cutoff, and differs from the model in one respect: it does not condition on
    what the previous settlement did. Skill against it therefore isolates the
    value of that conditioning bit from the value of merely being calibrated.
    """

    minimum_prior_cases: int = DEFAULT_MINIMUM_PRIOR_CASES

    def __post_init__(self) -> None:
        if self.minimum_prior_cases < 1:
            raise FundingModelError("minimum_prior_cases must be positive")

    def predict(
        self,
        history: Sequence[ResolvedCase],
        case: ResolvedCase,
    ) -> float | SkipReason:
        usable = tuple(item for item in history if item.resolved_at < case.decision_time)
        if len(usable) < self.minimum_prior_cases:
            return SkipReason.INSUFFICIENT_PRIOR_CASES
        positive = sum(1 for item in usable if item.outcome)
        return (positive + 1) / (len(usable) + 2)


@dataclass(frozen=True, slots=True)
class ScoredCase:
    """A case scored by the model and both baselines, ready for calibration."""

    case: ResolvedCase
    model_probability: float
    naive_probability: float
    climatology_probability: float
    estimate: PersistenceEstimate


@dataclass(frozen=True, slots=True)
class WalkForward:
    """Everything a walk-forward pass produced, including what it refused to score."""

    scored: tuple[ScoredCase, ...]
    skipped: tuple[tuple[ResolvedCase, SkipReason], ...]

    @property
    def eligible(self) -> int:
        return len(self.scored) + len(self.skipped)


def walk_forward(
    cases: Sequence[ResolvedCase],
    model: ExpandingPersistenceModel,
    climatology: ExpandingClimatology | None = None,
) -> WalkForward:
    """Score every case using only history that had resolved before it.

    Cases are processed in decision order and the history offered to each
    prediction is the full set of cases seen so far; the forecasters apply the
    resolution cutoff themselves, so a caller cannot accidentally widen it.

    A case is scored only when **both** the model and climatology can produce a
    number, so the two are always compared on an identical sample. A skill value
    computed over different case sets would not be a comparison at all.
    """
    baseline = climatology or ExpandingClimatology(minimum_prior_cases=model.minimum_prior_cases)
    ordered = sorted(cases, key=lambda item: (item.decision_time, item.symbol))
    scored: list[ScoredCase] = []
    skipped: list[tuple[ResolvedCase, SkipReason]] = []
    seen: list[ResolvedCase] = []
    for case in ordered:
        estimate = model.predict(seen, case)
        climate = baseline.predict(seen, case)
        if isinstance(estimate, SkipReason):
            skipped.append((case, estimate))
        elif isinstance(climate, SkipReason):
            skipped.append((case, climate))
        else:
            scored.append(
                ScoredCase(
                    case=case,
                    model_probability=estimate.probability,
                    naive_probability=naive_persistence_probability(case),
                    climatology_probability=climate,
                    estimate=estimate,
                )
            )
        seen.append(case)
    return WalkForward(scored=tuple(scored), skipped=tuple(skipped))


@dataclass(frozen=True, slots=True)
class SkillReport:
    """Model versus both baselines over one slice of scored cases.

    Skill is ``1 - model/baseline``; positive means the model won. It is never
    presented without ``n``, because a skill number detached from its sample size
    is exactly how a weak result gets promoted.

    Two skills are always reported together. ``brier_skill_vs_naive`` is the
    ADR-0004 kill-criterion comparison. ``brier_skill_vs_climatology`` is the one
    that shows whether the previous settlement carried information, rather than
    the model merely being better calibrated than a 0/1 label.
    """

    label: str
    n: int
    model: ReliabilityReport
    naive: ReliabilityReport
    climatology: ReliabilityReport
    brier_skill_vs_naive: float
    brier_skill_vs_climatology: float
    positive_rate: float

    @property
    def beats_naive(self) -> bool:
        return self.n > 0 and self.brier_skill_vs_naive > 0.0

    @property
    def beats_climatology(self) -> bool:
        return self.n > 0 and self.brier_skill_vs_climatology > 0.0

    @property
    def informative(self) -> bool:
        """Won both comparisons — calibrated *and* using the conditioning bit."""
        return self.beats_naive and self.beats_climatology


def score(scored: Sequence[ScoredCase], *, label: str = "all", bins: int = 10) -> SkillReport:
    """Calibration and skill for a set of scored cases."""
    model = reliability([(item.model_probability, item.case.outcome) for item in scored], bins=bins)
    naive = reliability([(item.naive_probability, item.case.outcome) for item in scored], bins=bins)
    climatology = reliability(
        [(item.climatology_probability, item.case.outcome) for item in scored], bins=bins
    )
    positives = sum(1 for item in scored if item.case.outcome)
    return SkillReport(
        label=label,
        n=len(scored),
        model=model,
        naive=naive,
        climatology=climatology,
        brier_skill_vs_naive=brier_skill_score(model.brier, naive.brier),
        brier_skill_vs_climatology=brier_skill_score(model.brier, climatology.brier),
        positive_rate=(positives / len(scored)) if scored else 0.0,
    )


def score_by_symbol(scored: Sequence[ScoredCase], *, bins: int = 10) -> tuple[SkillReport, ...]:
    """Per-symbol slices, so one symbol cannot hide behind a pooled average."""
    return _sliced(scored, lambda item: item.case.symbol, bins=bins)


def score_by_interval(scored: Sequence[ScoredCase], *, bins: int = 10) -> tuple[SkillReport, ...]:
    """Slices by the funding interval in force, which is the regime that moves."""
    return _sliced(
        scored,
        lambda item: (
            "unknown" if item.case.interval_hours is None else f"{item.case.interval_hours}h"
        ),
        bins=bins,
    )


def _sliced(
    scored: Sequence[ScoredCase],
    key: Callable[[ScoredCase], str],
    *,
    bins: int,
) -> tuple[SkillReport, ...]:
    groups: dict[str, list[ScoredCase]] = {}
    for item in scored:
        groups.setdefault(str(key(item)), []).append(item)
    return tuple(score(items, label=label, bins=bins) for label, items in sorted(groups.items()))
