"""Phase-4 model identity: complete provenance, content addressing, fail-closed capture."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from domain.model_provenance import (
    PROVENANCE_SCHEMA,
    DataSnapshot,
    ModelProvenance,
    ProvenanceError,
)
from modeling.provenance import (
    WorkingTreeDirty,
    capture,
    require_clean_source,
    snapshot_observations,
    source_digest,
)

START = datetime(2025, 1, 1, tzinfo=UTC)
END = datetime(2026, 1, 1, tzinfo=UTC)
DIGEST = "a" * 64
COMMIT = "b" * 40


def snapshot(**overrides: object) -> DataSnapshot:
    fields: dict[str, object] = {
        "content_sha256": DIGEST,
        "row_count": 10,
        "symbol_count": 2,
        "range_start": START,
        "range_end": END,
    }
    fields.update(overrides)
    return DataSnapshot(**fields)  # type: ignore[arg-type]


def provenance(**overrides: object) -> ModelProvenance:
    fields: dict[str, object] = {
        "semantic_version": "funding-persistence-v1",
        "source_sha256": DIGEST,
        "source_files": ("a.py", "b.py"),
        "code_commit": COMMIT,
        "data": snapshot(),
        "parameters": {"horizon": "1"},
    }
    fields.update(overrides)
    return ModelProvenance(**fields)  # type: ignore[arg-type]


# --- the contract ------------------------------------------------------------


def test_an_untrained_baseline_says_so_rather_than_leaving_nulls_ambiguous() -> None:
    """ADR-0015's lesson: null must never mean 'somebody forgot'."""
    model = provenance()

    assert model.untrained
    assert model.training_start is None
    assert model.as_dict()["untrained"] is True


def test_training_bounds_must_be_set_together() -> None:
    with pytest.raises(ProvenanceError, match="together"):
        provenance(training_start=START)


def test_a_short_commit_is_refused() -> None:
    with pytest.raises(ProvenanceError, match="40-character"):
        provenance(code_commit="abc123")


def test_unsorted_source_files_are_refused_so_the_digest_reproduces() -> None:
    with pytest.raises(ProvenanceError, match="sorted"):
        provenance(source_files=("b.py", "a.py"))


def test_a_non_digest_source_hash_is_refused() -> None:
    with pytest.raises(ProvenanceError, match="SHA-256"):
        provenance(source_sha256="not-a-digest")


def test_an_empty_data_snapshot_is_refused() -> None:
    with pytest.raises(ProvenanceError, match="at least one row"):
        snapshot(row_count=0)


def test_schema_is_pinned() -> None:
    assert provenance().schema == PROVENANCE_SCHEMA
    with pytest.raises(ProvenanceError, match="unsupported provenance schema"):
        provenance(schema="model-provenance-v2")


# --- content addressing ------------------------------------------------------


def test_identity_covers_code_data_and_parameters_together() -> None:
    """Reusing a semantic version with anything else changed is a different model."""
    base = provenance().content_sha256

    assert provenance(source_sha256="c" * 64).content_sha256 != base
    assert provenance(data=snapshot(content_sha256="d" * 64)).content_sha256 != base
    assert provenance(parameters={"horizon": "2"}).content_sha256 != base
    assert provenance(semantic_version="funding-persistence-v2").content_sha256 != base


def test_identity_is_stable_across_parameter_ordering() -> None:
    one = provenance(parameters={"a": "1", "b": "2"})
    other = provenance(parameters={"b": "2", "a": "1"})

    assert one.content_sha256 == other.content_sha256


def test_the_artifact_uri_is_the_source_digest() -> None:
    assert provenance().artifact_uri == f"source-sha256://{DIGEST}"


# --- data snapshots ----------------------------------------------------------


def test_a_reordered_input_set_is_a_different_snapshot() -> None:
    rows = [
        ("BTCUSDT", START, Decimal("0.0001")),
        ("ETHUSDT", START, Decimal("0.0002")),
    ]
    forward = snapshot_observations(rows, range_start=START, range_end=END)
    reversed_ = snapshot_observations(list(reversed(rows)), range_start=START, range_end=END)

    assert forward.content_sha256 != reversed_.content_sha256


def test_snapshot_hashes_exact_decimal_text_not_a_float() -> None:
    """Trailing zeros are a different exact value and must not collide."""
    one = snapshot_observations(
        [("BTCUSDT", START, Decimal("0.00010"))], range_start=START, range_end=END
    )
    other = snapshot_observations(
        [("BTCUSDT", START, Decimal("0.0001"))], range_start=START, range_end=END
    )

    assert one.content_sha256 != other.content_sha256


def test_an_empty_row_set_cannot_become_a_snapshot() -> None:
    with pytest.raises(ProvenanceError, match="zero observations"):
        snapshot_observations([], range_start=START, range_end=END)


# --- capture from the working tree -------------------------------------------


def _git(root: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "model.py").write_text("value = 1\n")
    _git(tmp_path, "add", "model.py")
    _git(tmp_path, "commit", "-qm", "initial")
    return tmp_path


def test_moving_a_file_changes_the_model_identity(repo: Path) -> None:
    """The path is folded into the digest: the same bytes under a different name
    are a different program."""
    (repo / "renamed.py").write_text((repo / "model.py").read_text())

    assert source_digest(repo, ["model.py"]) != source_digest(repo, ["renamed.py"])


def test_a_missing_source_file_fails_closed(repo: Path) -> None:
    with pytest.raises(ProvenanceError, match="missing"):
        source_digest(repo, ["absent.py"])


def test_an_uncommitted_model_file_refuses_registration(repo: Path) -> None:
    """A digest over uncommitted bytes names code that exists on one machine."""
    (repo / "model.py").write_text("value = 2\n")

    with pytest.raises(WorkingTreeDirty, match="uncommitted"):
        require_clean_source(repo, ["model.py"])


def test_an_untracked_model_file_also_refuses(repo: Path) -> None:
    (repo / "extra.py").write_text("value = 3\n")

    with pytest.raises(WorkingTreeDirty):
        require_clean_source(repo, ["model.py", "extra.py"])


def test_capture_on_a_clean_tree_produces_complete_provenance(repo: Path) -> None:
    captured = capture(
        repo,
        semantic_version="funding-persistence-v1",
        data=snapshot(),
        parameters={"horizon": "1"},
        files=["model.py"],
    )

    assert captured.untrained
    assert captured.source_files == ("model.py",)
    assert len(captured.code_commit) == 40
    assert captured.content_sha256 == captured.content_sha256


def test_capture_refuses_a_dirty_tree_by_default(repo: Path) -> None:
    (repo / "model.py").write_text("value = 99\n")

    with pytest.raises(WorkingTreeDirty):
        capture(
            repo,
            semantic_version="funding-persistence-v1",
            data=snapshot(),
            parameters={},
            files=["model.py"],
        )
