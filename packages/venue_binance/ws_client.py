"""Combined-stream WebSocket consumer with reconnect and gap detection.

The transport is injected as a factory rather than imported, so every behaviour
below — reconnect, backoff, gap detection, sequence resets — is tested against a
scripted fake with no network and no sleeping. A WebSocket client that can only
be tested against the live venue is a client whose failure paths are never tested,
and the failure paths are the entire point.

**Gap detection is the reason this exists.** A dropped frame does not announce
itself: the stream simply continues, one update poorer, and every book built from
it is subtly wrong forever after. Binance stamps each `bookTicker` frame with a
monotonically increasing update id (``u``), so a skipped id is detectable — and
the correct response is to treat the local view as invalid and resynchronise,
not to carry on.

What is verified and what is not (ADR-0003): the combined-stream envelope
``{"stream": ..., "data": {...}}`` and the `bookTicker` payload were recorded on
2026-08-09. The `markPrice` and `kline` payload shapes were **not** — the probe
connection began timing out first — so this module deliberately handles only the
frame it has seen and passes anything else through as an unrecognised payload
rather than guessing at its fields.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from domain.instrument import VenueEnvironment
from venue_binance.endpoints import BinanceEndpoints, Market
from venue_binance.schemas import BookTickerStreamWire, CombinedStreamWire

log = logging.getLogger("venue_binance.ws")

DEFAULT_INITIAL_BACKOFF_SECONDS = 1.0
DEFAULT_MAX_BACKOFF_SECONDS = 60.0
#: Binance closes a connection after 24 hours regardless of health, so a
#: long-lived consumer must treat reconnection as routine, not exceptional.
DEFAULT_MAX_CONNECTION_SECONDS = 23 * 3600


class WebSocketConnection(Protocol):
    """The slice of a WebSocket this module uses."""

    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


ConnectFactory = Callable[[str], Awaitable[WebSocketConnection]]


@dataclass(frozen=True, slots=True)
class StreamFrame:
    """One demultiplexed frame."""

    stream: str
    payload: dict[str, object]
    received_at: datetime


@dataclass(frozen=True, slots=True)
class SequenceGap:
    """A detected discontinuity in a stream's update ids."""

    stream: str
    expected_after: int
    received: int
    detected_at: datetime

    @property
    def missed(self) -> int:
        return max(0, self.received - self.expected_after - 1)


@dataclass
class SequenceTracker:
    """Per-stream monotonic update-id tracking.

    Three distinct cases, and conflating any two of them hides a real fault:

    * **advance** — the id increased; normal.
    * **gap** — the id increased by more than one; frames were lost.
    * **regression** — the id decreased or repeated. This is *not* a gap. It
      means the venue reset its sequence (a reconnect on their side, or a
      symbol re-listing), and the local view must be rebuilt rather than
      patched.
    """

    last_seen: dict[str, int] = field(default_factory=dict)

    def observe(self, stream: str, update_id: int | None, *, at: datetime) -> SequenceGap | None:
        if update_id is None:
            return None
        previous = self.last_seen.get(stream)
        self.last_seen[stream] = update_id
        if previous is None:
            return None
        if update_id <= previous:
            log.warning(
                "stream sequence regressed; local view must be rebuilt",
                extra={"stream": stream, "previous": previous, "received": update_id},
            )
            return None
        if update_id > previous + 1:
            return SequenceGap(
                stream=stream, expected_after=previous, received=update_id, detected_at=at
            )
        return None

    def reset(self, stream: str | None = None) -> None:
        """Forget sequence state, on reconnect or after a resynchronisation."""
        if stream is None:
            self.last_seen.clear()
        else:
            self.last_seen.pop(stream, None)


def backoff_delay(
    attempt: int,
    *,
    initial: float = DEFAULT_INITIAL_BACKOFF_SECONDS,
    maximum: float = DEFAULT_MAX_BACKOFF_SECONDS,
) -> float:
    """Exponential backoff, capped. ``attempt`` is 1-based.

    Deterministic rather than jittered because there is exactly one consumer of
    this venue on this host — jitter exists to de-synchronise a fleet, and adding
    it here would only make the behaviour harder to test.
    """
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    scaled: float = initial * float(2 ** (attempt - 1))
    return min(maximum, scaled)


class BinanceStreamConsumer:
    """Consumes combined streams, reconnecting and reporting gaps.

    Yields :class:`StreamFrame` objects forever. Connection failures are retried
    with capped exponential backoff; the caller sees a continuous frame sequence
    and is told about gaps through ``on_gap`` rather than by inference.
    """

    def __init__(
        self,
        *,
        environment: VenueEnvironment,
        market: Market,
        streams: tuple[str, ...],
        connect: ConnectFactory,
        on_gap: Callable[[SequenceGap], None] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_reconnects: int | None = None,
    ) -> None:
        if not streams:
            raise ValueError("at least one stream is required")
        self._endpoints = BinanceEndpoints(environment)
        self._market = market
        self._streams = streams
        self._connect = connect
        self._on_gap = on_gap
        self._sleep = sleep
        self._max_reconnects = max_reconnects
        self._tracker = SequenceTracker()
        self._reconnects = 0

    @property
    def url(self) -> str:
        return self._endpoints.combined_stream_url(self._market, self._streams)

    @property
    def reconnects(self) -> int:
        return self._reconnects

    async def frames(self) -> AsyncIterator[StreamFrame]:
        attempt = 0
        while True:
            try:
                connection = await self._connect(self.url)
            except Exception as exc:
                attempt += 1
                if self._max_reconnects is not None and self._reconnects >= self._max_reconnects:
                    raise
                self._reconnects += 1
                delay = backoff_delay(attempt)
                log.warning(
                    "stream connect failed; backing off",
                    extra={"attempt": attempt, "delay_seconds": delay, "error": str(exc)},
                )
                await self._sleep(delay)
                continue

            attempt = 0
            # A new connection means the venue's sequence may have restarted, so
            # the first frame after reconnect must never be compared against a
            # pre-disconnect id — that would report a spurious gap of millions.
            self._tracker.reset()
            try:
                async for frame in self._read(connection):
                    yield frame
            except StopAsyncIteration:
                return
            finally:
                with_close = getattr(connection, "close", None)
                if with_close is not None:
                    await connection.close()

            if self._max_reconnects is not None and self._reconnects >= self._max_reconnects:
                return
            self._reconnects += 1

    async def _read(self, connection: WebSocketConnection) -> AsyncIterator[StreamFrame]:
        while True:
            try:
                message = await connection.recv()
            except Exception as exc:
                log.warning("stream read failed; reconnecting", extra={"error": str(exc)})
                return
            frame = self._decode(message)
            if frame is None:
                continue
            gap = self._tracker.observe(
                frame.stream, _update_id(frame.payload), at=frame.received_at
            )
            if gap is not None:
                log.warning(
                    "sequence gap detected",
                    extra={"stream": gap.stream, "missed": gap.missed},
                )
                if self._on_gap is not None:
                    self._on_gap(gap)
            yield frame

    def _decode(self, message: str | bytes) -> StreamFrame | None:
        try:
            parsed = json.loads(message)
        except json.JSONDecodeError:
            log.warning("stream frame was not JSON; dropped")
            return None
        if not isinstance(parsed, dict):
            return None
        envelope = CombinedStreamWire.model_validate(parsed)
        if envelope.stream is None:
            # Control frames (subscription acks) have no stream name.
            return None
        return StreamFrame(
            stream=envelope.stream,
            payload=envelope.data,
            received_at=datetime.now(UTC),
        )


def _update_id(payload: dict[str, object]) -> int | None:
    """The book-ticker update id, when the payload carries one."""
    value = payload.get("u")
    return value if isinstance(value, int) else None


def parse_book_ticker_frame(frame: StreamFrame) -> BookTickerStreamWire:
    """Validate a `bookTicker` payload. The one WS shape we have recorded."""
    return BookTickerStreamWire.model_validate(frame.payload)


def book_ticker_stream(symbol: str) -> str:
    return f"{symbol.lower()}@bookTicker"


def mark_price_stream(symbol: str, *, fast: bool = True) -> str:
    """Mark-price stream name. **Payload shape unverified** — see module docstring."""
    return f"{symbol.lower()}@markPrice@1s" if fast else f"{symbol.lower()}@markPrice"
