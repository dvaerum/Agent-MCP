"""Unit tests for the signed forwarding-header (Wave 1).

The module under test (``agent_mcp.app.forwarding_header``) is the
sign / verify primitive that lets the router (Wave 2) carry an
authenticated operator identity to the per-project backend without
sharing a god-key bearer. Wave 1 ships only the verify side wired
into ``AuthHeaderMiddleware``; this file covers the primitive in
isolation.
"""

from __future__ import annotations

import os

import pytest

from agent_mcp.app import forwarding_header as fh


@pytest.fixture
def key() -> bytes:
    """A fresh 32-byte HMAC key per test."""
    return os.urandom(32)


def test_sign_then_verify_roundtrip_returns_operator_id(key: bytes) -> None:
    """A header signed with key K verifies under key K and yields the
    same operator_id the signer passed in."""
    header = fh.sign("dennis", key, ttl_sec=30)
    assert fh.verify(header, key) == "dennis"


def test_verify_with_wrong_key_returns_none(key: bytes) -> None:
    """The single most important property: a header forged by an
    attacker who doesn't hold the per-project key cannot satisfy the
    backend verifier."""
    other_key = os.urandom(32)
    header = fh.sign("dennis", key, ttl_sec=30)
    assert fh.verify(header, other_key) is None


def test_verify_rejects_expired_header(key: bytes) -> None:
    """A header whose claimed expiry is in the past is rejected even
    when signed by the legitimate key. Defends against capture-and-
    replay outside the freshness window."""
    # Sign at t=1000 with ttl=10 → expiry=1010. Verify at t=2000 (long
    # past expiry) → reject.
    header = fh.sign("dennis", key, ttl_sec=10, _now=1000)
    assert fh.verify(header, key, _now=2000) is None


def test_verify_accepts_unexpired_header(key: bytes) -> None:
    """Negative-space of the expiry check: at verify time t<expiry the
    header is accepted."""
    header = fh.sign("dennis", key, ttl_sec=30, _now=1000)
    assert fh.verify(header, key, _now=1015) == "dennis"


def test_verify_enforces_replay_window(key: bytes) -> None:
    """A header whose ``expiry`` is more than ``replay_window_sec`` in
    the future at verify time is rejected even if the HMAC is valid.

    This catches two abuse cases:
      * A router with a bug that sets a year-long TTL.
      * A captured header with an extended expiry (which would
        require breaking the HMAC, so really this is defence in
        depth — but it pins the verifier's tolerance).
    """
    # Sign with TTL = 10 minutes (well past the 30-second replay window).
    header = fh.sign("dennis", key, ttl_sec=600, _now=1000)
    # Verify at t=1001 → expiry-now = 599s, way beyond the 30s window.
    assert fh.verify(header, key, replay_window_sec=30, _now=1001) is None
    # But the same header IS accepted when the verifier explicitly
    # widens its window to cover the claimed TTL.
    assert fh.verify(header, key, replay_window_sec=700, _now=1001) == "dennis"


def test_verify_rejects_malformed_too_few_dots(key: bytes) -> None:
    """Fewer than 3 dot-separated fields → reject."""
    assert fh.verify("dennis.1234567890", key) is None
    assert fh.verify("dennis", key) is None


def test_verify_rejects_malformed_too_many_dots(key: bytes) -> None:
    """More than 3 dot-separated fields → reject. A real operator_id
    containing a dot would arrive here (sign refuses to produce one,
    but a hand-crafted attacker payload might) and must be rejected."""
    assert fh.verify("a.b.c.d", key) is None


def test_verify_rejects_non_integer_expiry(key: bytes) -> None:
    """Expiry field that isn't parseable as int → reject."""
    assert fh.verify("dennis.notanumber.deadbeef", key) is None


def test_verify_rejects_empty_string(key: bytes) -> None:
    """Empty header value → reject without raising."""
    assert fh.verify("", key) is None


def test_verify_rejects_when_key_is_empty() -> None:
    """No key → reject. Defends against a transitional ``g.forwarding_hmac_key=None``
    being passed in by a caller that forgot to gate on the key
    presence."""
    header = fh.sign("dennis", b"\x00" * 32, ttl_sec=30)
    assert fh.verify(header, b"") is None


def test_sign_rejects_empty_operator_id(key: bytes) -> None:
    """Empty operator_id is a programmer error — raise loudly."""
    with pytest.raises(ValueError):
        fh.sign("", key)


def test_sign_rejects_operator_id_with_dot(key: bytes) -> None:
    """Dot in operator_id would corrupt the wire format. Raise at
    sign time so the bug surfaces at the source."""
    with pytest.raises(ValueError):
        fh.sign("dennis.varum", key)


def test_sign_rejects_empty_key() -> None:
    """No key → ValueError, not a silently-broken HMAC."""
    with pytest.raises(ValueError):
        fh.sign("dennis", b"")


def test_sign_rejects_nonpositive_ttl(key: bytes) -> None:
    """TTL=0 or negative → ValueError; a header that's expired the
    instant it's signed is never useful."""
    with pytest.raises(ValueError):
        fh.sign("dennis", key, ttl_sec=0)
    with pytest.raises(ValueError):
        fh.sign("dennis", key, ttl_sec=-5)


def test_tampered_operator_id_rejected(key: bytes) -> None:
    """An attacker who swaps the operator_id field while preserving
    expiry + mac MUST be rejected (the HMAC covers operator_id)."""
    header = fh.sign("dennis", key, ttl_sec=30, _now=1000)
    operator_id, expiry, mac = header.split(".")
    forged = f"attacker.{expiry}.{mac}"
    assert fh.verify(forged, key, _now=1001) is None


def test_tampered_expiry_rejected(key: bytes) -> None:
    """An attacker who pushes the expiry field forward while
    preserving the MAC must be rejected (HMAC covers expiry too)."""
    header = fh.sign("dennis", key, ttl_sec=10, _now=1000)
    operator_id, expiry, mac = header.split(".")
    new_expiry = str(int(expiry) + 1000)
    forged = f"{operator_id}.{new_expiry}.{mac}"
    assert fh.verify(forged, key, _now=1011) is None


def test_header_name_constant_is_canonical() -> None:
    """The module exposes the wire header name as a constant so the
    middleware + tests can't drift."""
    assert fh.HEADER_NAME == "X-Agent-MCP-Forwarded-Operator"
