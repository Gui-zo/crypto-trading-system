"""Structured JSON logging with credential redaction.

Logs are JSON lines so a cron log can be grepped, shipped, or queried without a
parser that has to guess at field boundaries. Every record carries the venue
environment (ADR-0010) and the run source, because the first question about any
line in ``data/logs/`` is "was that testnet or production?".

The redaction filter is not decoration. This project holds a Binance HMAC secret
that authorizes withdrawals-adjacent account access, and the two classic leaks are
an exception traceback that includes a signed URL and a debug log of a request
header. Redaction happens at the formatter, so it applies to every handler and
cannot be bypassed by a call site that forgot.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from typing import Any

#: Query parameters and header names whose values must never reach a log.
_SENSITIVE_KEYS = ("signature", "apikey", "api_key", "secret", "token", "password", "listenkey")

_SENSITIVE_PATTERN = re.compile(
    r"(?i)\b("
    + "|".join(re.escape(key) for key in _SENSITIVE_KEYS)
    + r")\b\s*[=:]\s*([^\s&,;\"']+)"
)

REDACTED = "***REDACTED***"

#: Attributes the stdlib puts on every record; anything else is a caller's own
#: structured field and gets merged into the JSON output.
_STANDARD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
    | {"message", "asctime", "taskName"}
)


def redact(text: str) -> str:
    """Mask the value of any sensitive key/value pair in ``text``."""
    return _SENSITIVE_PATTERN.sub(lambda m: f"{m.group(1)}={REDACTED}", text)


class JsonFormatter(logging.Formatter):
    """Render a record as a single JSON line, redacting credentials."""

    def __init__(self, *, environment: str, source: str) -> None:
        super().__init__()
        self._environment = environment
        self._source = source

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "environment": self._environment,
            "source": self._source,
            "message": redact(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = redact(value) if isinstance(value, str) else value
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(
    *,
    level: str = "INFO",
    environment: str = "unknown",
    source: str | None = None,
    stream: Any = None,
) -> None:
    """Install the JSON formatter on the root logger, replacing any handlers.

    ``source`` defaults to ``PLATFORM_RUN_SOURCE`` (set to ``CRON`` by
    ``scripts/cron-run.sh``) so a scheduled run is distinguishable from an
    interactive one without the caller passing it around.

    Logs go to **stderr** so that a command's stdout stays a clean, pipeable
    result — the operator report is data, the log is diagnostics.
    """
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(
        JsonFormatter(
            environment=environment,
            source=source or os.environ.get("PLATFORM_RUN_SOURCE", "INTERACTIVE"),
        )
    )
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
