"""view_project_context must not leak admin token to non-admin callers.

UPSTREAM_ISSUES.md issue I: any agent with a valid token can call
view_project_context and read `config_admin_token` (the project's
admin credential). That's a direct worker→admin privilege
escalation through the tool surface.

Fix: filter rows whose context_key matches a sensitive pattern
(config_*_token, config_*_secret, etc.) when caller is not admin.
Admins continue to see everything.
"""

from __future__ import annotations

import asyncio


def _call_tool(arguments):
    """Run view_project_context_tool_impl synchronously in tests."""
    from agent_mcp.tools.project_context_tools import view_project_context_tool_impl

    return asyncio.run(view_project_context_tool_impl(arguments))


def _seed(client, *, key: str, value: str, token: str) -> None:
    r = client.post(
        "/api/memories",
        json={"token": token, "context_key": key, "context_value": value},
    )
    assert r.status_code == 200, r.text


def _admin_token(client) -> str:
    r = client.get("/api/tokens")
    assert r.status_code == 200, r.text
    return r.json()["admin_token"]


def _make_worker(client, admin_token: str) -> str:
    """Create a worker agent and return its token.

    Bypasses create_agent_tool_impl (which requires task_ids on a
    pre-existing task — too much fixture setup). Directly inserts an
    agents row and registers in g.active_agents.
    """
    import datetime as _dt
    import secrets

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
        (worker_token, "test-worker", "[]", now, "active", "/tmp", "#888", now),
    )
    conn.commit()
    conn.close()

    g.active_agents[worker_token] = {
        "agent_id": "test-worker",
        "status": "active",
        "created_at": now,
        "capabilities": [],
    }
    return worker_token


def test_admin_sees_config_admin_token(client) -> None:
    """Admins must continue to see config_admin_token (baseline)."""
    admin = _admin_token(client)
    # Seed a fake secret-looking key (config_admin_token already exists
    # from startup; we don't need to add it).
    result = _call_tool({"token": admin})
    text = result[0].text
    assert "config_admin_token" in text, (
        "admin should see config_admin_token in view_project_context output"
    )


def test_worker_does_not_see_config_admin_token(client) -> None:
    """Workers must NOT see config_admin_token — privilege escalation otherwise."""
    admin = _admin_token(client)
    worker = _make_worker(client, admin)

    result = _call_tool({"token": worker})
    text = result[0].text
    assert "config_admin_token" not in text, (
        "worker token can read config_admin_token via view_project_context — "
        "privilege escalation (issue I). Got:\n" + text[:1000]
    )
    # And the actual admin token value must not appear either.
    assert admin not in text, (
        "worker can read the literal admin token value (issue I)"
    )


def test_worker_does_not_see_other_config_secrets(client) -> None:
    """The redaction applies to any config_*_token / _secret / _password key."""
    admin = _admin_token(client)
    _seed(client, key="config_openai_secret", value="sk-very-secret-12345", token=admin)
    worker = _make_worker(client, admin)

    result = _call_tool({"token": worker})
    text = result[0].text
    assert "config_openai_secret" not in text
    assert "sk-very-secret-12345" not in text


def test_worker_still_sees_non_secret_keys(client) -> None:
    """Non-secret keys must still be visible to workers (no over-filtering)."""
    admin = _admin_token(client)
    _seed(client, key="project_notes", value="some non-secret info", token=admin)
    worker = _make_worker(client, admin)

    result = _call_tool({"token": worker})
    text = result[0].text
    assert "project_notes" in text
    assert "some non-secret info" in text
