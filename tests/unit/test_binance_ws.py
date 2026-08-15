"""WebSocket consumer tests: reconnect, backoff, and gap detection.

All of it runs against a scripted fake connection with a fake clock — no network,
no sleeping. These are the paths that only execute when something has already
gone wrong, so testing them against the live venue would mean never testing them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from domain.instrument import VenueEnvironment
from venue_binance.endpoints import BinanceEndpoints, Market
from venue_binance.ws_client import (
    BinanceStreamConsumer,
    SequenceTracker,
    backoff_delay,
    book_ticker_stream,
    parse_book_ticker_frame,
)

NOW = datetime(2026, 8, 9, 16, 0, tzinfo=UTC)


def frame(update_id: int, *, stream: str = "btcusdt@bookTicker") -> str:
    return json.dumps(
        {
            "stream": stream,
            "data": {
                "e": "bookTicker",
                "u": update_id,
                "s": "BTCUSDT",
                "b": "65091.30",
                "B": "4.167",
                "a": "65091.40",
                "A": "13.549",
                "T": 1786310574270,
                "E": 1786310574270,
            },
        }
    )


class FakeConnection:
    """Replays scripted messages, then raises to simulate a dropped connection."""

    def __init__(self, messages: list[str], *, then: Exception | None = None) -> None:
        self._messages = list(messages)
        self._then = then or ConnectionResetError("connection closed")
        self.closed = False

    async def recv(self) -> str:
        if self._messages:
            return self._messages.pop(0)
        raise self._then

    async def close(self) -> None:
        self.closed = True


class Script:
    """A connect factory that hands out prepared connections in order."""

    def __init__(self, *connections: FakeConnection | Exception) -> None:
        self._queue = list(connections)
        self.urls: list[str] = []

    async def __call__(self, url: str) -> FakeConnection:
        self.urls.append(url)
        if not self._queue:
            raise ConnectionRefusedError("no more connections scripted")
        nxt = self._queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class FakeClock:
    """Records what the consumer *would* have slept, without sleeping."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


async def collect(consumer: BinanceStreamConsumer, count: int) -> list[object]:
    out: list[object] = []
    async for item in consumer.frames():
        out.append(item)
        if len(out) >= count:
            break
    return out


def consumer(script: Script, clock: FakeClock, **kwargs: object) -> BinanceStreamConsumer:
    defaults: dict[str, object] = {
        "environment": VenueEnvironment.PRODUCTION,
        "market": Market.USDM,
        "streams": (book_ticker_stream("BTCUSDT"),),
        "connect": script,
        "sleep": clock,
    }
    defaults.update(kwargs)
    return BinanceStreamConsumer(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Sequence tracking
# ---------------------------------------------------------------------------


def test_consecutive_ids_report_no_gap() -> None:
    tracker = SequenceTracker()
    assert tracker.observe("s", 1, at=NOW) is None
    assert tracker.observe("s", 2, at=NOW) is None


def test_a_skipped_id_is_reported_with_the_count_missed() -> None:
    tracker = SequenceTracker()
    tracker.observe("s", 10, at=NOW)
    gap = tracker.observe("s", 15, at=NOW)
    assert gap is not None
    assert gap.expected_after == 10
    assert gap.received == 15
    assert gap.missed == 4


def test_the_first_frame_on_a_stream_cannot_be_a_gap() -> None:
    assert SequenceTracker().observe("s", 999_999, at=NOW) is None


def test_a_regressed_id_is_not_reported_as_a_gap() -> None:
    """A decrease means the venue reset its sequence, not that we lost frames."""
    tracker = SequenceTracker()
    tracker.observe("s", 100, at=NOW)
    assert tracker.observe("s", 5, at=NOW) is None
    assert tracker.observe("s", 6, at=NOW) is None


def test_a_repeated_id_is_not_a_gap() -> None:
    tracker = SequenceTracker()
    tracker.observe("s", 7, at=NOW)
    assert tracker.observe("s", 7, at=NOW) is None


def test_streams_are_tracked_independently() -> None:
    tracker = SequenceTracker()
    tracker.observe("a", 100, at=NOW)
    tracker.observe("b", 5, at=NOW)
    assert tracker.observe("a", 101, at=NOW) is None
    assert tracker.observe("b", 6, at=NOW) is None


def test_a_payload_without_an_update_id_is_skipped_not_guessed() -> None:
    assert SequenceTracker().observe("s", None, at=NOW) is None


def test_reset_clears_tracking() -> None:
    tracker = SequenceTracker()
    tracker.observe("s", 100, at=NOW)
    tracker.reset()
    assert tracker.observe("s", 500, at=NOW) is None


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------


def test_backoff_doubles_and_is_capped() -> None:
    assert backoff_delay(1) == 1.0
    assert backoff_delay(2) == 2.0
    assert backoff_delay(4) == 8.0
    assert backoff_delay(50) == 60.0


def test_a_nonpositive_attempt_is_refused() -> None:
    with pytest.raises(ValueError, match="attempt must be"):
        backoff_delay(0)


# ---------------------------------------------------------------------------
# Consumer
# ---------------------------------------------------------------------------


async def test_frames_are_demultiplexed_from_the_combined_envelope() -> None:
    script = Script(FakeConnection([frame(1), frame(2)]))
    frames = await collect(consumer(script, FakeClock()), 2)
    assert [f.stream for f in frames] == ["btcusdt@bookTicker"] * 2  # type: ignore[attr-defined]
    assert frames[0].payload["u"] == 1  # type: ignore[attr-defined]


async def test_the_combined_stream_url_is_used() -> None:
    script = Script(FakeConnection([frame(1)]))
    await collect(consumer(script, FakeClock()), 1)
    assert script.urls[0] == BinanceEndpoints(VenueEnvironment.PRODUCTION).combined_stream_url(
        Market.USDM, ("btcusdt@bookTicker",)
    )


async def test_a_dropped_connection_reconnects_and_keeps_yielding() -> None:
    script = Script(FakeConnection([frame(1)]), FakeConnection([frame(2), frame(3)]))
    frames = await collect(consumer(script, FakeClock()), 3)
    assert len(frames) == 3
    assert len(script.urls) == 2


async def test_connect_failures_back_off_with_increasing_delays() -> None:
    clock = FakeClock()
    script = Script(
        ConnectionRefusedError("down"),
        ConnectionRefusedError("still down"),
        FakeConnection([frame(1)]),
    )
    await collect(consumer(script, clock), 1)
    assert clock.delays == [1.0, 2.0]


async def test_backoff_resets_after_a_successful_connection() -> None:
    clock = FakeClock()
    script = Script(
        ConnectionRefusedError("down"),
        FakeConnection([frame(1)]),
        ConnectionRefusedError("down again"),
        FakeConnection([frame(2)]),
    )
    await collect(consumer(script, clock), 2)
    assert clock.delays == [1.0, 1.0]


async def test_reconnecting_does_not_report_a_spurious_gap() -> None:
    """The venue's sequence may restart, so ids must not span a reconnect.

    Without the reset this reports a gap of ~11 billion and the caller
    resynchronises for no reason on every single reconnect.
    """
    gaps: list[object] = []
    script = Script(
        FakeConnection([frame(11_249_981_101_619)]),
        FakeConnection([frame(1), frame(2)]),
    )
    await collect(consumer(script, FakeClock(), on_gap=gaps.append), 3)
    assert gaps == []


async def test_a_gap_within_one_connection_is_reported() -> None:
    gaps: list[object] = []
    script = Script(FakeConnection([frame(1), frame(9)]))
    await collect(consumer(script, FakeClock(), on_gap=gaps.append), 2)
    assert len(gaps) == 1
    assert gaps[0].missed == 7  # type: ignore[attr-defined]


async def test_the_frame_that_revealed_the_gap_is_still_delivered() -> None:
    """A gap invalidates the local view; it does not make the frame worthless."""
    script = Script(FakeConnection([frame(1), frame(9)]))
    frames = await collect(consumer(script, FakeClock()), 2)
    assert frames[1].payload["u"] == 9  # type: ignore[attr-defined]


async def test_malformed_frames_are_dropped_without_killing_the_stream() -> None:
    script = Script(FakeConnection(["not json at all", frame(1)]))
    frames = await collect(consumer(script, FakeClock()), 1)
    assert frames[0].payload["u"] == 1  # type: ignore[attr-defined]


async def test_control_frames_without_a_stream_name_are_skipped() -> None:
    script = Script(FakeConnection([json.dumps({"result": None, "id": 1}), frame(1)]))
    frames = await collect(consumer(script, FakeClock()), 1)
    assert frames[0].stream == "btcusdt@bookTicker"  # type: ignore[attr-defined]


async def test_connections_are_closed_on_reconnect() -> None:
    first = FakeConnection([frame(1)])
    script = Script(first, FakeConnection([frame(2)]))
    await collect(consumer(script, FakeClock()), 2)
    assert first.closed


async def test_max_reconnects_bounds_the_retry_loop() -> None:
    script = Script(ConnectionRefusedError("down"), ConnectionRefusedError("down"))
    with pytest.raises(ConnectionRefusedError):
        await collect(consumer(script, FakeClock(), max_reconnects=1), 1)


def test_at_least_one_stream_is_required() -> None:
    with pytest.raises(ValueError, match="at least one stream"):
        BinanceStreamConsumer(
            environment=VenueEnvironment.PRODUCTION,
            market=Market.USDM,
            streams=(),
            connect=Script(),
        )


async def test_a_delivered_frame_parses_as_a_book_ticker() -> None:
    script = Script(FakeConnection([frame(1)]))
    frames = await collect(consumer(script, FakeClock()), 1)
    wire = parse_book_ticker_frame(frames[0])  # type: ignore[arg-type]
    assert wire.s == "BTCUSDT"
    assert wire.b == "65091.30"
