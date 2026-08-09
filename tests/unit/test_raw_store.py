"""Raw-store tests.

The guarantee that matters: a key never silently changes meaning. ADR-0003 makes
every Binance schema in this repo a guess until it is validated against recorded
bytes, and that is only safe if the recorded bytes cannot be overwritten.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config.settings import RawStoreBackend, Settings
from storage.raw_store import (
    InMemoryRawStore,
    LocalRawStore,
    RawObjectConflict,
    RawObjectNotFound,
    RawStore,
    RawStoreError,
    create_raw_store,
)

PAYLOAD = b'{"symbol":"BTCUSDT","fundingRate":"0.00010000"}'
KEY = "binance/testnet/fapi/fundingRate/2026-08-09T16:00:00Z.json"


@pytest.fixture(params=["memory", "local"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> RawStore:
    if request.param == "memory":
        return InMemoryRawStore()
    return LocalRawStore(tmp_path)


def test_put_returns_a_content_addressed_reference(store: RawStore) -> None:
    ref = store.put(KEY, PAYLOAD)
    assert ref.key == KEY
    assert ref.size == len(PAYLOAD)
    assert len(ref.sha256) == 64


def test_stored_bytes_round_trip_exactly(store: RawStore) -> None:
    store.put(KEY, PAYLOAD)
    assert store.get(KEY) == PAYLOAD
    assert store.exists(KEY)


def test_rewriting_identical_bytes_is_idempotent(store: RawStore) -> None:
    first = store.put(KEY, PAYLOAD)
    second = store.put(KEY, PAYLOAD)
    assert first.sha256 == second.sha256


def test_rewriting_different_bytes_is_refused(store: RawStore) -> None:
    """The audit guarantee: a recorded payload is never destroyed by a later run."""
    store.put(KEY, PAYLOAD)
    with pytest.raises(RawObjectConflict):
        store.put(KEY, b'{"symbol":"BTCUSDT","fundingRate":"0.00020000"}')
    assert store.get(KEY) == PAYLOAD


def test_a_missing_key_raises_rather_than_returning_empty(store: RawStore) -> None:
    with pytest.raises(RawObjectNotFound):
        store.get("binance/testnet/absent.json")
    assert not store.exists("binance/testnet/absent.json")


def test_different_environments_do_not_collide(store: RawStore) -> None:
    """Environment-scoped keys are what keep testnet out of production (ADR-0010)."""
    testnet = b'{"markPrice":"1.00"}'
    production = b'{"markPrice":"64000.00"}'
    store.put("binance/testnet/premiumIndex/BTCUSDT.json", testnet)
    store.put("binance/production/premiumIndex/BTCUSDT.json", production)
    assert store.get("binance/testnet/premiumIndex/BTCUSDT.json") == testnet
    assert store.get("binance/production/premiumIndex/BTCUSDT.json") == production


def test_an_absolute_key_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RawStoreError, match="must be relative"):
        LocalRawStore(tmp_path).put("/etc/passwd", b"x")


def test_a_key_escaping_the_root_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RawStoreError, match="escapes store root"):
        LocalRawStore(tmp_path).put("../outside.json", b"x")


def test_nested_keys_create_their_directories(tmp_path: Path) -> None:
    LocalRawStore(tmp_path).put("a/b/c/d.json", PAYLOAD)
    assert (tmp_path / "a" / "b" / "c" / "d.json").read_bytes() == PAYLOAD


def test_the_factory_defaults_to_the_local_store(tmp_path: Path) -> None:
    settings = Settings(raw_store_backend=RawStoreBackend.LOCAL, raw_store_local_dir=str(tmp_path))
    assert isinstance(create_raw_store(settings), LocalRawStore)


def test_the_factory_refuses_s3_without_a_bucket() -> None:
    settings = Settings(raw_store_backend=RawStoreBackend.S3, raw_store_s3_bucket="")
    with pytest.raises(RawStoreError, match="RAW_STORE_S3_BUCKET"):
        create_raw_store(settings)
