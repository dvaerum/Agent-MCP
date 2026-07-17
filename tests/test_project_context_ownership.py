"""Per-entry creator-ownership rules for project_context writes/deletes.

Phase 7b — locked design (built on top of Phase 7a's SQLAlchemy + Alembic
infrastructure). Workers can create new entries (recording themselves as
the creator) and can edit/delete only entries they themselves created.
`config_*` keys remain admin-only regardless. Admins can do anything.

The bulk-update path must be atomic: a single unauthorized entry in the
batch rejects all in a single SQLAlchemy transaction.

Migrated to `tests/harness.py::mcp_session` (Candidate F from
architecture review 2026-06-02). The legacy file used
`client.portal.call(...)` to dispatch tool impls onto the lifespan
event loop because the impl could route through `execute_db_write`'s
per-loop queue. The project_context tools do NOT use the write queue,
so the harness's `admin.call` (running on pytest-asyncio's loop) is a
straight-through replacement.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


def _row(key: str) -> dict | None:
    """Read a project_context row as a plain dict (post-migration shape)."""
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM project_context WHERE context_key = ?", (key,)
    )
    r = cursor.fetchone()
    conn.close()
    return dict(r) if r else None


# === Schema migration shape test ===


async def test_migration_results_in_expected_schema(tmp_path) -> None:
    """Post-startup, project_context must carry the ownership columns
    (created_at, created_by) and the rename of last_updated -> updated_at."""
    from agent_mcp.db.connection import get_db_connection

    async with mcp_session(tmp_path):
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
        assert "last_updated" not in cols, (
            "rename of last_updated -> updated_at must have happened"
        )


async def test_orm_model_has_ownership_columns() -> None:
    """SQLAlchemy ProjectContext model must expose the ownership columns.

    Pure import test — no lifespan needed. Declared async to match the
    module-level `pytest.mark.asyncio`.
    """
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


async def test_1_admin_creates_key(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = await admin.call(
            "update_project_context",
            {"context_key": "foo", "context_value": "v1"},
        )
        assert "successfully" in r[0].text.lower(), r[0].text
        row = _row("foo")
        assert row is not None
        assert row["created_by"] == "admin"
        assert row["updated_by"] == "admin"
        assert row["created_at"]
        assert row["updated_at"]


async def test_2_admin_edits_own_key_preserves_creator(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.call(
            "update_project_context",
            {"context_key": "foo", "context_value": "v1"},
        )
        row = _row("foo")
        created_at_before = row["created_at"]

        await admin.call(
            "update_project_context",
            {"context_key": "foo", "context_value": "v2"},
        )
        row = _row("foo")
        assert row["updated_by"] == "admin"
        assert row["created_by"] == "admin"
        assert row["created_at"] == created_at_before, (
            "created_at must be preserved across edits"
        )


async def test_3_worker_creates_nonconfig_key(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        worker_a = await admin.create_worker("worker-A")
        r = await worker_a.call(
            "update_project_context",
            {"context_key": "bar", "context_value": "x"},
        )
        assert "successfully" in r[0].text.lower(), r[0].text
        row = _row("bar")
        assert row is not None
        assert row["created_by"] == "worker-A"
        assert row["updated_by"] == "worker-A"


async def test_4_worker_edits_own_key(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        worker_a = await admin.create_worker("worker-A")
        await worker_a.call(
            "update_project_context",
            {"context_key": "bar", "context_value": "x"},
        )
        r = await worker_a.call(
            "update_project_context",
            {"context_key": "bar", "context_value": "y"},
        )
        assert "successfully" in r[0].text.lower(), r[0].text
        row = _row("bar")
        assert json.loads(row["value"]) == "y"
        assert row["created_by"] == "worker-A"


async def test_5_worker_b_cannot_edit_worker_a_key(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        worker_a = await admin.create_worker("worker-A")
        worker_b = await admin.create_worker("worker-B")
        await worker_a.call(
            "update_project_context",
            {"context_key": "bar", "context_value": "x"},
        )
        r = await worker_b.call(
            "update_project_context",
            {"context_key": "bar", "context_value": "hacked"},
        )
        msg = r[0].text
        assert "Unauthorized" in msg, msg
        assert "created by 'worker-A'" in msg, msg
        assert "only its creator or admin" in msg, msg
        row = _row("bar")
        assert json.loads(row["value"]) == "x", (
            "row must be unchanged after rejection"
        )


async def test_6_worker_cannot_create_config_key(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        worker_a = await admin.create_worker("worker-A")
        r = await worker_a.call(
            "update_project_context",
            {"context_key": "config_foo", "context_value": "v"},
        )
        msg = r[0].text
        # Worker-message clarity: config_* rejection is Invalid, NOT the
        # Unauthorized-framed PermissionDenied.
        assert "Unauthorized" not in msg, msg
        assert "project settings store" in msg, msg
        assert _row("config_foo") is None


async def test_7_admin_cannot_create_config_key_either(tmp_path) -> None:
    """Wave 11 (ADR-0016): config_* is rejected on the knowledge write
    path for EVERYONE — admin included (the settings store owns the
    namespace)."""
    async with mcp_session(tmp_path) as admin:
        r = await admin.call(
            "update_project_context",
            {"context_key": "config_foo", "context_value": "v"},
        )
        msg = r[0].text
        assert "Unauthorized" not in msg, msg
        assert "project settings store" in msg, msg
        assert _row("config_foo") is None


async def test_8_worker_cannot_write_config_key_owned_or_not(tmp_path) -> None:
    """The rejection is unconditional — ownership doesn't matter for
    config_* on the knowledge path (ADR-0016)."""
    async with mcp_session(tmp_path) as admin:
        worker_a = await admin.create_worker("worker-A")
        r = await worker_a.call(
            "update_project_context",
            {"context_key": "config_foo", "context_value": "hacked"},
        )
        msg = r[0].text
        assert "Unauthorized" not in msg, msg
        assert "project settings store" in msg, msg


async def test_9_worker_deletes_own_key(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        worker_a = await admin.create_worker("worker-A")
        await worker_a.call(
            "update_project_context",
            {"context_key": "bar", "context_value": "x"},
        )
        r = await worker_a.call(
            "delete_project_context", {"context_key": "bar"},
        )
        text = r[0].text
        assert "Unauthorized" not in text, text
        assert "deleted" in text.lower() or "Deleted" in text, text
        assert _row("bar") is None


async def test_10_worker_b_cannot_delete_worker_a_key(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        worker_a = await admin.create_worker("worker-A")
        worker_b = await admin.create_worker("worker-B")
        await worker_a.call(
            "update_project_context",
            {"context_key": "bar", "context_value": "x"},
        )
        r = await worker_b.call(
            "delete_project_context", {"context_key": "bar"},
        )
        msg = r[0].text
        assert "Unauthorized" in msg, msg
        assert "created by 'worker-A'" in msg, msg
        assert _row("bar") is not None


async def test_11_admin_can_delete_any_key(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        worker_a = await admin.create_worker("worker-A")
        await worker_a.call(
            "update_project_context",
            {"context_key": "bar", "context_value": "x"},
        )
        r = await admin.call(
            "delete_project_context", {"context_key": "bar"},
        )
        text = r[0].text
        assert "Unauthorized" not in text, text
        assert _row("bar") is None


async def test_12_bulk_atomic_reject_on_unauthorized_entry(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        worker_a = await admin.create_worker("worker-A")
        worker_b = await admin.create_worker("worker-B")

        # B creates 'second'
        await worker_b.call(
            "update_project_context",
            {"context_key": "second", "context_value": "B-val"},
        )
        # A submits a bulk update including a key (`second`) it doesn't own
        r = await worker_a.call(
            "bulk_update_project_context",
            {
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


async def test_13_bulk_all_authorized_succeeds(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        worker_a = await admin.create_worker("worker-A")
        r = await worker_a.call(
            "bulk_update_project_context",
            {
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


async def test_14_validate_consistency_callable_by_worker(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        worker_a = await admin.create_worker("worker-A")
        r = await worker_a.call("validate_context_consistency", {})
        assert "Unauthorized" not in r[0].text, r[0].text


async def test_15_backup_admin_only(tmp_path) -> None:
    """Workers cannot call backup_project_context.

    Post auth-decorators (architecture review 2026-06-01 candidate A):
    @requires("admin") raises AuthRejected before the impl runs; the
    dispatcher translates that to isError=true over the wire. We assert
    on the wire response shape here (via the harness) — same surface a
    real worker would see, more robust than asserting on the Python
    exception type.
    """
    async with mcp_session(tmp_path) as admin:
        worker_a = await admin.create_worker("worker-A")
        await worker_a.assert_unauthorized("backup_project_context", {})


async def test_16_legacy_db_migration_backfills_created_columns(
    tmp_path,
) -> None:
    """Mimic an existing DB on the OLD (pre-7b) schema: lifespan startup
    must add the new columns and backfill created_at/created_by from
    the legacy updated_at/updated_by data.

    Cannot use mcp_session() here — we need to pre-seed a legacy-shaped
    DB BEFORE lifespan startup applies migrations. Build the DB by hand
    at the path create_app() will scan, then enter the harness so it
    runs the upgrade against it.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()
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
            ("legacy_key", '"legacy-value"', legacy_ts, "legacy-admin",
             "old row"),
        )
        conn.commit()
    finally:
        conn.close()

    # mcp_session(tmp_path) appends "/project" itself; we pre-built the
    # exact dir it will use. Lifespan startup runs Alembic and migrates.
    async with mcp_session(tmp_path):
        # Inspect the upgraded schema + backfill.
        conn = sqlite3.connect(str(db_path))
        try:
            conn.row_factory = sqlite3.Row
            cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(project_context)")
            }
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
