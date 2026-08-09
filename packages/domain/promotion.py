"""Promotion-gate arithmetic for a project with abundant history.

Gate thresholds live here rather than in the CLI so that the report, the
dashboard, and any future service agree on what "ready" means.

Why this is a rewrite and not a port (ADR-0012)
-----------------------------------------------
The sibling ``automated-trading-system`` gates on counts — 500 resolved
predictions, 500 matched forecasts, 60 outcome days. Those work as safety
mechanisms *because that project's history accrues one day per day*: they are
time gates wearing count costumes. Here, ``data.binance.vision`` publishes years
of free history, so any count gate can be satisfied in an afternoon by
backtesting. A gate you can clear in an afternoon is not a gate.

So gates here are **prospective wall-clock time on data the model has never
seen**. A backtest cannot contribute a single day. That rule is enforced
structurally by :class:`EvidenceSource` and :func:`prospective_days`, in the
domain, rather than by remembering to filter at each call site.

Two gate kinds, because higher-is-better is not universal
---------------------------------------------------------
The predecessor has only accrual gates, so its status rule is
``observed >= required``. Two of this project's gates are the opposite shape —
reconciliation discrepancies and liquidation-invariant violations must stay at
**zero** — and evaluating those with an accrual rule would report five violations
as a comfortable PASS against a threshold of zero. They are therefore a separate
type (:class:`CeilingGate`) with a separate status rule, and a breach is
:attr:`GateStatus.FAILED`: a violation that already happened never un-happens, so
no amount of waiting clears it. It takes a human decision and a new campaign.

Projections remain deliberately naive and are planning aids, never promotion
evidence: a gate is cleared by its observed value reaching the threshold, never
by a projected date arriving.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

# ---------------------------------------------------------------------------
# Thresholds. Changing one changes what the platform considers ready.
# These are the README's proposed starting set; retune the numbers by ADR, but
# keep the shape — prospective, forward-only, zero-tolerance where stated.
# ---------------------------------------------------------------------------

#: Consecutive wall-clock days of prospective paper operation. Forward-only.
REQUIRED_PROSPECTIVE_PAPER_DAYS = 90

#: Funding settlements observed prospectively (~3/day at the 8h cadence).
REQUIRED_PROSPECTIVE_SETTLEMENTS = 250

#: Net carry, after all costs, must beat simply holding USDT over the window.
#: Expressed in basis points because every threshold in this project is (§4).
REQUIRED_NET_CARRY_VS_BENCHMARK_BPS = 0.0

#: The funding-persistence model must beat naive persistence ("funding will be
#: what it was last period"). Skill is ``1 - model_brier / baseline_brier``.
REQUIRED_BRIER_SKILL_VS_NAIVE = 0.0

#: Ceilings. Both are zero and both are meant literally.
MAX_RECONCILIATION_DISCREPANCIES = 0
MAX_LIQUIDATION_INVARIANT_VIOLATIONS = 0

DEFAULT_ACCRUAL_WINDOW_DAYS = 7


class GateStatus(StrEnum):
    PASS = "PASS"
    ACCRUING = "ACCRUING"
    STALLED = "STALLED"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"


class EvidenceSource(StrEnum):
    """Where a day of evidence came from.

    Only :attr:`PAPER_PROSPECTIVE` counts toward a promotion gate. The others are
    recorded because they are genuinely useful — backtests reject ideas cheaply,
    size the opportunity, and find bugs — but they are not promotion evidence.
    """

    #: Forward-only paper operation against live data the model had not seen.
    PAPER_PROSPECTIVE = "PAPER_PROSPECTIVE"
    #: Replay of archived history. Never promotion evidence, however long it ran.
    BACKTEST = "BACKTEST"
    #: Testnet fills. Real order semantics, but not production liquidity, so the
    #: fill evidence is not production evidence either.
    TESTNET = "TESTNET"
    #: Explicitly synthetic or overridden runs. Never promotion evidence.
    SYNTHETIC = "SYNTHETIC"


#: The single source of truth for what a gate is allowed to count.
PROMOTION_ELIGIBLE_SOURCES: frozenset[EvidenceSource] = frozenset(
    {EvidenceSource.PAPER_PROSPECTIVE}
)


def is_promotion_eligible(source: EvidenceSource) -> bool:
    """Whether evidence from ``source`` may count toward a promotion gate."""
    return source in PROMOTION_ELIGIBLE_SOURCES


@dataclass(frozen=True, slots=True)
class EvidenceDay:
    """One UTC day on which the campaign produced evidence."""

    observed_on: date
    source: EvidenceSource


def prospective_days(days: Iterable[EvidenceDay], *, today: date) -> int:
    """Length of the unbroken run of promotion-eligible days ending at ``today``.

    "Consecutive" is meant strictly: one missed day resets the run to whatever
    has accrued since. That is the point — a gate measuring 90 days of *continuous
    operation* must not be satisfiable by 90 scattered days over a year, because
    the thing being demonstrated is that the system runs unattended without
    breaking.

    Days at or after ``today`` are ignored: the current day is still accruing and
    counting it would let the gate clear a day early. Ineligible sources are
    dropped before the run is measured, so interleaving backtest days cannot
    bridge a gap in paper operation.
    """
    eligible = {
        day.observed_on
        for day in days
        if is_promotion_eligible(day.source) and day.observed_on < today
    }
    if not eligible:
        return 0

    # The run must end on the most recent complete day, i.e. yesterday. If the
    # campaign stopped before that, there is no *current* run at all.
    cursor = today - timedelta(days=1)
    if cursor not in eligible:
        return 0

    run = 0
    while cursor in eligible:
        run += 1
        cursor -= timedelta(days=1)
    return run


@dataclass(frozen=True, slots=True)
class AccrualSample:
    """One point-in-time cumulative reading of a gate's observed value."""

    observed_on: date
    cumulative: float


def estimate_daily_rate(
    samples: Sequence[AccrualSample],
    *,
    today: date,
    window_days: int = DEFAULT_ACCRUAL_WINDOW_DAYS,
) -> float | None:
    """Return the mean daily increase across complete days, or None.

    Samples dated ``today`` are dropped: the current day is still accruing and
    would understate the rate. Returns None when fewer than two complete days
    remain or when the series did not grow, because "no observed progress" must
    never be rendered as a confident date.
    """
    if window_days < 1:
        raise ValueError("accrual window must be at least one day")

    complete = sorted(
        (sample for sample in samples if sample.observed_on < today),
        key=lambda sample: sample.observed_on,
    )
    if len(complete) < 2:
        return None

    earliest_allowed = complete[-1].observed_on - timedelta(days=window_days)
    window = [sample for sample in complete if sample.observed_on >= earliest_allowed] or complete[
        -2:
    ]
    if len(window) < 2:
        return None

    span_days = (window[-1].observed_on - window[0].observed_on).days
    if span_days <= 0:
        return None
    delta = window[-1].cumulative - window[0].cumulative
    return delta / span_days if delta > 0 else None


@dataclass(frozen=True, slots=True)
class AccrualGate:
    """A threshold that is cleared by an observed value climbing to ``required``.

    Two fields exist because the obvious implementation reports a false PASS:

    ``has_evidence``
        A gate with no observations behind it is :attr:`GateStatus.UNAVAILABLE`,
        never PASS. "Net carry beat the benchmark by 0 bps, threshold 0" reads as
        a pass, but nothing was measured — the campaign has not run. An
        unmeasured gate must never look like a cleared one.
    ``strict``
        Gates phrased as *beating* something ("net carry positive over the
        window", "the model beats naive persistence") clear on ``observed >
        required``, not ``>=``. Tying with the baseline means the model added
        nothing, which is precisely the outcome the gate is there to catch.
    """

    key: str
    label: str
    observed: float
    required: float
    daily_rate: float | None
    unit: str = ""
    projected_days_override: int | None = None
    has_evidence: bool = True
    strict: bool = False

    @property
    def cleared(self) -> bool:
        return self.observed > self.required if self.strict else self.observed >= self.required

    @property
    def status(self) -> GateStatus:
        if not self.has_evidence:
            return GateStatus.UNAVAILABLE
        if self.cleared:
            return GateStatus.PASS
        if self.daily_rate is None and self.projected_days_override is None:
            return GateStatus.STALLED
        return GateStatus.ACCRUING

    @property
    def remaining(self) -> float:
        return max(0.0, self.required - self.observed)

    @property
    def fraction_complete(self) -> float:
        if self.required <= 0:
            return 1.0
        return min(1.0, max(0.0, self.observed / self.required))

    @property
    def projected_days(self) -> int | None:
        """Whole days until the threshold is met at the observed rate."""
        if self.status is GateStatus.PASS:
            return 0
        if self.status is GateStatus.UNAVAILABLE:
            return None
        if self.projected_days_override is not None:
            return self.projected_days_override
        if self.daily_rate is None or self.daily_rate <= 0:
            return None
        return max(0, int(self.remaining / self.daily_rate + 0.999))

    def projected_date(self, today: date) -> date | None:
        days = self.projected_days
        return None if days is None else today + timedelta(days=days)


def wall_clock_gate(
    key: str,
    label: str,
    *,
    observed_days: int,
    required_days: int = REQUIRED_PROSPECTIVE_PAPER_DAYS,
    campaign_running: bool = True,
) -> AccrualGate:
    """An :class:`AccrualGate` over consecutive prospective days.

    A wall-clock gate needs no rate estimate: an unbroken campaign accrues exactly
    one day per day, so the projection is the remainder, exactly. That is more
    honest than extrapolating a measured rate — and it makes the gate's nature
    obvious in the report, which is the whole point of ADR-0012.

    ``campaign_running=False`` (no paper campaign has started) makes the gate
    UNAVAILABLE rather than ACCRUING, because a projected clearance date for a
    campaign nobody has started is fiction.
    """
    remaining = max(0, required_days - observed_days)
    return AccrualGate(
        key=key,
        label=label,
        observed=float(observed_days),
        required=float(required_days),
        daily_rate=1.0 if campaign_running else None,
        unit="days",
        projected_days_override=remaining if campaign_running else None,
        has_evidence=campaign_running,
    )


@dataclass(frozen=True, slots=True)
class CeilingGate:
    """A threshold that is cleared by an observed value staying at or below ``limit``.

    Distinct from :class:`AccrualGate` because a breach is permanent: a
    reconciliation discrepancy or a liquidation-invariant violation that already
    happened cannot be undone by waiting, so its status is
    :attr:`GateStatus.FAILED` and it has no projected date. Clearing it takes a
    root-cause fix and a fresh campaign, which is a human decision.

    ``has_evidence`` carries the same weight it does on :class:`AccrualGate`, and
    the trap is subtler here: zero violations observed across zero opportunities
    to violate is not evidence of safety, it is the absence of a test.
    """

    key: str
    label: str
    observed: float
    limit: float
    unit: str = ""
    has_evidence: bool = True

    @property
    def status(self) -> GateStatus:
        # A breach is reported even without evidence: observing a violation is
        # itself the evidence, and it disqualifies the campaign either way.
        if self.observed > self.limit:
            return GateStatus.FAILED
        return GateStatus.PASS if self.has_evidence else GateStatus.UNAVAILABLE

    @property
    def breach(self) -> float:
        return max(0.0, self.observed - self.limit)

    @property
    def projected_days(self) -> int | None:
        return 0 if self.status is GateStatus.PASS else None

    def projected_date(self, today: date) -> date | None:
        days = self.projected_days
        return None if days is None else today + timedelta(days=days)


Gate = AccrualGate | CeilingGate


def binding_gate(gates: Sequence[Gate]) -> Gate | None:
    """The gate that decides when — or whether — promotion becomes possible.

    Ordering, most severe first:

    1. **FAILED** — the campaign is disqualified; nothing else matters.
    2. **UNAVAILABLE** — the gate cannot be measured yet, so no date exists.
    3. **STALLED** — measurable, but showing no observed progress.
    4. The latest projected clearance among the rest.

    Returns ``None`` when every gate passes.
    """
    outstanding = [gate for gate in gates if gate.status is not GateStatus.PASS]
    if not outstanding:
        return None

    for severity in (GateStatus.FAILED, GateStatus.UNAVAILABLE, GateStatus.STALLED):
        matching = [gate for gate in outstanding if gate.status is severity]
        if matching:
            return matching[0]

    return max(
        outstanding,
        key=lambda gate: gate.projected_days if gate.projected_days is not None else -1,
    )
