"""Security: GET /api/node-details must not leak agent bearer tokens.

Vulnerability (audit 2026-07): the node-details handler had NO auth
dependency and, for an ``agent_<id>`` node, ran ``SELECT * FROM agents``
and returned ``dict(row)`` verbatim — including the secret ``token``
column (the agent's bearer). Any caller who could reach the URL (and,
via the router's GET-admits-viewers rule, any viewer-tier operator)
could read an agent's bearer token and replay it to escalate from
read-only to write.

The fix:

  1. Gate the endpoint behind ``require_operator_session`` so it is no
     longer open to unauthenticated callers.
  2. Replace the ``SELECT *`` on ``agents`` with an explicit
     safe-column projection that EXCLUDES ``token`` (and the
     ``aoe_session_id`` side-channel session id), so no auth tier ever
     receives the bearer via this panel.

These tests pin both halves: the token never appears in the agent
node payload, and the safe display fields still do.
"""

from __future__ import annotations

import datetime
import secrets

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


def _seed_agent(agent_id: str, token: str, agent_role: str = "worker") -> None:
    """INSERT a bare agents row directly (harness convention)."""
    from agent_mcp.db.connection import get_db_connection

    now = datetime.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO agents (token, agent_id, "
            "created_at, status, working_directory, color, updated_at, "
            "agent_role) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                token, agent_id, now, "active", "/tmp", "#abc",
                now, agent_role,
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def test_node_details_unauthenticated_returns_401(tmp_path) -> None:
    """No auth at all → 401. The endpoint used to have no gate."""
    async with mcp_session(tmp_path) as admin:
        secret = secrets.token_hex(16)
        _seed_agent("leaky-agent", secret)
        r = admin.client.get("/api/node-details?node_id=agent_leaky-agent")
        assert r.status_code == 401, r.text
        assert secret not in r.text, "bearer token leaked in 401 body"


async def test_node_details_operator_bearer_omits_token(tmp_path) -> None:
    """Even a confirmed operator-tier caller must NOT receive the
    agent's bearer token — the projection excludes it for everyone."""
    async with mcp_session(tmp_path) as admin:
        secret = secrets.token_hex(16)
        _seed_agent("leaky-agent", secret)
        r = admin.client.get(
            "/api/node-details?node_id=agent_leaky-agent",
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 200, r.text
        assert secret not in r.text, (
            "agent bearer token leaked in node-details response"
        )
        data = r.json().get("data", {})
        assert "token" not in data, (
            f"node-details agent data must not carry 'token'; keys: "
            f"{list(data.keys())}"
        )
        # Safe display fields survive.
        assert data.get("agent_id") == "leaky-agent"
        assert data.get("status") == "active"
        assert data.get("agent_role") == "worker"


async def test_node_details_worker_bearer_returns_401(tmp_path) -> None:
    """A worker bearer is not operator-tier → rejected by the new gate."""
    async with mcp_session(tmp_path) as admin:
        secret = secrets.token_hex(16)
        _seed_agent("leaky-agent", secret)
        worker = await admin.create_worker("nosy-worker")
        r = admin.client.get(
            "/api/node-details?node_id=agent_leaky-agent",
            headers={"Authorization": f"Bearer {worker.token}"},
        )
        assert r.status_code == 401, r.text
        assert secret not in r.text, "bearer token leaked to worker"


async def test_node_details_forwarding_operator_omits_token(tmp_path) -> None:
    """The dashboard/operator-session (forwarding) path — the same path
    a viewer-tier operator arrives on for a GET — must not leak the
    token either."""
    async with mcp_session(tmp_path) as admin:
        secret = secrets.token_hex(16)
        _seed_agent("leaky-agent", secret)
        # admin.get() attaches the signed forwarding header (the
        # operator-session path; tier is unverifiable in the backend).
        r = admin.get("/api/node-details?node_id=agent_leaky-agent")
        assert r.status_code == 200, r.text
        assert secret not in r.text, (
            "agent bearer token leaked to forwarding/session caller"
        )
        assert "token" not in r.json().get("data", {})
