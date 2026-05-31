"""Backend tests for POST /api/agents/<id>/edit.

The dashboard's Agents page Edit-icon needs a way to mutate the
admin-editable agent fields: `capabilities`, `color`,
`working_directory`. This PR adds a minimal admin-only REST endpoint
that updates the row using the existing `update_agent_db_field`
helper.

Contract:
  - Method: POST
  - URL:    /api/agents/<id>/edit
  - Body:   {"token": admin_token, "capabilities"?: [...], "color"?: str,
                                  "working_directory"?: str}
  - Auth:   admin token required (403 otherwise)
  - 404 when the agent_id doesn't exist
  - 200 + updated row echoed back on success
  - Omitting all editable fields → 400 (nothing to update)
"""

from __future__ import annotations

import datetime as _dt
import json
import secrets


def _admin(client) -> str:
    return client.get("/api/tokens").json()["admin_token"]


def _seed_worker(name: str = "alice") -> tuple[str, str]:
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
        (worker_token, name, "[]", now, "active", "/tmp", "#888", now),
    )
    conn.commit()
    conn.close()
    g.active_agents[worker_token] = {
        "agent_id": name,
        "status": "active",
        "created_at": now,
        "capabilities": [],
        "color": "#888",
    }
    return worker_token, name


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


# -------------------- happy path -------------------------------------


def test_edit_updates_capabilities(client) -> None:
    _seed_worker("alice")
    admin = _admin(client)
    resp = client.post(
        "/api/agents/alice/edit",
        json={"token": admin, "capabilities": ["code_edit", "file_read"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("success") is True, body

    row = _row("agents", "agent_id = ?", ("alice",))
    assert row is not None
    assert json.loads(row["capabilities"]) == ["code_edit", "file_read"]


def test_edit_updates_color(client) -> None:
    _seed_worker("alice")
    admin = _admin(client)
    resp = client.post(
        "/api/agents/alice/edit",
        json={"token": admin, "color": "#abcdef"},
    )
    assert resp.status_code == 200, resp.text

    row = _row("agents", "agent_id = ?", ("alice",))
    assert row is not None
    assert row["color"] == "#abcdef"


def test_edit_updates_working_directory(client) -> None:
    _seed_worker("alice")
    admin = _admin(client)
    resp = client.post(
        "/api/agents/alice/edit",
        json={"token": admin, "working_directory": "/workspace/alice"},
    )
    assert resp.status_code == 200, resp.text

    row = _row("agents", "agent_id = ?", ("alice",))
    assert row is not None
    assert row["working_directory"] == "/workspace/alice"


def test_edit_updates_multiple_fields_at_once(client) -> None:
    _seed_worker("alice")
    admin = _admin(client)
    resp = client.post(
        "/api/agents/alice/edit",
        json={
            "token": admin,
            "capabilities": ["one", "two"],
            "color": "#deadbe",
            "working_directory": "/home/alice",
        },
    )
    assert resp.status_code == 200, resp.text

    row = _row("agents", "agent_id = ?", ("alice",))
    assert row is not None
    assert json.loads(row["capabilities"]) == ["one", "two"]
    assert row["color"] == "#deadbe"
    assert row["working_directory"] == "/home/alice"


# -------------------- auth + validation ------------------------------


def test_edit_rejects_worker_token(client) -> None:
    _seed_worker("alice")
    worker_token, _ = _seed_worker("bob")
    resp = client.post(
        "/api/agents/alice/edit",
        json={"token": worker_token, "color": "#000000"},
    )
    assert resp.status_code in (401, 403), resp.text


def test_edit_404_when_agent_missing(client) -> None:
    admin = _admin(client)
    resp = client.post(
        "/api/agents/nonexistent/edit",
        json={"token": admin, "color": "#000000"},
    )
    assert resp.status_code == 404, resp.text


def test_edit_400_when_no_editable_fields(client) -> None:
    _seed_worker("alice")
    admin = _admin(client)
    resp = client.post(
        "/api/agents/alice/edit",
        json={"token": admin},
    )
    assert resp.status_code == 400, resp.text


def test_edit_rejects_non_whitelisted_fields(client) -> None:
    """Sending `status` or `agent_id` (not in the whitelist) must not
    touch the row — only capabilities/color/working_directory are
    editable through this endpoint."""
    _seed_worker("alice")
    admin = _admin(client)
    resp = client.post(
        "/api/agents/alice/edit",
        json={
            "token": admin,
            "status": "terminated",
            "agent_id": "renamed",
        },
    )
    # Either 400 (no editable fields supplied) or 200 (silently ignored).
    # Either way, the agents row must NOT have been mutated.
    assert resp.status_code in (200, 400), resp.text
    row = _row("agents", "agent_id = ?", ("alice",))
    assert row is not None, "alice row must still exist with the original agent_id"
    assert row["status"] == "active"
