"""Model identity: what code, over what data, produced a prediction.

Ported in spirit from the sibling repo's ADR-0015. A semantic label like
``funding-persistence-v1`` cannot establish which code produced a number or
whether a later invocation is the same model, so identity here is
**content-addressed**: the digest of the model-relevant source, the digest of the
exact input rows, and the commit those were registered from, all required
together.

The baseline this was written for has no fit step — it is an expanding empirical
frequency, so its "artifact" *is* its source code and its parameters are the
history it has seen. ``training_start``/``training_end`` are therefore genuinely
null rather than forgotten, and :attr:`ModelProvenance.untrained` says so
explicitly. Null must never ambiguously mean "nobody filled this in".
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from domain.errors import DomainError

#: The provenance contract this module implements. Bump it, do not bend it.
PROVENANCE_SCHEMA = "model-provenance-v1"

#: Prefix marking a code-native artifact — one with no trained weights, whose
#: identity is the digest of the source that implements its semantics.
SOURCE_ARTIFACT_SCHEME = "source-sha256://"


class ProvenanceError(DomainError):
    """Incomplete or self-inconsistent model provenance. Always fails closed."""


def _require_digest(value: str, field: str) -> str:
    digest = value.lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ProvenanceError(f"{field} must be a hex SHA-256 digest")
    return digest


@dataclass(frozen=True, slots=True)
class DataSnapshot:
    """Identity of the exact rows a model consumed.

    ``content_sha256`` covers every observation in order, so a changed,
    reordered, or extended input set produces a different snapshot and therefore
    a different model version. The counts and bounds are carried alongside
    because a digest alone tells a reader nothing about what it covers.
    """

    content_sha256: str
    row_count: int
    symbol_count: int
    range_start: datetime
    range_end: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "content_sha256", _require_digest(self.content_sha256, "data content_sha256")
        )
        if self.row_count <= 0 or self.symbol_count <= 0:
            raise ProvenanceError("a data snapshot must cover at least one row and one symbol")
        if self.range_start.tzinfo is None or self.range_end.tzinfo is None:
            raise ProvenanceError("data snapshot bounds must be timezone-aware")
        if self.range_end < self.range_start:
            raise ProvenanceError("data snapshot range_end precedes range_start")

    @property
    def snapshot_id(self) -> str:
        return f"rows-sha256://{self.content_sha256}"


@dataclass(frozen=True, slots=True)
class ModelProvenance:
    """Complete, reconstructable identity for one model version.

    Every field is required together. That is the ADR-0015 lesson: a schema that
    permits a null artifact URI beside a populated commit cannot distinguish
    "this model has no trained artifact" from "somebody forgot".
    """

    semantic_version: str
    source_sha256: str
    source_files: tuple[str, ...]
    code_commit: str
    data: DataSnapshot
    parameters: dict[str, str]
    training_start: datetime | None = None
    training_end: datetime | None = None
    schema: str = PROVENANCE_SCHEMA

    def __post_init__(self) -> None:
        if not self.semantic_version:
            raise ProvenanceError("semantic_version is required")
        object.__setattr__(self, "source_sha256", _require_digest(self.source_sha256, "source"))
        if not self.source_files:
            raise ProvenanceError("source_files must name the files the digest covers")
        if tuple(sorted(self.source_files)) != self.source_files:
            raise ProvenanceError("source_files must be sorted so the digest is reproducible")
        if len(self.code_commit) != 40 or any(
            character not in "0123456789abcdef" for character in self.code_commit.lower()
        ):
            raise ProvenanceError("code_commit must be a full 40-character git commit")
        if (self.training_start is None) != (self.training_end is None):
            raise ProvenanceError("training_start and training_end must be set together")
        if self.schema != PROVENANCE_SCHEMA:
            raise ProvenanceError(f"unsupported provenance schema {self.schema!r}")

    @property
    def untrained(self) -> bool:
        """True when there is no fit step, said explicitly rather than by nulls."""
        return self.training_start is None

    @property
    def artifact_uri(self) -> str:
        return f"{SOURCE_ARTIFACT_SCHEME}{self.source_sha256}"

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "semantic_version": self.semantic_version,
            "artifact_uri": self.artifact_uri,
            "source_files": list(self.source_files),
            "code_commit": self.code_commit,
            "data_snapshot_id": self.data.snapshot_id,
            "data_row_count": self.data.row_count,
            "data_symbol_count": self.data.symbol_count,
            "data_range_start": self.data.range_start.isoformat(),
            "data_range_end": self.data.range_end.isoformat(),
            "parameters": dict(sorted(self.parameters.items())),
            "training_start": None
            if self.training_start is None
            else self.training_start.isoformat(),
            "training_end": None if self.training_end is None else self.training_end.isoformat(),
            "untrained": self.untrained,
        }

    @property
    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")

    @property
    def content_sha256(self) -> str:
        """The model version's identity — code, data, and parameters together.

        Two registrations agree only if all three agree. Reusing a semantic
        version with different source, data, or parameters is a different model
        and must be recorded as one.
        """
        return hashlib.sha256(self.canonical_bytes).hexdigest()
