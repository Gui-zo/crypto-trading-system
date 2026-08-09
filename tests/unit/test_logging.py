"""Structured-logging tests, with credential redaction as the point.

The two leaks this guards against are real and boring: a signed request URL in an
exception traceback, and an API key echoed in a debug log line. Redaction lives
in the formatter so no call site can forget it.
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from observability.logging import REDACTED, JsonFormatter, configure_logging, redact


@pytest.fixture
def captured() -> io.StringIO:
    stream = io.StringIO()
    configure_logging(level="DEBUG", environment="testnet", source="TEST", stream=stream)
    return stream


def lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def test_records_are_json_lines_with_environment_and_source(captured: io.StringIO) -> None:
    logging.getLogger("test").info("hello")
    (record,) = lines(captured)
    assert record["message"] == "hello"
    assert record["environment"] == "testnet"
    assert record["source"] == "TEST"
    assert record["level"] == "INFO"


def test_structured_extras_are_merged_into_the_record(captured: io.StringIO) -> None:
    logging.getLogger("test").info("scan", extra={"symbol": "BTCUSDT", "candidates": 3})
    (record,) = lines(captured)
    assert record["symbol"] == "BTCUSDT"
    assert record["candidates"] == 3


@pytest.mark.parametrize(
    "message",
    [
        "GET /fapi/v1/account?timestamp=1&signature=deadbeefcafe",
        "headers: X-MBX-APIKEY: abc123def456",
        "connecting with listenKey=xyz789",
        "secret: hunter2",
    ],
)
def test_credentials_are_redacted_from_messages(captured: io.StringIO, message: str) -> None:
    logging.getLogger("test").info(message)
    (record,) = lines(captured)
    rendered = str(record["message"])
    assert REDACTED in rendered
    for leaked in ("deadbeefcafe", "abc123def456", "xyz789", "hunter2"):
        assert leaked not in rendered


def test_redaction_preserves_the_surrounding_text() -> None:
    redacted = redact("url?symbol=BTCUSDT&signature=abc")
    assert redacted == f"url?symbol=BTCUSDT&signature={REDACTED}"


def test_redaction_is_case_insensitive() -> None:
    assert REDACTED in redact("Signature=abc")
    assert REDACTED in redact("API_KEY: abc")


def test_a_message_with_no_credentials_is_untouched() -> None:
    assert redact("funding rate 0.0001 for BTCUSDT") == "funding rate 0.0001 for BTCUSDT"


def test_credentials_are_redacted_from_tracebacks(captured: io.StringIO) -> None:
    try:
        raise RuntimeError("request failed: /account?signature=deadbeefcafe")
    except RuntimeError:
        logging.getLogger("test").exception("call failed")
    (record,) = lines(captured)
    assert "deadbeefcafe" not in json.dumps(record)
    assert REDACTED in str(record["exception"])


def test_credentials_are_redacted_from_structured_extras(captured: io.StringIO) -> None:
    logging.getLogger("test").info("req", extra={"url": "/x?signature=deadbeefcafe"})
    (record,) = lines(captured)
    assert "deadbeefcafe" not in json.dumps(record)


def test_configure_logging_replaces_existing_handlers() -> None:
    root = logging.getLogger()
    root.addHandler(logging.StreamHandler(io.StringIO()))
    configure_logging(level="INFO", environment="testnet", source="TEST", stream=io.StringIO())
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonFormatter)


def test_the_level_is_honoured(captured: io.StringIO) -> None:
    configure_logging(level="WARNING", environment="testnet", source="TEST", stream=captured)
    logging.getLogger("test").info("suppressed")
    logging.getLogger("test").warning("kept")
    (record,) = lines(captured)
    assert record["message"] == "kept"


def test_the_source_defaults_to_the_cron_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLATFORM_RUN_SOURCE", "CRON")
    stream = io.StringIO()
    configure_logging(level="INFO", environment="production", stream=stream)
    logging.getLogger("test").info("scheduled")
    (record,) = lines(stream)
    assert record["source"] == "CRON"
