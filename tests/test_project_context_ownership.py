"""Per-entry creator-ownership rules for project_context writes/deletes.

Phase 7b — locked design (built on top of Phase 7a's SQLAlchemy + Alembic
infrastructure). Workers can create new entries (recording themselves as
the creator) and can edit/delete only entries they themselves created.
`config_*` keys remain admin-only regardless. Admins can do anything.

The bulk-update path must be atomic: a single unauthorized entry in the
batch rejects all in a single SQLAlchemy transaction.
"""

from __future__ import annotations

import json
import secrets
import sqlite3


# The MCP-tool write paths post results through `execute_db_write`, whose
# queue worker runs on the Starlette TestClient's lifespan event loop.
# Calling those tools from a fresh `asyncio.run` loop would block forever
# (the queue.put lands on a different loop than the worker's). We use
# the TestClient's anyio BlockingPortal so each tool call runs on the
# same loop that owns the write queue.
def _portal_call(client, coro_func, *args, **kwargs):
    return client.portal.call(coro_func, *args, **kwargs)


def _run_tool(client, tool_impl, arguments):
    """Run an async MCP tool implementation on the lifespan event loop."""
    async def _wrapped():
        return await tool_impl(arguments)
    return _portal_call(client, _wrapped)


def _admin_token(client) -> str:
    r = client.get("/api/tokens")
    assert r.status_code == 200, r.text
    return r.json()["admin_token"]


def _make_worker(name: str) -> str:
    """Register a worker agent directly in DB + g.active_agents.

    Mirrors the helper in test_view_project_context_redaction.py.
    """
    import datetime as _dt

    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    token = secrets.token_hex(16)
    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO agents (token, agent_id, capabilities, created_at, "
        "status, working_directory, color, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (token, name, "[]", now, "active", "/tmp", "#888", now),
    )
    conn.commit()
    conn.close()
    g.active_agents[token] = {
        "agent_id": name,
        "status": "active",
        "created_at": now,
        "capabilities": [],
    }
    return token


def _row(key: str) -> dict | None:
    """Read a project_context row as a plain dict (post-migration shape)."""
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM project_context WHERE context_key = ?", (key,))
    r = cursor.fetchone()
    conn.close()
    return dict(r) if r else None


# === Schema migration shape test ===
def test_migration_results_in_expected_schema(client) -> None:
    """Post-startup, project_context must carry the ownership columns
    (created_at, created_by) and the rename of last_updated -> updated_at."""
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(project_context)")
        cols = {row["name"] for row in cursor.fetchall()}
    finally:
        conn.close()

    assert "created_at" in cols
    assert "created_by" in cols
    assert "updated_at" in cols
    assert "updated_by" in cols
    assert (
        "last_updated" not in cols
    ), "rename of last_updated -> updated_at must have happened"


def test_orm_model_has_ownership_columns() -> None:
    """SQLAlchemy ProjectContext model must expose the ownership columns."""
    from agent_mcp.db.models import ProjectContext

    cols = {c.name for c in ProjectContext.__table__.columns}
    assert cols == {
        "context_key",
        "value",
        "description",
        "created_at",
        "created_by",
        "updated_at",
        "updated_by",
    }, f"ORM columns drifted: {cols}"


# === 15-scenario auth matrix ===

def test_1_admin_creates_key(client) -> None:
    from agent_mcp.tools.project_context_tools import update_project_context_tool_impl

    admin = _admin_token(client)
    r = _run_tool(client, update_project_context_tool_impl, {"token": admin, "context_key": "foo", "context_value": "v1"})
    assert "successfully" in r[0].text.lower(), r[0].text
    row = _row("foo")
    assert row is not None
    assert row["created_by"] == "admin"
    assert row["updated_by"] == "admin"
    assert row["created_at"]
    assert row["updated_at"]


def test_2_admin_edits_own_key_preserves_creator(client) -> None:
    from agent_mcp.tools.project_context_tools import update_project_context_tool_impl

    admin = _admin_token(client)
    _run_tool(client, update_project_context_tool_impl, {"token": admin, "context_key": "foo", "context_value": "v1"})
    row = _row("foo")
    created_at_before = row["created_at"]

    _run_tool(client, update_project_context_tool_impl, {"token": admin, "context_key": "foo", "context_value": "v2"})
    row = _row("foo")
    assert row["updated_by"] == "admin"
    assert row["created_by"] == "admin"
    assert row["created_at"] == created_at_before, (
        "created_at must be preserved across edits"
    )


def test_3_worker_creates_nonconfig_key(client) -> None:
    from agent_mcp.tools.project_context_tools import update_project_context_tool_impl

    _admin_token(client)
    worker_a = _make_worker("worker-A")
    r = _run_tool(client, update_project_context_tool_impl, {"token": worker_a, "context_key": "bar", "context_value": "x"})
    assert "successfully" in r[0].text.lower(), r[0].text
    row = _row("bar")
    assert row is not None
    assert row["created_by"] == "worker-A"
    assert row["updated_by"] == "worker-A"


def test_4_worker_edits_own_key(client) -> None:
    from agent_mcp.tools.project_context_tools import update_project_context_tool_impl

    _admin_token(client)
    worker_a = _make_worker("worker-A")
    _run_tool(client, update_project_context_tool_impl, {"token": worker_a, "context_key": "bar", "context_value": "x"})
    r = _run_tool(client, update_project_context_tool_impl, {"token": worker_a, "context_key": "bar", "context_value": "y"})
    assert "successfully" in r[0].text.lower(), r[0].text
    row = _row("bar")
    assert json.loads(row["value"]) == "y"
    assert row["created_by"] == "worker-A"


def test_5_worker_b_cannot_edit_worker_a_key(client) -> None:
    from agent_mcp.tools.project_context_tools import update_project_context_tool_impl

    _admin_token(client)
    worker_a = _make_worker("worker-A")
    worker_b = _make_worker("worker-B")
    _run_tool(client, update_project_context_tool_impl, {"token": worker_a, "context_key": "bar", "context_value": "x"})
    r = _run_tool(client, update_project_context_tool_impl, {"token": worker_b, "context_key": "bar", "context_value": "hacked"})
    msg = r[0].text
    assert "Unauthorized" in msg, msg
    assert "created by 'worker-A'" in msg, msg
    assert "only its creator or admin" in msg, msg
    row = _row("bar")
    assert json.loads(row["value"]) == "x", "row must be unchanged after rejection"


def test_6_worker_cannot_create_config_key(client) -> None:
    from agent_mcp.tools.project_context_tools import update_project_context_tool_impl

    _admin_token(client)
    worker_a = _make_worker("worker-A")
    r = _run_tool(client, update_project_context_tool_impl, {"token": worker_a, "context_key": "config_foo", "context_value": "v"})
    msg = r[0].text
    assert "Unauthorized" in msg, msg
    assert "config_* keys are admin-only" in msg, msg
    assert _row("config_foo") is None


def test_7_admin_can_create_config_key(client) -> None:
    from agent_mcp.tools.project_context_tools import update_project_context_tool_impl

    admin = _admin_token(client)
    r = _run_tool(client, update_project_context_tool_impl, {"token": admin, "context_key": "config_foo", "context_value": "v"})
    assert "successfully" in r[0].text.lower(), r[0].text
    row = _row("config_foo")
    assert row is not None
    assert row["created_by"] == "admin"


def test_8_worker_cannot_edit_admin_owned_config_key(client) -> None:
    from agent_mcp.tools.project_context_tools import update_project_context_tool_impl

    admin = _admin_token(client)
    worker_a = _make_worker("worker-A")
    _run_tool(client, update_project_context_tool_impl, {"token": admin, "context_key": "config_foo", "context_value": "v"})
    r = _run_tool(client, update_project_context_tool_impl, {"token": worker_a, "context_key": "config_foo", "context_value": "hacked"})
    msg = r[0].text
    assert "Unauthorized" in msg, msg
    assert "config_* keys are admin-only" in msg, msg


def test_9_worker_deletes_own_key(client) -> None:
    from agent_mcp.tools.project_context_tools import (
        delete_project_context_tool_impl,
        update_project_context_tool_impl,
    )

    _admin_token(client)
    worker_a = _make_worker("worker-A")
    _run_tool(client, update_project_context_tool_impl, {"token": worker_a, "context_key": "bar", "context_value": "x"})
    r = _run_tool(client, delete_project_context_tool_impl, {"token": worker_a, "context_key": "bar"})
    text = r[0].text
    assert "Unauthorized" not in text, text
    assert "deleted" in text.lower() or "Deleted" in text, text
    assert _row("bar") is None


def test_10_worker_b_cannot_delete_worker_a_key(client) -> None:
    from agent_mcp.tools.project_context_tools import (
        delete_project_context_tool_impl,
        update_project_context_tool_impl,
    )

    _admin_token(client)
    worker_a = _make_worker("worker-A")
    worker_b = _make_worker("worker-B")
    _run_tool(client, update_project_context_tool_impl, {"token": worker_a, "context_key": "bar", "context_value": "x"})
    r = _run_tool(client, delete_project_context_tool_impl, {"token": worker_b, "context_key": "bar"})
    msg = r[0].text
    assert "Unauthorized" in msg, msg
    assert "created by 'worker-A'" in msg, msg
    assert _row("bar") is not None


def test_11_admin_can_delete_any_key(client) -> None:
    from agent_mcp.tools.project_context_tools import (
        delete_project_context_tool_impl,
        update_project_context_tool_impl,
    )

    admin = _admin_token(client)
    worker_a = _make_worker("worker-A")
    _run_tool(client, update_project_context_tool_impl, {"token": worker_a, "context_key": "bar", "context_value": "x"})
    r = _run_tool(client, delete_project_context_tool_impl, {"token": admin, "context_key": "bar"})
    text = r[0].text
    assert "Unauthorized" not in text, text
    assert _row("bar") is None


def test_12_bulk_atomic_reject_on_unauthorized_entry(client) -> None:
    from agent_mcp.tools.project_context_tools import (
        bulk_update_project_context_tool_impl,
        update_project_context_tool_impl,
    )

    _admin_token(client)
    worker_a = _make_worker("worker-A")
    worker_b = _make_worker("worker-B")

    # B creates 'second'
    _run_tool(client, update_project_context_tool_impl, {"token": worker_b, "context_key": "second", "context_value": "B-val"})
    # A submits a bulk update including a key (`second`) it doesn't own
    r = _run_tool(
        client,
        bulk_update_project_context_tool_impl,
        {
            "token": worker_a,
            "updates": [
                {"context_key": "first", "context_value": "A-val"},
                {"context_key": "second", "context_value": "A-overwrite"},
            ],
        },
    )
    msg = r[0].text
    assert "Unauthorized" in msg, msg
    # Atomicity: neither row should have been mutated
    assert _row("first") is None, "atomic reject must not insert 'first'"
    second = _row("second")
    assert second is not None
    assert json.loads(second["value"]) == "B-val", (
        "atomic reject must not overwrite 'second'"
    )
    assert second["updated_by"] == "worker-B"


def test_13_bulk_all_authorized_succeeds(client) -> None:
    from agent_mcp.tools.project_context_tools import (
        bulk_update_project_context_tool_impl,
    )

    _admin_token(client)
    worker_a = _make_worker("worker-A")
    r = _run_tool(
        client,
        bulk_update_project_context_tool_impl,
        {
            "token": worker_a,
            "updates": [
                {"context_key": "k1", "context_value": "v1"},
                {"context_key": "k2", "context_value": "v2"},
            ],
        },
    )
    text = r[0].text
    assert "Unauthorized" not in text, text
    k1 = _row("k1")
    k2 = _row("k2")
    assert k1 is not None and k1["created_by"] == "worker-A"
    assert k2 is not None and k2["created_by"] == "worker-A"


def test_14_validate_consistency_callable_by_worker(client) -> None:
    from agent_mcp.tools.project_context_tools import (
        validate_context_consistency_tool_impl,
    )

    _admin_token(client)
    worker_a = _make_worker("worker-A")
    r = _run_tool(client, validate_context_consistency_tool_impl, {"token": worker_a})
    assert "Unauthorized" not in r[0].text, r[0].text


def test_15_backup_admin_only(client) -> None:
    from agent_mcp.tools.project_context_tools import backup_project_context_tool_impl

    _admin_token(client)
    worker_a = _make_worker("worker-A")
    r = _run_tool(client, backup_project_context_tool_impl, {"token": worker_a})
    assert "Unauthorized" in r[0].text, r[0].text


def test_16_legacy_db_migration_backfills_created_columns(
    project_dir, reset_globals
) -> None:
    """Mimic an existing DB on the OLD (pre-7b) schema: lifespan startup
    must add the new columns and backfill created_at/created_by from
    the legacy updated_at/updated_by data."""
    # Build a legacy-shaped DB at the path the lifespan startup will see.
    agent_dir = project_dir / ".agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    db_path = agent_dir / "mcp_state.db"

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE project_context (
                context_key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                last_updated TEXT NOT NULL,
                updated_by TEXT NOT NULL,
                description TEXT
            )
            """
        )
        legacy_ts = "2024-01-01T12:00:00"
        conn.execute(
            "INSERT INTO project_context "
            "(context_key, value, last_updated, updated_by, description) "
            "VALUES (?, ?, ?, ?, ?)",
            ("legacy_key", '"legacy-value"', legacy_ts, "legacy-admin", "old row"),
        )
        conn.commit()
    finally:
        conn.close()

    # Now boot the app against this project_dir. Lifespan startup must
    # apply Alembic migrations, including the 7b ownership migration.
    from starlette.testclient import TestClient
    from agent_mcp.app.main_app import create_app

    app = create_app(project_dir=str(project_dir))
    with TestClient(app):
        # Inspect the upgraded schema + backfill.
        conn = sqlite3.connect(str(db_path))
        try:
            conn.row_factory = sqlite3.Row
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(project_context)")}
            assert {"created_at", "created_by", "updated_at"} <= cols
            assert "last_updated" not in cols

            row = conn.execute(
                "SELECT * FROM project_context WHERE context_key = 'legacy_key'"
            ).fetchone()
            assert row is not None
            assert row["created_by"] == "legacy-admin"
            assert row["created_at"] == legacy_ts
            assert row["updated_at"] == legacy_ts
            assert row["updated_by"] == "legacy-admin"
        finally:
            conn.close()
