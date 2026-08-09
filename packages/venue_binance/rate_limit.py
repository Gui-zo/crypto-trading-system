"""Request-weight budget, driven by the venue's own accounting.

Binance meters requests by *weight*, not count, and reports the running total in
a response header. Two things about that header were confirmed on 2026-08-09 and
shape this module:

* The name is ``X-MBX-USED-WEIGHT-1M`` and HTTP/2 delivers it **lower-cased**
  (``x-mbx-used-weight-1m``). Header lookup must be case-insensitive.
* Spot returns *both* ``x-mbx-used-weight`` and ``x-mbx-used-weight-1m``; USDⓈ-M
  returns only the ``-1m`` form. Reading the unsuffixed name alone would find
  nothing on futures, and the budget would silently never update.

The design principle: **the venue's number wins.** Local estimates exist only to
refuse a request *before* sending it, and are overwritten by the authoritative
header on every response. A budget that trusts its own arithmetic drifts, and it
drifts in the dangerous direction — under-counting — because retries, redirects,
and shared IPs all add weight we never attributed.

The observed USDⓈ-M ceiling was 2400/minute (from the `exchangeInfo` envelope),
but the limit is read from the venue rather than hardcoded; the constant in
`endpoints` is a conservative fallback for the moment before the first response.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

USED_WEIGHT_HEADERS = ("x-mbx-used-weight-1m", "x-mbx-used-weight")
RETRY_AFTER_HEADER = "retry-after"


@dataclass(frozen=True, slots=True)
class WeightSnapshot:
    """What the venue said about our usage, and when it said it."""

    used: int
    limit: int
    observed_at: datetime

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def utilisation(self) -> float:
        return 0.0 if self.limit <= 0 else min(1.0, self.used / self.limit)


class RateLimitExceeded(RuntimeError):
    """Raised when a request would exceed the configured safety threshold.

    This is a *local* refusal, before anything is sent. Being refused here is
    always preferable to a 429, and enormously preferable to the 418 IP ban that
    follows ignoring 429s.
    """


class RateLimitBudget:
    """Tracks request weight within the rolling minute window.

    The window is approximated as a fixed minute bucket keyed off the last
    observation. That is deliberately cruder than Binance's true rolling window
    and errs toward *over*-counting, which is the safe direction: the worst
    outcome is a request refused a little early.
    """

    def __init__(
        self,
        *,
        limit_per_minute: int,
        safety_fraction: float = 0.75,
        now: datetime | None = None,
    ) -> None:
        if limit_per_minute <= 0:
            raise ValueError("weight limit must be positive")
        if not 0 < safety_fraction <= 1:
            raise ValueError("safety fraction must be in (0, 1]")
        self._limit = limit_per_minute
        self._safety_fraction = safety_fraction
        self._used = 0
        self._window_started = now or datetime.now(UTC)

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def threshold(self) -> int:
        """The local ceiling: we stop here, well below the venue's own limit."""
        return int(self._limit * self._safety_fraction)

    def snapshot(self, *, now: datetime | None = None) -> WeightSnapshot:
        moment = now or datetime.now(UTC)
        self._roll(moment)
        return WeightSnapshot(used=self._used, limit=self._limit, observed_at=moment)

    def _roll(self, now: datetime) -> None:
        if now - self._window_started >= timedelta(minutes=1):
            self._used = 0
            self._window_started = now

    def check(self, weight: int, *, now: datetime | None = None) -> None:
        """Refuse a request that would cross the local threshold."""
        if weight < 0:
            raise ValueError("request weight cannot be negative")
        moment = now or datetime.now(UTC)
        self._roll(moment)
        if self._used + weight > self.threshold:
            raise RateLimitExceeded(
                f"request weight {weight} would take used weight to "
                f"{self._used + weight}, past the local threshold {self.threshold} "
                f"(venue limit {self._limit})"
            )

    def charge(self, weight: int, *, now: datetime | None = None) -> None:
        """Record an estimated cost locally, pending the authoritative header."""
        moment = now or datetime.now(UTC)
        self._roll(moment)
        self._used += weight

    def observe_headers(
        self, headers: Mapping[str, str], *, now: datetime | None = None
    ) -> WeightSnapshot | None:
        """Adopt the venue's usage figure, overriding local estimates.

        Returns ``None`` when no usage header is present, which is normal for
        WebSocket handshakes and some error responses — the caller keeps its
        local estimate rather than resetting to zero.
        """
        moment = now or datetime.now(UTC)
        self._roll(moment)
        lowered = {key.lower(): value for key, value in headers.items()}
        for name in USED_WEIGHT_HEADERS:
            raw = lowered.get(name)
            if raw is None:
                continue
            try:
                used = int(raw)
            except ValueError:
                continue
            # Trust the venue even when it is *lower* than our estimate: the
            # window may have rolled on their side.
            self._used = used
            self._window_started = min(self._window_started, moment)
            return WeightSnapshot(used=used, limit=self._limit, observed_at=moment)
        return None

    def adopt_limit(self, limit_per_minute: int) -> None:
        """Replace the limit with the value read from `exchangeInfo`."""
        if limit_per_minute <= 0:
            raise ValueError("weight limit must be positive")
        self._limit = limit_per_minute


def parse_retry_after(headers: Mapping[str, str]) -> float | None:
    """Seconds from a ``Retry-After`` header, if the venue sent one."""
    lowered = {key.lower(): value for key, value in headers.items()}
    raw = lowered.get(RETRY_AFTER_HEADER)
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None
