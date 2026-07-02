"""Security: agent bearer tokens must not leak to viewer-tier operators.

Vulnerability (audit 2026-07): three GET endpoints returned agent
bearer tokens to any caller the router admitted. The router admits
viewer-tier operators on GET requests (the operator/viewer split is
enforced only on mutations), so a read-only viewer could harvest an
agent's bearer token and replay it as that agent to escalate to write.

Tier is NOT resolvable in the per-project FastAPI backend: the router
knows the project role but does not forward it (the forwarding header
carries only ``operator_id``; the cookie path resolves a user but no
project role). By design the backend never resolves project role —
that is the router middleware's job. See ``require_operator_session``
in ``agent_mcp/app/deps.py``.

The only auth path where the backend can CONFIRM operator tier is the
per-agent operator-tier bearer (``kind == "operator_bearer"``:
manager/admin agent row, worker tokens rejected). For the
cookie/session (``"session"``) and forwarding-header (``"forwarding"``)
paths the tier is unverifiable — could be a viewer — so tokens are
withheld.

  * ``GET /api/tokens``  → 403 unless the caller is a confirmed
    operator-tier bearer.
  * ``GET /api/all-data`` → per-agent ``auth_token`` included only for
    a confirmed operator-tier bearer; omitted otherwise.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


# ── GET /api/tokens ────────────────────────────────────────────────


async def test_tokens_operator_bearer_still_returns_200(tmp_path) -> None:
    """Confirmed operator-tier bearer keeps the full token list."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("w1")
        r = admin.client.get(
            "/api/tokens",
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 200, r.text
        assert "agent_tokens" in r.json()


async def test_tokens_forwarding_operator_returns_403(tmp_path) -> None:
    """The forwarding/session path (a viewer arrives here too) is not a
    confirmed operator-tier caller → 403, and no token leaks."""
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("w1")
        r = admin.get("/api/tokens")
        assert r.status_code == 403, r.text
        assert worker.token not in r.text, "agent token leaked in 403 body"
        assert admin.admin_token not in r.text


# ── GET /api/all-data ──────────────────────────────────────────────


async def test_all_data_operator_bearer_includes_auth_token(tmp_path) -> None:
    """Confirmed operator-tier bearer keeps per-agent ``auth_token``."""
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("w1")
        r = admin.client.get(
            "/api/all-data",
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 200, r.text
        agents = r.json().get("agents", [])
        w1 = next((a for a in agents if a.get("agent_id") == "w1"), None)
        assert w1 is not None, f"w1 missing from agents: {agents}"
        assert w1.get("auth_token") == worker.token, (
            "operator-tier bearer must still receive per-agent auth_token"
        )
        # The raw ``token`` column (from the agents SELECT *) must never
        # be surfaced — the canonical field is the gated ``auth_token``.
        assert "token" not in w1, (
            f"raw agents.token column leaked in all-data; keys: {list(w1)}"
        )


async def test_all_data_worker_bearer_returns_401(tmp_path) -> None:
    """A worker bearer is not operator-tier → rejected by the dep (401),
    never reaching the token-bearing payload."""
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("w1")
        r = admin.client.get(
            "/api/all-data",
            headers={"Authorization": f"Bearer {worker.token}"},
        )
        assert r.status_code == 401, r.text


async def test_all_data_forwarding_operator_omits_auth_token(tmp_path) -> None:
    """The forwarding/session path (viewer-reachable) must NOT receive
    per-agent bearer tokens, but the agents list is otherwise intact."""
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("w1")
        r = admin.get("/api/all-data")
        assert r.status_code == 200, r.text
        body = r.json()
        assert worker.token not in r.text, (
            "agent bearer token leaked to forwarding/session caller"
        )
        agents = body.get("agents", [])
        w1 = next((a for a in agents if a.get("agent_id") == "w1"), None)
        assert w1 is not None, "agents list must still be populated"
        assert w1.get("auth_token") in (None, ""), (
            "auth_token must be omitted/blank for unverifiable-tier caller"
        )
        # Other safe fields still present.
        assert "status" in w1
