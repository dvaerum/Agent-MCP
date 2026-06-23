"""retire-system-token Wave 1 — the system_token god-key is gone.

These tests pin the post-Wave-1 contract at the HTTP layer:

  * ``Authorization: Bearer <g.system_token>`` against a sensitive
    backend route → 401 (was the god-key; now rejected).
  * ``Authorization: Bearer <per-agent-token>`` → 200 (per-agent
    bearers are the surviving backend bearer surface).
  * ``X-Agent-MCP-Forwarded-Operator: <signed>`` with a key matching
    ``g.forwarding_hmac_key`` → 200 with ``g.current_operator``
    stamped.
  * Forwarding header with wrong HMAC → 401.
  * Forwarding header with an expired ``expiry`` → 401.

Two routes are exercised so the contract isn't pinned to one
handler's idiosyncrasies:

  * ``GET /mcp`` — guarded by ``AuthHeaderMiddleware`` directly.
  * ``GET /api/tokens`` — guarded by the ``require_operator_session``
    FastAPI dep.

Both must reject the system_token bearer after Wave 1; both must
accept the forwarding header.
"""

from __future__ import annotations

import os

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


# ---------- helpers --------------------------------------------------


def _sign_header(operator_id: str, key: bytes, ttl_sec: int = 30) -> dict:
    """Sign a forwarding-header value and return the dict form."""
    from agent_mcp.app import forwarding_header as _fh

    return {_fh.HEADER_NAME: _fh.sign(operator_id, key, ttl_sec=ttl_sec)}


# ---------- /api/tokens (require_operator_session dep) ---------------


async def test_api_route_rejects_system_token_bearer(tmp_path) -> None:
    """A request that presents the legacy system_token as a bearer
    on a route gated by ``require_operator_session`` MUST 401 after
    Wave 1.

    Pre-Wave-1 this returned 200 because the dep's legacy bearer
    branch admitted ``g.system_token``. The branch is gone; the only
    bearer paths now accepted are (a) a per-agent token via the
    agents-table or (b) the signed forwarding header (covered below).

    retire-system-token Wave 3 removed the ``g.system_token`` global
    itself. The contract this test pins is "an unrelated 32-char hex
    bearer is rejected" — i.e. only real agents-table rows or the
    signed forwarding header authenticate. We use a fresh random
    bearer here to stand in for the former god-key.
    """
    async with mcp_session(tmp_path) as admin:
        import secrets as _secrets

        bogus_bearer = _secrets.token_hex(16)
        r = admin.client.get(
            "/api/tokens",
            headers={"Authorization": f"Bearer {bogus_bearer}"},
        )
        assert r.status_code == 401, r.text


async def test_api_route_rejects_per_agent_bearer_alone(tmp_path) -> None:
    """A per-agent bearer alone does NOT pass
    ``require_operator_session``. The dep is operator-only; per-agent
    tokens are for the ``/mcp`` transport (where per-tool role gating
    handles authorization), not for the operator-tier REST surface.

    Without this, a worker token could escalate to the operator-only
    ``/api/tokens`` listing by virtue of being any-old valid agent
    bearer — see ``tests/test_tokens_endpoint_worker_guard.py`` for
    the matching expectation."""
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("worker-escalation-probe")
        r = admin.client.get(
            "/api/tokens",
            headers={"Authorization": f"Bearer {worker.token}"},
        )
        assert r.status_code == 401, r.text


async def test_api_route_accepts_signed_forwarding_header(tmp_path) -> None:
    """A request that carries a valid signed forwarding header passes
    auth and stamps ``g.current_operator`` for downstream handlers."""
    async with mcp_session(tmp_path) as admin:
        from agent_mcp.core import globals as g

        # The harness already stamped a key on ``g.forwarding_hmac_key``;
        # use it to sign a header for an arbitrary operator id and
        # confirm the middleware accepts.
        assert g.forwarding_hmac_key, (
            "harness should have stamped a forwarding HMAC key"
        )
        headers = _sign_header("alice", g.forwarding_hmac_key)

        r = admin.client.get("/api/tokens", headers=headers)
        assert r.status_code == 200, r.text


async def test_forwarding_header_with_wrong_hmac_is_rejected(tmp_path) -> None:
    """A forwarding header signed under a key that doesn't match
    ``g.forwarding_hmac_key`` MUST be rejected (401), not silently
    fall through to the bearer path."""
    async with mcp_session(tmp_path) as admin:
        # Sign with a DIFFERENT key from what the harness loaded.
        wrong_key = os.urandom(32)
        headers = _sign_header("alice", wrong_key)

        r = admin.client.get("/api/tokens", headers=headers)
        assert r.status_code == 401, r.text


async def test_forwarding_header_expired_is_rejected(tmp_path) -> None:
    """A forwarding header whose ``expiry`` claim has already passed
    MUST be rejected, even when signed under the correct key."""
    async with mcp_session(tmp_path) as admin:
        from agent_mcp.app import forwarding_header as _fh
        from agent_mcp.core import globals as g

        # Sign with _now in the deep past so expiry < now-at-verify.
        header_value = _fh.sign(
            "alice", g.forwarding_hmac_key, ttl_sec=10, _now=1
        )
        headers = {_fh.HEADER_NAME: header_value}

        r = admin.client.get("/api/tokens", headers=headers)
        assert r.status_code == 401, r.text


# ---------- /mcp (AuthHeaderMiddleware directly) ---------------------


async def test_mcp_rejects_system_token_bearer(tmp_path) -> None:
    """``POST /mcp`` with the legacy system_token bearer MUST 401 after
    Wave 1. Pin at the transport gate (the middleware itself) so we
    catch a regression even if a downstream tool layer accidentally
    re-admits the god-key.
    """
    async with mcp_session(tmp_path) as admin:
        import secrets as _secrets

        # retire-system-token Wave 3: ``g.system_token`` is gone. Use a
        # fresh random bearer to stand in for the former god-key — the
        # contract being pinned is "an unrelated 32-char hex bearer is
        # rejected at the /mcp gate".
        bogus_bearer = _secrets.token_hex(16)
        r = admin.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {bogus_bearer}",
            },
        )
        assert r.status_code == 401, r.text


async def test_mcp_accepts_per_agent_bearer(tmp_path) -> None:
    """The per-agent token surviving Wave 1 still passes
    ``AuthHeaderMiddleware`` and reaches ``tools/list``."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {admin.admin_token}",
            },
        )
        assert r.status_code == 200, r.text


async def test_mcp_accepts_signed_forwarding_header(tmp_path) -> None:
    """The forwarding header is sufficient on /mcp too — the router
    will use it post-Wave-2 for cookie-based dashboard requests that
    initiate MCP calls.
    """
    async with mcp_session(tmp_path) as admin:
        from agent_mcp.core import globals as g

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        headers.update(_sign_header("alice", g.forwarding_hmac_key))

        r = admin.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers=headers,
        )
        assert r.status_code == 200, r.text


async def test_mcp_rejects_tampered_forwarding_header(tmp_path) -> None:
    """A tampered forwarding header at /mcp MUST be rejected hard,
    not silently fall through to the bearer path."""
    async with mcp_session(tmp_path) as admin:
        wrong_key = os.urandom(32)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        headers.update(_sign_header("alice", wrong_key))
        # Even if a per-agent bearer is also present, the tampered
        # forwarding header should win the rejection — the middleware
        # never falls back when the header is present-but-invalid.
        headers["Authorization"] = f"Bearer {admin.admin_token}"

        r = admin.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers=headers,
        )
        assert r.status_code == 401, r.text
