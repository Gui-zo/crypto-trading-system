"""Signer tests.

The signing format itself is **unverified** against the live venue (ADR-0003), so
these tests pin the properties we can assert without it: that the signed bytes are
the sent bytes, that the secret never escapes, and that the HMAC matches a value
computed independently.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime

import pytest

from venue_binance.auth import BinanceSigner

NOW = datetime(2026, 8, 9, 16, 0, tzinfo=UTC)
SECRET = "test-secret-not-a-real-key"


def signer(**kwargs: object) -> BinanceSigner:
    return BinanceSigner("test-api-key", SECRET, **kwargs)  # type: ignore[arg-type]


def test_signature_matches_an_independently_computed_hmac() -> None:
    query = "symbol=BTCUSDT&timestamp=1"
    expected = hmac.new(SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    assert signer().sign(query) == expected


def test_signed_query_ends_with_its_own_signature_over_the_preceding_bytes() -> None:
    """The property that matters: what was signed is what will be sent.

    Re-encoding params after signing is the classic way to produce a request that
    looks right and is rejected, so the signer returns the final string.
    """
    query = signer().signed_query([("symbol", "BTCUSDT")], now=NOW)
    body, _, signature = query.rpartition("&signature=")
    assert signer().verify(body, signature)


def test_parameter_order_is_preserved_exactly_as_given() -> None:
    query = signer().signed_query([("b", "2"), ("a", "1")], now=NOW)
    assert query.startswith("b=2&a=1&timestamp=")


def test_timestamp_is_epoch_milliseconds() -> None:
    query = signer().signed_query(now=NOW)
    assert f"timestamp={int(NOW.timestamp() * 1000)}" in query


def test_recv_window_is_included() -> None:
    assert "recvWindow=5000" in signer().signed_query(now=NOW)
    assert "recvWindow=2000" in signer(recv_window_ms=2000).signed_query(now=NOW)


def test_values_are_url_encoded() -> None:
    query = signer().signed_query([("note", "a b&c")], now=NOW)
    assert "a+b%26c" in query


def test_a_different_secret_yields_a_different_signature() -> None:
    other = BinanceSigner("test-api-key", "different-secret")
    assert signer().sign("x=1") != other.sign("x=1")


def test_verify_rejects_a_tampered_query() -> None:
    query = signer().signed_query([("symbol", "BTCUSDT")], now=NOW)
    body, _, signature = query.rpartition("&signature=")
    assert not signer().verify(body.replace("BTCUSDT", "ETHUSDT"), signature)


def test_the_api_key_header_is_set_and_the_secret_is_not() -> None:
    headers = signer().headers()
    assert headers == {"X-MBX-APIKEY": "test-api-key"}
    assert SECRET not in str(headers)


def test_repr_does_not_leak_the_secret_or_the_full_key() -> None:
    rendered = repr(signer())
    assert SECRET not in rendered
    assert "test-api-key" not in rendered


def test_the_secret_is_not_reachable_as_an_attribute() -> None:
    """`__slots__` plus no property: there is no supported way to read it back."""
    instance = signer()
    assert not hasattr(instance, "secret")
    with pytest.raises(AttributeError):
        instance.__dict__  # noqa: B018 - slots class has no __dict__


@pytest.mark.parametrize(
    ("api_key", "secret", "match"),
    [("", SECRET, "API key"), ("  ", SECRET, "API key"), ("k", "", "secret")],
)
def test_blank_credentials_are_refused(api_key: str, secret: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        BinanceSigner(api_key, secret)


@pytest.mark.parametrize("window", [0, -1, 60_001])
def test_an_out_of_range_recv_window_is_refused(window: int) -> None:
    with pytest.raises(ValueError, match="recvWindow"):
        signer(recv_window_ms=window)
