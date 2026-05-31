"""GET /api/tokens must not return admin_token to non-admin bearers.

UPSTREAM_ISSUES.md issue O (filed during this plan execution). Today
`/api/tokens` returns the admin token in plaintext to any HTTP
caller. Anyone on the network who can reach the endpoint can
escalate to admin by curling `/api/tokens`. Same shape as issue I
(view_project_context) but via the REST surface.

Fix: when the request carries `Authorization: Bearer <worker_token>`,
return 403. Unauthenticated requests (the dashboard's normal usage)
still get the full response, consistent with the "dashboard = admin
by design" stance (ADR-0003).
"""

from __future__ import annotations

import datetime as _dt
import secrets


def _admin_token(client) -> str:
    r = client.get("/api/tokens")
    assert r.status_code == 200, r.text
    return r.json()["admin_token"]


def _seed_worker(admin_token_value: str) -> str:
    """Insert a worker row + register in globals so verify_token recognizes it."""
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    worker_token = secrets.token_hex(16)
    now = _dt.datetime.now().isoformat()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO agents (token, agent_id, capabilities, created_at, "
        "status, working_directory, color, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (worker_token, "worker-x", "[]", now, "active", "/tmp", "#888", now),
    )
    conn.commit()
    conn.close()

    g.active_agents[worker_token] = {
        "agent_id": "worker-x",
        "status": "active",
        "created_at": now,
        "capabilities": [],
    }
    return worker_token


def test_tokens_endpoint_unauthenticated_still_returns_admin_token(client) -> None:
    """Baseline: no Authorization header → admin_token returned (preserves
    dashboard-as-admin behavior in path-prefixed deployments)."""
    r = client.get("/api/tokens")
    assert r.status_code == 200, r.text
    assert "admin_token" in r.json()


def test_tokens_endpoint_with_admin_bearer_returns_admin_token(client) -> None:
    """Admin Authorization header → admin_token returned."""
    admin = _admin_token(client)
    r = client.get("/api/tokens", headers={"Authorization": f"Bearer {admin}"})
    assert r.status_code == 200, r.text
    assert r.json()["admin_token"] == admin


def test_tokens_endpoint_with_worker_bearer_returns_403(client) -> None:
    """Worker Authorization header → 403, admin_token NEVER appears in response.

    Without this, any worker token can `curl -H 'Authorization: Bearer
    <worker>' /api/tokens` and read the admin token. Issue O.
    """
    admin = _admin_token(client)
    worker = _seed_worker(admin)

    r = client.get("/api/tokens", headers={"Authorization": f"Bearer {worker}"})
    assert r.status_code == 403, r.text
    body = r.text
    assert admin not in body, (
        "worker bearer received the admin token in response body — escalation"
    )
