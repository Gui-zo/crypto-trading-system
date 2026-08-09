"""Pure forecast-calibration statistics.

Ported verbatim from the sibling ``automated-trading-system`` repo. These are
model-free diagnostics over ``(predicted probability, outcome)`` samples, so they
apply unchanged to this project's forecasting problem — funding persistence,
``P(funding stays above the cost threshold over the next N settlements)`` —
even though the underlying domain is entirely different.

* **Reliability / Brier / ECE** for binary threshold predictions — bucket predicted
  probabilities and compare each bucket's mean prediction to the observed frequency.
* **PIT histogram** — where the observed value falls within a forecast ensemble.
  A calibrated ensemble is uniform; a U-shape means under-dispersion
  (over-confident), a hump means over-dispersion.
* **CRPS** — a proper score for the whole predictive distribution (lower is better).

All functions are pure and take plain samples, so they are exhaustively testable
and reusable by a backtester.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReliabilityBin:
    lower: float  # bin's predicted-probability range [lower, upper)
    upper: float
    count: int
    mean_predicted: float  # mean predicted probability of samples in the bin
    empirical_freq: float  # observed frequency of the positive outcome in the bin


@dataclass(frozen=True, slots=True)
class ReliabilityReport:
    n: int
    brier: float  # mean squared error of the probability, lower is better
    ece: float  # expected calibration error (count-weighted |pred - obs|)
    bins: tuple[ReliabilityBin, ...]


def reliability(samples: Sequence[tuple[float, bool]], *, bins: int = 10) -> ReliabilityReport:
    """Reliability diagram, Brier score, and ECE for (predicted_prob, outcome)."""
    if bins < 1:
        raise ValueError("bins must be >= 1")
    n = len(samples)
    if n == 0:
        return ReliabilityReport(n=0, brier=0.0, ece=0.0, bins=())

    brier = sum((p - float(o)) ** 2 for p, o in samples) / n

    counts = [0] * bins
    pred_sum = [0.0] * bins
    pos_sum = [0] * bins
    for p, outcome in samples:
        # p == 1.0 belongs in the last bin, not a phantom bin `bins`.
        idx = min(int(p * bins), bins - 1)
        counts[idx] += 1
        pred_sum[idx] += p
        pos_sum[idx] += int(outcome)

    out: list[ReliabilityBin] = []
    ece = 0.0
    for i in range(bins):
        if counts[i] == 0:
            continue
        mean_pred = pred_sum[i] / counts[i]
        freq = pos_sum[i] / counts[i]
        ece += counts[i] / n * abs(mean_pred - freq)
        out.append(
            ReliabilityBin(
                lower=i / bins,
                upper=(i + 1) / bins,
                count=counts[i],
                mean_predicted=mean_pred,
                empirical_freq=freq,
            )
        )
    return ReliabilityReport(n=n, brier=brier, ece=ece, bins=tuple(out))


def pit_value(members: Sequence[float], observed: float) -> float:
    """Probability-integral-transform value in [0, 1]: the mid-rank fraction of
    ensemble members at or below ``observed`` (handles ties)."""
    if not members:
        raise ValueError("pit_value requires a non-empty ensemble")
    below = sum(1 for m in members if m < observed)
    ties = sum(1 for m in members if m == observed)
    return (below + 0.5 * ties) / len(members)


def pit_histogram(
    pairs: Sequence[tuple[Sequence[float], float]], *, bins: int = 10
) -> tuple[int, ...]:
    """Histogram (``bins`` equal-width buckets over [0, 1]) of PIT values. Uniform
    counts indicate a calibrated ensemble spread."""
    if bins < 1:
        raise ValueError("bins must be >= 1")
    counts = [0] * bins
    for members, observed in pairs:
        idx = min(int(pit_value(members, observed) * bins), bins - 1)
        counts[idx] += 1
    return tuple(counts)


def crps_ensemble(members: Sequence[float], observed: float) -> float:
    """Continuous Ranked Probability Score for an empirical ensemble (lower better):
    ``mean|x - y| - 0.5 * mean|x - x'|``."""
    n = len(members)
    if n == 0:
        raise ValueError("crps_ensemble requires a non-empty ensemble")
    mad_obs = sum(abs(x - observed) for x in members) / n
    spread = sum(abs(x - y) for x in members for y in members) / (n * n)
    return mad_obs - 0.5 * spread


def brier_skill_score(model_brier: float, baseline_brier: float) -> float:
    """Skill of ``model_brier`` against ``baseline_brier``: ``1 - model/baseline``.

    Positive means the model beats the baseline; zero means it merely matches it.
    Promotion gate 4 (see :mod:`domain.promotion`) scores the funding-persistence
    model against naive persistence — "funding will be what it was last period" —
    and that comparison is the whole reason the model layer is allowed to exist.

    A baseline that is already perfect (``0.0``) admits no skill improvement, so
    it returns ``0.0`` rather than dividing by zero.
    """
    if model_brier < 0 or baseline_brier < 0:
        raise ValueError("Brier scores cannot be negative")
    if baseline_brier == 0:
        return 0.0
    return 1.0 - model_brier / baseline_brier
