"""Immutable raw-payload store.

Every raw API response (Binance REST and WebSocket payloads, archive downloads)
is retained verbatim so the system can be replayed and audited, and so parser
bugs can be fixed retroactively without having lost the source data. Objects are
content-addressable by SHA-256 and are never overwritten with different content.

This is what makes ADR-0003 safe: every Binance schema in this repo starts as a
documented guess, and retaining raw bytes means correcting a guess is a replay,
not a data loss. Keys should carry the venue environment (ADR-0010) so a testnet
payload can never be mistaken for a production one.

Implementations:
  * ``LocalRawStore``  — filesystem; the default, and what local development uses.
  * ``InMemoryRawStore`` — for tests and ephemeral runs.
  * ``S3RawStore`` — object storage, for a deployed runtime.

Use :func:`create_raw_store` rather than naming an implementation directly, so the
backend is an environment decision instead of a code one.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from config.settings import Settings


class RawStoreError(Exception):
    """Base class for raw-store errors."""


class RawObjectConflict(RawStoreError):
    """Raised when a key already holds *different* bytes than those being written."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"Refusing to overwrite {key!r} with different content")


class RawObjectNotFound(RawStoreError):
    """Raised when a requested key does not exist."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"No raw object at {key!r}")


@dataclass(frozen=True, slots=True)
class RawObjectRef:
    """A pointer to a stored raw object."""

    key: str
    sha256: str
    size: int


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@runtime_checkable
class RawStore(Protocol):
    """Content-preserving object store for raw payloads."""

    def put(self, key: str, data: bytes) -> RawObjectRef:
        """Store ``data`` at ``key``. Writing identical bytes to an existing key is
        idempotent; writing *different* bytes raises :class:`RawObjectConflict`."""
        ...

    def get(self, key: str) -> bytes:
        """Return the bytes at ``key`` or raise :class:`RawObjectNotFound`."""
        ...

    def exists(self, key: str) -> bool: ...


class InMemoryRawStore:
    """A :class:`RawStore` kept in a dict. For tests and ephemeral use."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> RawObjectRef:
        existing = self._objects.get(key)
        if existing is not None and existing != data:
            raise RawObjectConflict(key)
        self._objects[key] = data
        return RawObjectRef(key=key, sha256=_digest(data), size=len(data))

    def get(self, key: str) -> bytes:
        try:
            return self._objects[key]
        except KeyError:
            raise RawObjectNotFound(key) from None

    def exists(self, key: str) -> bool:
        return key in self._objects


class LocalRawStore:
    """A :class:`RawStore` backed by the local filesystem, rooted at ``root``.

    Keys are treated as relative POSIX paths under ``root``. Absolute keys or keys
    that escape the root (via ``..``) are rejected.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root).resolve()

    def _resolve(self, key: str) -> Path:
        if key.startswith("/"):
            raise RawStoreError(f"Key must be relative, got {key!r}")
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root):
            raise RawStoreError(f"Key escapes store root: {key!r}")
        return path

    def put(self, key: str, data: bytes) -> RawObjectRef:
        path = self._resolve(key)
        if path.exists() and path.read_bytes() != data:
            raise RawObjectConflict(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return RawObjectRef(key=key, sha256=_digest(data), size=len(data))

    def get(self, key: str) -> bytes:
        path = self._resolve(key)
        if not path.exists():
            raise RawObjectNotFound(key)
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._resolve(key).exists()


class S3RawStore:
    """A :class:`RawStore` backed by an S3 bucket.

    Keys are stored under an optional ``prefix``. The conflict guarantee matches
    the other implementations: rewriting a key with identical bytes is idempotent,
    and rewriting it with *different* bytes raises rather than destroying the
    original payload. That check costs one HEAD/GET per existing key, which is the
    right trade for an audit store that must never lose a source document.

    ``boto3`` is imported lazily and is an optional dependency (``uv sync --extra
    aws``), so a local checkout without it keeps working.
    """

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "",
        client: Any = None,
        region_name: str | None = None,
    ) -> None:
        if not bucket:
            raise RawStoreError("S3RawStore requires a bucket name")
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        if client is not None:
            self._client = client
        else:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - environment-dependent
                raise RawStoreError(
                    "S3RawStore needs boto3; install with `uv sync --extra aws`"
                ) from exc
            self._client = boto3.client("s3", region_name=region_name)

    def _resolve(self, key: str) -> str:
        if key.startswith("/"):
            raise RawStoreError(f"Key must be relative, got {key!r}")
        if any(part == ".." for part in key.split("/")):
            raise RawStoreError(f"Key escapes store prefix: {key!r}")
        return f"{self._prefix}/{key}" if self._prefix else key

    def put(self, key: str, data: bytes) -> RawObjectRef:
        object_key = self._resolve(key)
        existing = self._read(object_key)
        if existing is not None:
            if existing != data:
                raise RawObjectConflict(key)
            return RawObjectRef(key=key, sha256=_digest(data), size=len(data))
        self._client.put_object(Bucket=self._bucket, Key=object_key, Body=data)
        return RawObjectRef(key=key, sha256=_digest(data), size=len(data))

    def _read(self, object_key: str) -> bytes | None:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=object_key)
        except Exception as exc:
            if _is_missing_key(exc):
                return None
            raise
        body = response["Body"]
        data = body.read()
        return bytes(data)

    def get(self, key: str) -> bytes:
        data = self._read(self._resolve(key))
        if data is None:
            raise RawObjectNotFound(key)
        return data

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=self._resolve(key))
        except Exception as exc:
            if _is_missing_key(exc):
                return False
            raise
        return True


def _is_missing_key(exc: Exception) -> bool:
    """True when a botocore error means "no such object" rather than a real fault.

    Matched structurally instead of by exception class so the store needs no
    botocore import, and so a genuine permissions or network error still raises
    instead of being silently reported as a missing object.
    """
    code = getattr(exc, "response", {}).get("Error", {}).get("Code")
    return code in {"NoSuchKey", "404", "NotFound"}


def create_raw_store(settings: Settings) -> RawStore:
    """Build the raw store the environment asks for.

    Local development keeps the filesystem store with no configuration; a deployed
    runtime sets ``RAW_STORE_BACKEND=s3`` and a bucket.
    """
    from config.settings import RawStoreBackend

    if settings.raw_store_backend is RawStoreBackend.S3:
        if not settings.raw_store_s3_bucket:
            raise RawStoreError("RAW_STORE_BACKEND=s3 requires RAW_STORE_S3_BUCKET to be set")
        return S3RawStore(
            settings.raw_store_s3_bucket,
            prefix=settings.raw_store_s3_prefix,
            region_name=settings.aws_region,
        )
    return LocalRawStore(settings.raw_store_local_dir)
