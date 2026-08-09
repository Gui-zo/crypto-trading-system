"""Secret-provider tests, including the file-permission refusal."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from config.secrets import (
    EnvSecretProvider,
    FileSecretProvider,
    InsecureSecretError,
    MissingSecretError,
    SecretProvider,
)


def test_both_providers_satisfy_the_protocol() -> None:
    assert isinstance(EnvSecretProvider({}), SecretProvider)
    assert isinstance(FileSecretProvider({}), SecretProvider)


def test_env_provider_reads_a_value() -> None:
    provider = EnvSecretProvider({"BINANCE_API_KEY_ID": "abc123"})
    assert provider.get_secret("BINANCE_API_KEY_ID") == "abc123"
    assert provider.require_secret("BINANCE_API_KEY_ID") == "abc123"


def test_an_empty_placeholder_is_treated_as_unset() -> None:
    """A blank `.env` line must not masquerade as a configured credential."""
    provider = EnvSecretProvider({"BINANCE_API_KEY_ID": ""})
    assert provider.get_secret("BINANCE_API_KEY_ID") is None
    with pytest.raises(MissingSecretError):
        provider.require_secret("BINANCE_API_KEY_ID")


def test_a_missing_secret_names_itself_without_leaking_a_value() -> None:
    with pytest.raises(MissingSecretError) as exc:
        EnvSecretProvider({}).require_secret("BINANCE_API_SECRET")
    assert "BINANCE_API_SECRET" in str(exc.value)


def test_file_provider_reads_and_strips_the_file(tmp_path: Path) -> None:
    secret_file = tmp_path / "binance.key"
    secret_file.write_text("s3cr3t\n")
    secret_file.chmod(0o600)
    provider = FileSecretProvider({"BINANCE_API_SECRET_PATH": str(secret_file)})
    assert provider.get_secret("BINANCE_API_SECRET_PATH") == "s3cr3t"


def test_a_group_or_world_readable_secret_file_is_refused(tmp_path: Path) -> None:
    """A 0644 key must fail loudly here, not survive quietly to production."""
    secret_file = tmp_path / "binance.key"
    secret_file.write_text("s3cr3t")
    secret_file.chmod(0o644)
    provider = FileSecretProvider({"BINANCE_API_SECRET_PATH": str(secret_file)})
    with pytest.raises(InsecureSecretError, match="chmod 600"):
        provider.get_secret("BINANCE_API_SECRET_PATH")


def test_a_missing_file_reads_as_unset(tmp_path: Path) -> None:
    provider = FileSecretProvider({"BINANCE_API_SECRET_PATH": str(tmp_path / "absent.key")})
    assert provider.get_secret("BINANCE_API_SECRET_PATH") is None


def test_an_empty_file_reads_as_unset(tmp_path: Path) -> None:
    secret_file = tmp_path / "binance.key"
    secret_file.write_text("   \n")
    secret_file.chmod(0o600)
    provider = FileSecretProvider({"BINANCE_API_SECRET_PATH": str(secret_file)})
    assert provider.get_secret("BINANCE_API_SECRET_PATH") is None


def test_an_unset_path_variable_reads_as_unset() -> None:
    assert FileSecretProvider({}).get_secret("BINANCE_API_SECRET_PATH") is None


def test_the_home_shorthand_is_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    secret_file = tmp_path / "binance.key"
    secret_file.write_text("value")
    secret_file.chmod(0o600)
    provider = FileSecretProvider({"BINANCE_API_SECRET_PATH": "~/binance.key"})
    assert provider.get_secret("BINANCE_API_SECRET_PATH") == "value"


def test_the_env_provider_snapshots_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reading os.environ once keeps a later mutation from changing a decision."""
    monkeypatch.setenv("SOME_SECRET", "first")
    provider = EnvSecretProvider()
    os.environ["SOME_SECRET"] = "second"
    assert provider.get_secret("SOME_SECRET") == "first"
