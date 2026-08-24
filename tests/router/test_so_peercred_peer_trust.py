"""Unified trusted-peer detection, kernel-verified for UDS peers.

Two independently-maintained functions used to answer "may this direct
peer set ``X-Forwarded-*`` / the SSO trust header?" and they DISAGREED
on the one input class that matters for a Unix-socket-fronted deploy:

  * ``rate_limit._is_trusted_proxy`` saw an empty ``request.remote``
    (the shape a UDS connection produces) and returned True — "it's a
    UDS, so it must be the reverse proxy".
  * ``sso.is_trusted_proxy_source`` saw the same empty string, failed to
    parse it as an IP, and returned False.

The empty string is not evidence of anything: it is simply the absence
of a peer *address*, which is what AF_UNIX sockets have. The property
``rate_limit`` was reaching for — "the thing on the other end is our own
co-located reverse proxy" — is a real, unspoofable kernel fact available
via ``SO_PEERCRED``, so these tests pin it to that instead of to a
heuristic:

  1. both public entry points return the SAME verdict for the same peer,
  2. a UDS peer is trusted iff the kernel-reported peer UID is the
     router's own UID, or is in ``AGENT_MCP_TRUSTED_PEER_UIDS``,
  3. everything else about the TCP path is unchanged — an allowlist
     membership test, per caller, with no implicit trust.

``SO_PEERCRED`` cannot be mocked meaningfully (the credentials only
exist on a real connected socket), so these tests use
``socket.socketpair(AF_UNIX, …)``: two genuinely connected endpoints in
THIS process, whose kernel-reported peer credentials are this process's
own pid/uid/gid. The UID-mismatch branch is exercised by moving the
OTHER side of the comparison (``os.getuid``), never by faking the
syscall's answer.
"""

from __future__ import annotations

import os
import socket
import struct
from unittest import mock

import pytest

from agent_mcp.router import rate_limit, sso
from tests.router.uds_peer import uds_peer_socket

# ── Helpers ────────────────────────────────────────────────────────


@pytest.fixture
def uds_socketpair() -> socket.socket:
    """One end of a real connected AF_UNIX pair (see ``uds_peer.py``)."""
    return uds_peer_socket()


def _request(*, peername, sock: socket.socket | None = None):
    """A mocked request whose transport reports ``peername`` / ``sock``.

    ``peername=""`` reproduces the UDS shape (aiohttp's ``request.remote``
    is the empty string); a ``(host, port)`` tuple reproduces a TCP peer.
    """
    from aiohttp.test_utils import make_mocked_request

    transport = mock.Mock()
    transport.get_extra_info = lambda key, default=None: (
        peername if key == "peername"
        else sock if key == "socket"
        else default
    )
    return make_mocked_request("GET", "/agent-mcp/x", transport=transport)


def _sso_settings(trusted_ips: frozenset[str]) -> sso.ProxyHeaderSettings:
    return sso.ProxyHeaderSettings(
        trust_header="Remote-User",
        trusted_ips=trusted_ips,
        default_is_sysadmin=False,
    )


def _both_verdicts(request, *, trusted_ips: frozenset[str]) -> tuple[bool, bool]:
    """``(rate_limit verdict, sso verdict)`` for the same request/allowlist."""
    cfg = rate_limit.RateLimitConfig.from_env()
    cfg = type(cfg)(**{**cfg.__dict__, "trusted_proxies": trusted_ips})
    return (
        rate_limit._is_trusted_proxy(request, cfg),
        sso.is_trusted_proxy_source(request, _sso_settings(trusted_ips)),
    )


@pytest.fixture(autouse=True)
def _clean_peer_uid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No inherited ``AGENT_MCP_TRUSTED_PEER_UIDS`` leaking into a case."""
    monkeypatch.delenv("AGENT_MCP_TRUSTED_PEER_UIDS", raising=False)
    rate_limit._parse_trusted_peer_uids.cache_clear()
    sso._reset_cache_for_tests()


# ── The disagreement itself ────────────────────────────────────────


def test_both_entry_points_agree_on_a_uds_peer(uds_socketpair) -> None:
    """The live disagreement: ``rate_limit`` said True unconditionally,
    ``sso`` said False, for the exact same UDS-shaped request."""
    req = _request(peername="", sock=uds_socketpair)
    rl, ss = _both_verdicts(req, trusted_ips=frozenset({"127.0.0.1"}))
    assert rl == ss, (
        f"rate_limit={rl} but sso={ss} for the same UDS peer — the two "
        "trust checks must not diverge"
    )
    # And the shared answer is the kernel-verified one: the socketpair's
    # peer IS this process, so its UID is the router's own UID.
    assert rl is True


def test_both_entry_points_agree_on_an_untrusted_tcp_peer() -> None:
    """A real, non-allowlisted TCP peer is untrusted on both sides."""
    req = _request(peername=("203.0.113.7", 40000))
    assert _both_verdicts(req, trusted_ips=frozenset({"127.0.0.1"})) == (
        False, False,
    )


def test_both_entry_points_agree_on_an_allowlisted_tcp_peer() -> None:
    """An explicitly allowlisted TCP peer is trusted on both sides."""
    req = _request(peername=("10.99.99.99", 40000))
    assert _both_verdicts(req, trusted_ips=frozenset({"10.99.99.99"})) == (
        True, True,
    )


# ── The kernel fact ────────────────────────────────────────────────


def test_so_peercred_reports_this_process_for_a_socketpair(
    uds_socketpair,
) -> None:
    """Baseline: the helper reads the REAL kernel credentials, not a
    stand-in. ``socketpair`` peers are this very process."""
    assert rate_limit.peer_uid(uds_socketpair) == os.getuid()
    # ...and that really is what the kernel reports, independently read.
    raw = uds_socketpair.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"),
    )
    _pid, uid, _gid = struct.unpack("3i", raw)
    assert uid == os.getuid()


def test_uds_peer_with_foreign_uid_is_not_trusted(
    uds_socketpair, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A UDS peer whose kernel-reported UID is NOT the router's own (and
    not configured) MUST NOT be trusted.

    New coverage, not a regression pin: before the fix EVERY empty-peer
    connection was trusted by ``rate_limit`` and none by ``sso``, so this
    case did not exist as a distinct outcome on either side.

    The syscall's answer is left alone — this moves the router's own
    identity instead, which is the same comparison from the other end.
    """
    monkeypatch.setattr(os, "getuid", lambda: 99999)
    req = _request(peername="", sock=uds_socketpair)
    assert _both_verdicts(req, trusted_ips=frozenset({"127.0.0.1"})) == (
        False, False,
    )


def test_uds_peer_uid_in_configured_allowlist_is_trusted(
    uds_socketpair, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AGENT_MCP_TRUSTED_PEER_UIDS`` is the escape hatch for a proxy
    that legitimately runs as a different user."""
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: 99999)
    monkeypatch.setenv("AGENT_MCP_TRUSTED_PEER_UIDS", f"4242,{real_uid}")
    rate_limit._parse_trusted_peer_uids.cache_clear()
    req = _request(peername="", sock=uds_socketpair)
    assert _both_verdicts(req, trusted_ips=frozenset({"127.0.0.1"})) == (
        True, True,
    )


def test_garbage_in_trusted_peer_uids_is_dropped_not_fatal(
    uds_socketpair, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo'd entry drops out with a warning; the valid ones still work
    (same posture as the trusted-IP parsers)."""
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: 99999)
    monkeypatch.setenv(
        "AGENT_MCP_TRUSTED_PEER_UIDS", f" not-a-uid , , {real_uid} ",
    )
    rate_limit._parse_trusted_peer_uids.cache_clear()
    req = _request(peername="", sock=uds_socketpair)
    assert _both_verdicts(req, trusted_ips=frozenset())[0] is True


# ── Fail-closed paths ──────────────────────────────────────────────


def test_uds_peer_without_a_readable_socket_is_not_trusted() -> None:
    """No socket on the transport → no kernel fact → no trust.

    Previously this was the plain "empty remote, therefore trusted"
    branch, i.e. the one an attacker would want to reach.
    """
    req = _request(peername="", sock=None)
    assert _both_verdicts(req, trusted_ips=frozenset({"127.0.0.1"})) == (
        False, False,
    )


def test_peer_uid_is_none_on_a_non_uds_socket() -> None:
    """A TCP socket has no peer credentials; the helper reports None
    rather than inventing one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        assert rate_limit.peer_uid(sock) is None


def test_platform_without_so_peercred_fails_closed(
    uds_socketpair, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SO_PEERCRED`` is Linux-only. On macOS/BSD the kernel fact is
    unavailable, so a UDS peer is NOT trusted — never default-trusted."""
    monkeypatch.delattr(socket, "SO_PEERCRED", raising=True)
    assert rate_limit.peer_uid(uds_socketpair) is None
    req = _request(peername="", sock=uds_socketpair)
    assert _both_verdicts(req, trusted_ips=frozenset({"127.0.0.1"})) == (
        False, False,
    )


# ── The TCP allowlist is per-caller and NOT widened ────────────────


def test_sso_does_not_inherit_the_rate_limiter_default_loopback_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The N3 Tier 1 regression guard, at unit level.

    ``rate_limit``'s allowlist defaults to loopback; ``sso``'s is
    operator-configured with no default. Centralising the check MUST NOT
    union the two — otherwise any loopback-originating request could
    forge the SSO trust header (the integration-level guard for this is
    ``test_sso_proxy_header.test_trusted_header_from_untrusted_source_rejected``).
    """
    monkeypatch.setenv("AGENT_MCP_SSO_PROXY_HEADER", "Remote-User")
    monkeypatch.setenv("AGENT_MCP_SSO_PROXY_TRUSTED_IPS", "10.99.99.99")
    sso._reset_cache_for_tests()
    try:
        req = _request(peername=("127.0.0.1", 40000))
        settings = sso.get_sso_config(reload=True).proxy
        assert settings is not None
        assert sso.is_trusted_proxy_source(req, settings) is False
        # The rate limiter, with its own loopback-defaulted allowlist,
        # still trusts that same peer — deliberately different config,
        # same mechanism.
        assert rate_limit._is_trusted_proxy(
            req, rate_limit.RateLimitConfig.from_env(),
        ) is True
    finally:
        sso._reset_cache_for_tests()
