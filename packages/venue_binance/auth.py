"""Request signing — the single point of correction for authentication (ADR-0003).

**Unverified.** Nothing in this repository has ever sent a signed request; every
endpoint exercised on 2026-08-09 was public. The one authenticated call attempted
(`leverageBracket`) was rejected at the key-format stage with HTTP 401
``-2014``, before any signature was checked, so the signing format below is still
documentation plus reasoning — exactly the state ADR-0003 describes.

What is implemented, and why it is shaped this way:

* Binance signs the **exact query string that is sent**, so the signature must be
  computed over an already-serialised string and that same string must go on the
  wire. Re-encoding the parameters after signing — which any dict-based query
  builder will happily do — produces a valid-looking request with a signature
  over different bytes. `signed_query()` therefore returns the final string.
* ``timestamp`` is milliseconds since epoch, and ``recvWindow`` bounds how long
  the venue will accept it.
* Comparison of any signature we ever verify uses `hmac.compare_digest`.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Sequence
from datetime import UTC, datetime
from urllib.parse import urlencode

#: Binance rejects a request whose timestamp is outside this window. Five seconds
#: is the documented default; a smaller window is safer against replay and more
#: fragile against clock drift, so it is configurable per signer.
DEFAULT_RECV_WINDOW_MS = 5_000


class BinanceSigner:
    """Signs USDⓈ-M / spot REST requests with HMAC-SHA256.

    The secret is held here and nowhere else. It is never logged, never returned,
    and never placed on an exception. `observability.logging` additionally
    redacts ``signature=`` from any string that reaches a log, so a leaked URL
    does not become a leaked credential.
    """

    __slots__ = ("_api_key", "_recv_window_ms", "_secret")

    def __init__(
        self,
        api_key: str,
        secret: str,
        *,
        recv_window_ms: int = DEFAULT_RECV_WINDOW_MS,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Binance API key cannot be empty")
        if not secret.strip():
            raise ValueError("Binance API secret cannot be empty")
        if recv_window_ms <= 0 or recv_window_ms > 60_000:
            raise ValueError("recvWindow must be in (0, 60000] milliseconds")
        self._api_key = api_key.strip()
        self._secret = secret.strip().encode("utf-8")
        self._recv_window_ms = recv_window_ms

    @property
    def api_key(self) -> str:
        """The key identifier, which is sent as a header and is not secret."""
        return self._api_key

    def headers(self) -> dict[str, str]:
        return {"X-MBX-APIKEY": self._api_key}

    def sign(self, query_string: str) -> str:
        """Hex HMAC-SHA256 of ``query_string`` under the secret."""
        return hmac.new(self._secret, query_string.encode("utf-8"), hashlib.sha256).hexdigest()

    def signed_query(
        self,
        params: Sequence[tuple[str, str | int]] = (),
        *,
        now: datetime | None = None,
    ) -> str:
        """Return the complete query string, ending in its own signature.

        Parameters are an ordered sequence rather than a dict because the signed
        bytes are the sent bytes: the caller must control ordering, and a dict
        invites a later `sorted()` that silently changes what was signed.
        """
        moment = now or datetime.now(UTC)
        ordered: list[tuple[str, str | int]] = [
            *params,
            ("timestamp", int(moment.timestamp() * 1000)),
            ("recvWindow", self._recv_window_ms),
        ]
        query = urlencode(ordered)
        return f"{query}&signature={self.sign(query)}"

    def verify(self, query_string: str, signature: str) -> bool:
        """Constant-time check that ``signature`` matches ``query_string``."""
        return hmac.compare_digest(self.sign(query_string), signature)

    def __repr__(self) -> str:  # pragma: no cover - trivial, but keeps secrets out
        return f"BinanceSigner(api_key={self._api_key[:4]}...)"
