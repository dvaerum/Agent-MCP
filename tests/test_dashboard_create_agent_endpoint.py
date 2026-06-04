"""Regression guards for the dashboard Deploy / Create-Agent flow.

The dashboard's "Deploy" button (CreateAgentModal in
agent_mcp/dashboard/components/dashboard/agents-dashboard.tsx) calls
``apiClient.createAgent`` which POSTs to ``/api/agents`` with the new
agent's payload (agent_id, capabilities, working_directory).

Pre-PR this was broken in two directions:
  1. The backend's ``/api/agents`` Route registered only GET + OPTIONS;
     a POST returned 405 Method Not Allowed.
  2. The frontend's ``createAgent`` body omitted the admin token, so
     even if a POST handler had existed the call would have 401'd.

This module pins the contract:
  * ``POST /api/agents`` accepts ``{token, agent_id, capabilities,
    working_directory}`` and creates the row.
  * ``apiClient.createAgent`` in api.ts includes the admin token in
    the request body via the same getTokens() pattern restoreAgent /
    editAgent / purgeAgent already use.
  * The legacy ``POST /api/create-agent`` route stays as a back-compat
    alias (it's been the on-disk endpoint since the dashboard was
    introduced; some out-of-tree integrations may rely on it).

Backend assertions use the standard ``tests.harness.mcp_session``
TestClient pattern (Candidate F, architecture review 2026-06-02).
Frontend assertions are source-grep on api.ts (matches
test_dashboard_no_auto_cleanup / test_dashboard_api_no_mutation_retry).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.harness import mcp_session


REPO_ROOT = Path(__file__).resolve().parent.parent
API_FILE = REPO_ROOT / "agent_mcp" / "dashboard" / "lib" / "api.ts"


def _row(table: str, where_sql: str, params: tuple) -> dict | None:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM {table} WHERE {where_sql}", params)
        r = cursor.fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


# -------------------- backend: POST /api/agents ------------------------


@pytest.mark.asyncio
async def test_post_api_agents_creates_agent_row(tmp_path) -> None:
    """The dashboard's createAgent posts to /api/agents. The route must
    accept POST + return 200 + create the agents row.

    Pre-fix: returns 405 (route registered with methods=['GET', 'OPTIONS']
    only). The dashboard's Deploy button has been silently broken since
    the dashboard was introduced.
    """
    async with mcp_session(tmp_path) as admin:
        resp = admin.client.post(
            "/api/agents",
            json={
                "token": admin.admin_token,
                "agent_id": "e2e-deploy",
                "capabilities": ["test"],
            },
        )
        assert resp.status_code == 200, (
            f"POST /api/agents must succeed; got {resp.status_code} {resp.text!r}"
        )
        row = _row("agents", "agent_id = ?", ("e2e-deploy",))
        assert row is not None, (
            "POST /api/agents must insert an agents row for the new id"
        )
        assert row["status"] in {"created", "active", "pending", "running"}, (
            f"new agent should start in a non-terminated state; got {row['status']!r}"
        )


@pytest.mark.asyncio
async def test_post_api_agents_rejects_missing_token(tmp_path) -> None:
    """No admin token in body → 401, not 200 or 500."""
    async with mcp_session(tmp_path) as admin:
        resp = admin.client.post(
            "/api/agents",
            json={"agent_id": "no-token-attempt"},
        )
        assert resp.status_code == 401, (
            f"POST /api/agents without admin token must 401; "
            f"got {resp.status_code} {resp.text!r}"
        )
        assert _row("agents", "agent_id = ?", ("no-token-attempt",)) is None


@pytest.mark.asyncio
async def test_post_api_agents_rejects_bad_token(tmp_path) -> None:
    """Non-admin token in body → 401."""
    async with mcp_session(tmp_path) as admin:
        resp = admin.client.post(
            "/api/agents",
            json={
                "token": "definitely-not-admin",
                "agent_id": "bad-token-attempt",
            },
        )
        assert resp.status_code == 401, (
            f"POST /api/agents with bad token must 401; "
            f"got {resp.status_code} {resp.text!r}"
        )
        assert _row("agents", "agent_id = ?", ("bad-token-attempt",)) is None


@pytest.mark.asyncio
async def test_post_api_create_agent_back_compat_alias_still_works(tmp_path) -> None:
    """The original on-disk endpoint /api/create-agent stays as an alias.

    Some out-of-tree integrations may POST there directly (the route
    has been the only working create endpoint since the dashboard was
    introduced). Removing it would silently break those callers.
    """
    async with mcp_session(tmp_path) as admin:
        resp = admin.client.post(
            "/api/create-agent",
            json={
                "token": admin.admin_token,
                "agent_id": "back-compat-alias",
            },
        )
        assert resp.status_code == 200, (
            f"POST /api/create-agent back-compat alias must still 200; "
            f"got {resp.status_code} {resp.text!r}"
        )
        assert _row("agents", "agent_id = ?", ("back-compat-alias",)) is not None


# -------------------- frontend: api.ts createAgent ---------------------


def test_api_client_create_agent_includes_admin_token() -> None:
    """apiClient.createAgent must send the admin token in the request
    body, same shape as restoreAgent / editAgent / purgeAgent.

    Pre-fix the body was just ``JSON.stringify(data)`` where ``data``
    only had ``{agent_id, capabilities?, working_directory?}`` — no
    token. The backend would 401 once a POST handler existed.
    """
    src = API_FILE.read_text(encoding="utf-8")
    import re

    # Locate the createAgent method body.
    match = re.search(
        r"async\s+createAgent\s*\([^)]*\)[^{]*\{(.*?)\n  \}",
        src,
        re.DOTALL,
    )
    assert match, (
        f"Couldn't locate createAgent() in {API_FILE.name}; rename? "
        "Update this test."
    )
    body = match.group(1)
    # The body must reference getTokens() (the existing pattern that
    # fetches admin/agent tokens) AND must mention `admin_token` so the
    # POSTed body carries the bearer.
    assert "getTokens" in body, (
        "createAgent must call this.getTokens() to pull the admin token, "
        "matching the convention used by restoreAgent / editAgent / "
        "purgeAgent. Pre-fix the call sent no token and the backend 401'd."
    )
    assert "admin_token" in body, (
        "createAgent must include admin_token in the POST body. Without "
        "it the backend's verify_token check returns 401 and the Deploy "
        "button silently fails."
    )


def test_api_client_create_agent_targets_post_agents() -> None:
    """createAgent must POST to /agents (the modern path-style URL the
    dashboard already expects in its router) — NOT to a relative URL
    that doesn't match a registered route.
    """
    src = API_FILE.read_text(encoding="utf-8")
    import re

    match = re.search(
        r"async\s+createAgent\s*\([^)]*\)[^{]*\{(.*?)\n  \}",
        src,
        re.DOTALL,
    )
    assert match, "createAgent rename?"
    body = match.group(1)
    assert "method: 'POST'" in body or 'method: "POST"' in body, (
        "createAgent must be a POST."
    )
    # Either the modern '/agents' shape (which the backend POST handler
    # accepts post-fix) or the legacy '/create-agent' alias is OK; pin
    # one of them so a typo doesn't drift the URL.
    assert (
        "'/agents'" in body
        or '"/agents"' in body
        or "'/create-agent'" in body
        or '"/create-agent"' in body
    ), (
        "createAgent must POST to /agents (modern) or /create-agent "
        "(back-compat alias)."
    )
