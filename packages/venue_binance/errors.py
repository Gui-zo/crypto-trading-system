"""Venue errors as typed exceptions — the single point of correction for status
and error-code interpretation (ADR-0003).

Binance signals failure two ways at once: an HTTP status *and* a negative ``code``
in a JSON body. Both were recorded on 2026-08-09:

* ``HTTP 400`` with ``{"code": -1121, "msg": "Invalid symbol."}``
* ``HTTP 401`` with ``{"code": -2014, "msg": "API-key format invalid."}``

The distinction that matters operationally is between *this request was wrong*
(never retry — the symbol does not exist) and *you are going too fast* (back off,
and on 418 stop entirely because the IP is banned). Conflating them produces a
retry loop that turns a rate-limit warning into a ban.
"""

from __future__ import annotations

from dataclasses import dataclass


class BinanceError(Exception):
    """Base class for every failure originating at the venue boundary."""


class BinanceTransportError(BinanceError):
    """The request never produced a usable response (DNS, TLS, timeout, reset)."""


class BinanceAuthenticationError(BinanceError):
    """A signed read was requested without locally configured credentials."""


@dataclass
class BinanceAPIError(BinanceError):
    """The venue answered with an error status and, usually, a coded body."""

    status_code: int
    code: int | None
    message: str
    path: str

    def __post_init__(self) -> None:
        super().__init__(f"{self.path} -> HTTP {self.status_code} code={self.code}: {self.message}")

    @property
    def is_invalid_symbol(self) -> bool:
        """``-1121``. The symbol does not exist; retrying cannot help."""
        return self.code == -1121

    @property
    def is_auth_problem(self) -> bool:
        """Missing, malformed, or unauthorised key. Also cannot be retried."""
        return self.status_code in {401, 403} or self.code in {-2014, -2015, -1022}

    @property
    def is_retryable(self) -> bool:
        """Whether a later identical request could plausibly succeed.

        5xx only. A 4xx means the request itself was wrong, and 429/418 are
        handled by :class:`BinanceRateLimitError` rather than here so a caller
        cannot accidentally treat a ban as a transient blip.
        """
        return self.status_code >= 500


@dataclass
class BinanceRateLimitError(BinanceError):
    """HTTP 429 (slow down) or 418 (IP banned for ignoring 429s).

    ``retry_after`` comes from the ``Retry-After`` header when present. **This
    path has not been observed** — the 2026-08-09 capture stayed far inside the
    limit — so the handling here is written from documentation and is flagged as
    unverified in `tests/fixtures/binance/README.md`.
    """

    status_code: int
    retry_after_seconds: float | None
    path: str

    def __post_init__(self) -> None:
        super().__init__(
            f"{self.path} -> HTTP {self.status_code} rate limited; "
            f"retry_after={self.retry_after_seconds}"
        )

    @property
    def is_ip_banned(self) -> bool:
        """418. Not a backoff signal — the IP is banned and must stop entirely."""
        return self.status_code == 418
