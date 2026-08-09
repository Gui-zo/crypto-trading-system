"""Secret access behind a narrow interface.

Ported from the sibling ``automated-trading-system`` repo, plus one addition:
:class:`FileSecretProvider`. Binance API secrets are long HMAC strings that are
easy to leak through shell history, process listings, and ``.env`` files copied
between machines, so ``BINANCE_API_SECRET_PATH`` names a *file* and the value is
read from it. The env-var implementation remains for everything else.

All secret reads go through :class:`SecretProvider`; later phases can add an AWS
Secrets Manager implementation without touching any call site. Secrets are never
logged and never passed to an LLM.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, runtime_checkable


class SecretError(Exception):
    """Base class for secret-access errors."""


class MissingSecretError(SecretError):
    """Raised when a required secret is absent."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Required secret {name!r} is not set")


class InsecureSecretError(SecretError):
    """Raised when a secret file is readable by users other than its owner."""

    def __init__(self, path: Path, mode: int) -> None:
        self.path = path
        self.mode = mode
        super().__init__(
            f"Secret file {str(path)!r} has mode {mode:o}; it must not be group- or "
            f"world-readable. Run: chmod 600 {path}"
        )


@runtime_checkable
class SecretProvider(Protocol):
    """Reads named secrets. Implementations must not log secret values."""

    def get_secret(self, name: str) -> str | None:
        """Return the secret value, or ``None`` if it is not set."""
        ...

    def require_secret(self, name: str) -> str:
        """Return the secret value, or raise :class:`MissingSecretError`."""
        ...


class EnvSecretProvider:
    """A :class:`SecretProvider` backed by environment variables.

    Suitable for local development. A blank string is treated as unset so that an
    empty ``.env`` placeholder does not masquerade as a real value.
    """

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self._environ = environ if environ is not None else dict(os.environ)

    def get_secret(self, name: str) -> str | None:
        value = self._environ.get(name)
        if value is None or value == "":
            return None
        return value

    def require_secret(self, name: str) -> str:
        value = self.get_secret(name)
        if value is None:
            raise MissingSecretError(name)
        return value


class FileSecretProvider:
    """A :class:`SecretProvider` that reads each secret from a file on disk.

    ``name`` is an environment variable holding a *path*, not a value — so the
    secret itself never appears in the environment, in ``ps`` output, or in a
    ``.env`` file that gets copied somewhere careless.

    Permissions are checked, not assumed: a secret file that any local user can
    read is treated as missing rather than usable, because silently accepting it
    is how a 0644 key survives to production.
    """

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self._environ = environ if environ is not None else dict(os.environ)

    def get_secret(self, name: str) -> str | None:
        raw_path = self._environ.get(name)
        if not raw_path:
            return None
        path = Path(raw_path).expanduser()
        if not path.is_file():
            return None
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise InsecureSecretError(path, mode)
        value = path.read_text(encoding="utf-8").strip()
        return value or None

    def require_secret(self, name: str) -> str:
        value = self.get_secret(name)
        if value is None:
            raise MissingSecretError(name)
        return value
