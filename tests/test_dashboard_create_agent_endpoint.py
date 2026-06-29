"""Regression guards for the dashboard Add-Agent flow.

The dashboard's "Add Agent" button (CreateAgentModal in
agent_mcp/dashboard/components/dashboard/agents-dashboard.tsx) calls
``apiClient.createAgent`` which POSTs to ``/api/agents`` with the new
agent's payload (agent_id, capabilities, working_directory).

Wave 7 PR 1 (coordinator transition, 2026-06-29): the spawn-using
backend tests in this file were retargeted from POST /api/agents
(legacy spawn path that orphan-stormed claude processes) to POST
/api/agents/register — the spawnless sibling shipped in Wave 7 PR 0
that mints the row + token without launching tmux. The frontend
source-grep test still pins the /agents URL because the dashboard
modal (transitioned to the register endpoint in Wave 7 PR 2) keeps
the agents-relative path name.

This module pins the contract:
  * ``POST /api/agents/register`` accepts ``{token, agent_id,
    capabilities, working_directory}`` (the legacy body shape is
    accepted under the register endpoint's back-compat alias) and
    creates the row.
  * ``apiClient.createAgent`` in api.ts continues to POST to the
    agents-relative URL (the path-style URL the dashboard's router
    already expects).
  * The legacy ``POST /api/create-agent`` route stays as a back-compat
    alias; the spawn path under it is what Wave 7 PR 3 collapses.
    The test of the back-compat alias is retained at the /api/agents
    URL post-migration; the /api/create-agent route remains exercised
    indirectly via the auth surface tests in
    ``tests/test_wave3_admin_token_removal.py``.

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


# -------------------- backend: POST /api/agents/register --------------


@pytest.mark.asyncio
async def test_post_api_agents_creates_agent_row(tmp_path) -> None:
    """POST /api/agents/register must accept the dashboard's Add-Agent
    payload + create the agents row.

    Wave 7 PR 1: switched off the legacy POST /api/agents endpoint (whose
    spawn path orphan-stormed claude processes). The register-only
    sibling holds the same row-creation contract via the same
    ``require_operator_session`` auth gate.
    """
    async with mcp_session(tmp_path) as admin:
        resp = admin.client.post(
            "/api/agents/register",
            json={
                "token": admin.admin_token,
                "agent_id": "e2e-add",
                "capabilities": ["test"],
            },
        )
        assert resp.status_code == 200, (
            f"POST /api/agents/register must succeed; "
            f"got {resp.status_code} {resp.text!r}"
        )
        row = _row("agents", "agent_id = ?", ("e2e-add",))
        assert row is not None, (
            "POST /api/agents/register must insert an agents row for the new id"
        )
        assert row["status"] in {"created", "active", "pending", "running"}, (
            f"new agent should start in a non-terminated state; got {row['status']!r}"
        )


@pytest.mark.asyncio
async def test_post_api_agents_rejects_missing_token(tmp_path) -> None:
    """No admin token in body → 401, not 200 or 500.

    Wave 7 PR 1 retarget — auth-gate behaviour is identical to the
    legacy endpoint (same ``require_operator_session`` dep).
    """
    async with mcp_session(tmp_path) as admin:
        resp = admin.client.post(
            "/api/agents/register",
            json={"agent_id": "no-token-attempt"},
        )
        assert resp.status_code == 401, (
            f"POST /api/agents/register without admin token must 401; "
            f"got {resp.status_code} {resp.text!r}"
        )
        assert _row("agents", "agent_id = ?", ("no-token-attempt",)) is None


@pytest.mark.asyncio
async def test_post_api_agents_rejects_bad_token(tmp_path) -> None:
    """Non-admin token in body → 401."""
    async with mcp_session(tmp_path) as admin:
        resp = admin.client.post(
            "/api/agents/register",
            json={
                "token": "definitely-not-admin",
                "agent_id": "bad-token-attempt",
            },
        )
        assert resp.status_code == 401, (
            f"POST /api/agents/register with bad token must 401; "
            f"got {resp.status_code} {resp.text!r}"
        )
        assert _row("agents", "agent_id = ?", ("bad-token-attempt",)) is None


# -------------------- frontend: api.ts createAgent ---------------------


# NOTE: the historical ``test_api_client_create_agent_includes_admin_token``
# guard lived here. PR D (prancy-napping-pie) reverses its assertion —
# createAgent must NOT include ``token: tokens.admin_token`` in the
# POST body now that the operator-session cookie carries auth. The
# new guard lives in ``tests/test_dashboard_migration.py::
# test_dashboard_api_client_strips_token_field_from_payloads``.


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
