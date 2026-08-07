"""Unit tests for the signed forwarding-header (Wave 1 + SEC-1).

The module under test (``agent_mcp.app.forwarding_header``) is the
sign / verify primitive that lets the router carry an authenticated
operator identity — AND their real per-project role — to the
per-project backend without sharing a god-key bearer.

SEC-1 (2026-07): the header now carries a ``role`` field
(``operator`` / ``viewer``) inside the HMAC. The backend parses it
onto ``Principal.project_role`` so a viewer gets viewer caps, not the
operator bundle. These tests pin the four-field wire format, the role
round-trip, and the hard-reject of tampered / unknown roles.
"""

from __future__ import annotations

import os

import pytest

from agent_mcp.app import forwarding_header as fh


@pytest.fixture
def key() -> bytes:
    """A fresh 32-byte HMAC key per test."""
    return os.urandom(32)


def test_sign_then_verify_roundtrip_returns_operator_id_and_role(
    key: bytes,
) -> None:
    """A header signed with key K verifies under key K and yields the
    same (operator_id, role) the signer passed in."""
    header = fh.sign("dennis", "operator", key, ttl_sec=30)
    assert fh.verify(header, key) == ("dennis", "operator")


def test_sign_then_verify_roundtrip_viewer_role(key: bytes) -> None:
    """The role round-trips faithfully for a viewer too — this is the
    SEC-1 fix: a viewer's role is carried, not collapsed to operator."""
    header = fh.sign("alice", "viewer", key, ttl_sec=30)
    assert fh.verify(header, key) == ("alice", "viewer")


def test_wire_format_is_four_dot_separated_fields(key: bytes) -> None:
    """``operator_id.role.expiry.hmac`` — exactly four fields."""
    header = fh.sign("dennis", "viewer", key, ttl_sec=30)
    parts = header.split(".")
    assert len(parts) == 4
    assert parts[0] == "dennis"
    assert parts[1] == "viewer"


def test_verify_with_wrong_key_returns_none(key: bytes) -> None:
    """The single most important property: a header forged by an
    attacker who doesn't hold the per-project key cannot satisfy the
    backend verifier."""
    other_key = os.urandom(32)
    header = fh.sign("dennis", "operator", key, ttl_sec=30)
    assert fh.verify(header, other_key) is None


def test_verify_rejects_expired_header(key: bytes) -> None:
    """A header whose claimed expiry is in the past is rejected even
    when signed by the legitimate key. Defends against capture-and-
    replay outside the freshness window."""
    header = fh.sign("dennis", "operator", key, ttl_sec=10, _now=1000)
    assert fh.verify(header, key, _now=2000) is None


def test_verify_accepts_unexpired_header(key: bytes) -> None:
    """Negative-space of the expiry check: at verify time t<expiry the
    header is accepted."""
    header = fh.sign("dennis", "operator", key, ttl_sec=30, _now=1000)
    assert fh.verify(header, key, _now=1015) == ("dennis", "operator")


def test_verify_enforces_replay_window(key: bytes) -> None:
    """A header whose ``expiry`` is more than ``replay_window_sec`` in
    the future at verify time is rejected even if the HMAC is valid."""
    header = fh.sign("dennis", "operator", key, ttl_sec=600, _now=1000)
    assert fh.verify(header, key, replay_window_sec=30, _now=1001) is None
    # But the same header IS accepted when the verifier explicitly
    # widens its window to cover the claimed TTL.
    assert fh.verify(
        header, key, replay_window_sec=700, _now=1001
    ) == ("dennis", "operator")


def test_verify_rejects_malformed_too_few_dots(key: bytes) -> None:
    """Fewer than 4 dot-separated fields → reject."""
    assert fh.verify("dennis.operator.1234567890", key) is None
    assert fh.verify("dennis.1234567890", key) is None
    assert fh.verify("dennis", key) is None


def test_verify_rejects_malformed_too_many_dots(key: bytes) -> None:
    """More than 4 dot-separated fields → reject."""
    assert fh.verify("a.operator.b.c.d", key) is None


def test_verify_rejects_non_integer_expiry(key: bytes) -> None:
    """Expiry field that isn't parseable as int → reject."""
    assert fh.verify("dennis.operator.notanumber.deadbeef", key) is None


def test_verify_rejects_empty_string(key: bytes) -> None:
    """Empty header value → reject without raising."""
    assert fh.verify("", key) is None


def test_verify_rejects_when_key_is_empty() -> None:
    """No key → reject."""
    header = fh.sign("dennis", "operator", b"\x00" * 32, ttl_sec=30)
    assert fh.verify(header, b"") is None


def test_verify_rejects_unknown_role(key: bytes) -> None:
    """SEC-1: a role outside the known set (``operator`` / ``viewer``)
    is hard-rejected even when the rest of the header is well-formed.

    An attacker who somehow injects a header claiming
    ``role="sysadmin"`` (or any unrecognised tier) must NOT be admitted
    — an unknown role must never reach ``resolve_capabilities`` where a
    surprise value could be mishandled into a broad grant."""
    # Hand-craft a header with an unknown role, correctly HMAC'd, so the
    # ONLY thing wrong is the role value. We reach into the private
    # helper to sign the exact bytes — proving the role check rejects
    # even a cryptographically-valid header.
    expiry = 10_000
    forged_mac = fh._hmac_hex("dennis", "sysadmin", expiry, key)
    forged = f"dennis.sysadmin.{expiry}.{forged_mac}"
    assert fh.verify(forged, key, _now=expiry - 5) is None


def test_verify_rejects_empty_role_field(key: bytes) -> None:
    """An empty role field → reject."""
    expiry = 10_000
    forged_mac = fh._hmac_hex("dennis", "", expiry, key)
    forged = f"dennis..{expiry}.{forged_mac}"
    assert fh.verify(forged, key, _now=expiry - 5) is None


def test_tampered_role_rejected(key: bytes) -> None:
    """SEC-1 core property: an attacker who swaps a signed ``viewer``
    role for ``operator`` while keeping the original MAC is rejected —
    the role is inside the HMAC, so the swap breaks the signature."""
    header = fh.sign("alice", "viewer", key, ttl_sec=30, _now=1000)
    operator_id, role, expiry, mac = header.split(".")
    assert role == "viewer"
    forged = f"{operator_id}.operator.{expiry}.{mac}"
    assert fh.verify(forged, key, _now=1001) is None


def test_sign_rejects_empty_operator_id(key: bytes) -> None:
    """Empty operator_id is a programmer error — raise loudly."""
    with pytest.raises(ValueError):
        fh.sign("", "operator", key)


def test_sign_rejects_operator_id_with_dot(key: bytes) -> None:
    """Dot in operator_id would corrupt the wire format. Raise at
    sign time so the bug surfaces at the source."""
    with pytest.raises(ValueError):
        fh.sign("dennis.varum", "operator", key)


def test_sign_rejects_unknown_role(key: bytes) -> None:
    """SEC-1: ``sign`` refuses to mint a header for a role outside the
    known set — the router should never sign an unrecognised tier."""
    with pytest.raises(ValueError):
        fh.sign("dennis", "sysadmin", key)
    with pytest.raises(ValueError):
        fh.sign("dennis", "", key)


def test_sign_rejects_empty_key() -> None:
    """No key → ValueError, not a silently-broken HMAC."""
    with pytest.raises(ValueError):
        fh.sign("dennis", "operator", b"")


def test_sign_rejects_nonpositive_ttl(key: bytes) -> None:
    """TTL=0 or negative → ValueError."""
    with pytest.raises(ValueError):
        fh.sign("dennis", "operator", key, ttl_sec=0)
    with pytest.raises(ValueError):
        fh.sign("dennis", "operator", key, ttl_sec=-5)


def test_tampered_operator_id_rejected(key: bytes) -> None:
    """An attacker who swaps the operator_id field while preserving
    role/expiry/mac MUST be rejected (the HMAC covers operator_id)."""
    header = fh.sign("dennis", "operator", key, ttl_sec=30, _now=1000)
    _operator_id, role, expiry, mac = header.split(".")
    forged = f"attacker.{role}.{expiry}.{mac}"
    assert fh.verify(forged, key, _now=1001) is None


def test_tampered_expiry_rejected(key: bytes) -> None:
    """An attacker who pushes the expiry field forward while preserving
    the MAC must be rejected (HMAC covers expiry too)."""
    header = fh.sign("dennis", "operator", key, ttl_sec=10, _now=1000)
    operator_id, role, expiry, mac = header.split(".")
    new_expiry = str(int(expiry) + 1000)
    forged = f"{operator_id}.{role}.{new_expiry}.{mac}"
    assert fh.verify(forged, key, _now=1011) is None


def test_header_name_constant_is_canonical() -> None:
    """The module exposes the wire header name as a constant so the
    middleware + tests can't drift."""
    assert fh.HEADER_NAME == "X-Agent-MCP-Forwarded-Operator"


def test_valid_roles_constant() -> None:
    """The known role vocabulary is exactly operator + viewer."""
    assert fh.VALID_ROLES == frozenset({"operator", "viewer"})
