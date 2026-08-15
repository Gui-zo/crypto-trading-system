"""Capture model provenance from the working tree: source digest and commit.

The pure contract lives in :mod:`domain.model_provenance`. This module is the
part that has to touch the filesystem and Git, and the part that **fails closed**
when the working tree cannot support an honest claim about which code ran.

The rule that matters: if any file covered by the source digest is modified,
staged, or untracked, registration is refused. A digest computed over
uncommitted bytes names code that exists on exactly one machine, so a prediction
carrying it could never be reproduced.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from domain.model_provenance import DataSnapshot, ModelProvenance, ProvenanceError

#: Files whose contents define the funding-persistence baseline's behaviour.
#: Changing any of them changes the model, so all of them are inside the digest.
#: Paths are repo-relative and kept sorted for a reproducible hash.
MODEL_SOURCE_FILES: tuple[str, ...] = (
    "packages/domain/calibration.py",
    "packages/domain/funding_model.py",
    "packages/domain/model_provenance.py",
    "packages/modeling/provenance.py",
)


class WorkingTreeDirty(ProvenanceError):
    """A model-relevant file is uncommitted, so its digest names unreproducible code."""


def _git(repo_root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ("git", *args),
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:  # pragma: no cover - git absent is environmental
        raise ProvenanceError("git is required to capture model provenance") from exc
    except subprocess.CalledProcessError as exc:
        raise ProvenanceError(f"git {' '.join(args)} failed: {exc.stderr.strip()}") from exc
    return completed.stdout.strip()


def source_digest(repo_root: Path, files: Sequence[str] = MODEL_SOURCE_FILES) -> str:
    """SHA-256 over the named files, each length-prefixed by its path.

    The path is folded into the digest so that moving a file changes the model
    identity — the same bytes under a different name are a different program.
    """
    ordered = tuple(sorted(files))
    if ordered != tuple(files):
        raise ProvenanceError("source files must be passed in sorted order")
    digest = hashlib.sha256()
    for relative in ordered:
        path = repo_root / relative
        if not path.is_file():
            raise ProvenanceError(f"model source file is missing: {relative}")
        payload = path.read_bytes()
        digest.update(f"{relative}:{len(payload)}\n".encode("ascii"))
        digest.update(payload)
    return digest.hexdigest()


def require_clean_source(repo_root: Path, files: Sequence[str] = MODEL_SOURCE_FILES) -> None:
    """Refuse to proceed when any model-relevant file is uncommitted."""
    status = _git(repo_root, "status", "--porcelain", "--", *files)
    if status:
        dirty = sorted(line[3:].strip() for line in status.splitlines() if line.strip())
        raise WorkingTreeDirty(
            "model-relevant files are uncommitted, so their digest cannot identify "
            f"reproducible code: {', '.join(dirty)}"
        )


def head_commit(repo_root: Path) -> str:
    return _git(repo_root, "rev-parse", "HEAD")


def snapshot_observations(
    rows: Iterable[tuple[str, datetime, Decimal]],
    *,
    range_start: datetime,
    range_end: datetime,
) -> DataSnapshot:
    """Content-address the exact settlements a model consumed.

    Rows are hashed in the order given, so a different selection *or a different
    ordering* is a different snapshot. Values go in as their exact decimal text
    — never a float, which would make the digest depend on repr behaviour.
    """
    digest = hashlib.sha256()
    symbols: set[str] = set()
    count = 0
    for symbol, funding_time, funding_rate in rows:
        symbols.add(symbol)
        count += 1
        digest.update(f"{symbol}|{funding_time.isoformat()}|{funding_rate}\n".encode("ascii"))
    if count == 0:
        raise ProvenanceError("a data snapshot cannot be built from zero observations")
    return DataSnapshot(
        content_sha256=digest.hexdigest(),
        row_count=count,
        symbol_count=len(symbols),
        range_start=range_start,
        range_end=range_end,
    )


def capture(
    repo_root: Path,
    *,
    semantic_version: str,
    data: DataSnapshot,
    parameters: dict[str, str],
    files: Sequence[str] = MODEL_SOURCE_FILES,
    allow_dirty: bool = False,
) -> ModelProvenance:
    """Build complete provenance for a code-native model version.

    ``allow_dirty`` exists for tests only. Nothing that writes a model version to
    the database may set it.
    """
    if not allow_dirty:
        require_clean_source(repo_root, files)
    return ModelProvenance(
        semantic_version=semantic_version,
        source_sha256=source_digest(repo_root, files),
        source_files=tuple(sorted(files)),
        code_commit=head_commit(repo_root),
        data=data,
        parameters=parameters,
    )
