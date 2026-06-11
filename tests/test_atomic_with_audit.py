"""Contract tests for ``agent_mcp.db.atomic.atomic_with_audit``.

The context manager collapses the "open conn → cursor → write → log audit
→ commit → close" boilerplate that repeats across ``agent_mcp/tools/`` and
names the invariant — *every successful write block produces exactly one
audit row* — in code. Today that invariant is convention; a missed
``log_agent_action_to_db`` call silently produces a write without an
audit row. The context manager makes the audit row a *required parameter
of the seam itself* (operation name is keyword-only and mandatory), so
forgetting it becomes a TypeError at the call site, not a quiet
production bug.

These tests pin the contract:

* Happy path — cursor yielded, write commits, exactly one audit row.
* Exception path — write raises inside the block, transaction rolls
  back, no audit row, exception re-raised.
* Multi-write atomicity — several writes in one block, exactly one
  audit row at the end, all committed together.
* Operation name required — calling without ``operation=`` is a
  ``TypeError`` (compile-time-ish absence; the seam refuses to open).
* Actor optional — ``actor=None`` is allowed (admin-less audit rows
  log with NULL agent_id).
* Details optional — omitted ``details`` stores NULL/empty, not a
  serialization crash.
* Task ID optional — ``task_id`` propagates to the audit row when
  passed; absent otherwise.
* Connection lifecycle — the underlying connection is closed when the
  block exits, both on success and on exception (no leaks).
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from agent_mcp.app.main_app import create_app


def _make_client(project_dir):
    app = create_app(project_dir=str(project_dir))
    return TestClient(app)


def _count_actions(action_type: str) -> int:
    """Return the number of agent_actions rows of the given type."""
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM agent_actions WHERE action_type = ?",
            (action_type,),
        )
        return cur.fetchone()[0]
    finally:
        conn.close()


def _fetch_action(action_type: str) -> dict | None:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT agent_id, action_type, task_id, details "
            "FROM agent_actions WHERE action_type = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (action_type,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# --- Happy path ---------------------------------------------------------


def test_yields_cursor_writes_commit_and_audit_row_appended(
    project_dir, reset_globals,
):
    """One write inside the block → one audit row, committed."""
    with _make_client(project_dir):
        from agent_mcp.db.atomic import atomic_with_audit

        # Use an existing table the schema guarantees: project_context.
        # The block performs one write and we expect an audit row for
        # the operation name we pass.
        with atomic_with_audit(
            operation="test.happy_path",
            actor="agent-1",
            details={"key": "value"},
        ) as cursor:
            cursor.execute(
                "INSERT OR REPLACE INTO project_context "
                "(context_key, value, last_updated, updated_by, description) "
                "VALUES (?, ?, datetime('now'), ?, ?)",
                ("atomic_test_key", '"v"', "agent-1", "test"),
            )

        # The audit row must be present after the block exits.
        assert _count_actions("test.happy_path") == 1
        row = _fetch_action("test.happy_path")
        assert row is not None
        assert row["agent_id"] == "agent-1"
        # The write must be visible from a fresh connection — proves
        # the commit happened, not just a buffered insert.
        from agent_mcp.db.connection import get_db_connection
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT value FROM project_context WHERE context_key = ?",
                ("atomic_test_key",),
            )
            assert cur.fetchone() is not None
        finally:
            conn.close()


# --- Exception path -----------------------------------------------------


def test_exception_in_block_rolls_back_and_no_audit_row(
    project_dir, reset_globals,
):
    """Write raises → no commit, no audit row, exception propagates."""
    with _make_client(project_dir):
        from agent_mcp.db.atomic import atomic_with_audit

        before = _count_actions("test.exception_path")

        class _Boom(RuntimeError):
            pass

        with pytest.raises(_Boom):
            with atomic_with_audit(
                operation="test.exception_path",
                actor="agent-2",
                details={"will": "not_persist"},
            ) as cursor:
                cursor.execute(
                    "INSERT OR REPLACE INTO project_context "
                    "(context_key, value, last_updated, updated_by, description) "
                    "VALUES (?, ?, datetime('now'), ?, ?)",
                    ("rollback_test_key", '"v"', "agent-2", "test"),
                )
                raise _Boom("simulated failure inside block")

        # Audit row count for this operation must be unchanged.
        assert _count_actions("test.exception_path") == before
        # The attempted write must have rolled back.
        from agent_mcp.db.connection import get_db_connection
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT value FROM project_context WHERE context_key = ?",
                ("rollback_test_key",),
            )
            assert cur.fetchone() is None, (
                "Pre-exception write must roll back when the block raises"
            )
        finally:
            conn.close()


# --- Multi-write atomicity ----------------------------------------------


def test_multiple_writes_in_one_block_emit_one_audit_row(
    project_dir, reset_globals,
):
    """N writes + 1 audit row + 1 commit — the named invariant."""
    with _make_client(project_dir):
        from agent_mcp.db.atomic import atomic_with_audit

        before = _count_actions("test.multi_write")

        with atomic_with_audit(
            operation="test.multi_write",
            actor="agent-3",
            details={"writes": 3},
        ) as cursor:
            for i in range(3):
                cursor.execute(
                    "INSERT OR REPLACE INTO project_context "
                    "(context_key, value, last_updated, updated_by, description) "
                    "VALUES (?, ?, datetime('now'), ?, ?)",
                    (f"multi_key_{i}", '"v"', "agent-3", "test"),
                )

        # One audit row, not three.
        assert _count_actions("test.multi_write") == before + 1
        # All three writes visible.
        from agent_mcp.db.connection import get_db_connection
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM project_context "
                "WHERE context_key LIKE 'multi_key_%'"
            )
            assert cur.fetchone()[0] == 3
        finally:
            conn.close()


# --- Operation name required --------------------------------------------


def test_operation_name_is_required_keyword(project_dir, reset_globals):
    """Calling without ``operation=`` is a TypeError.

    The whole point of the seam: the audit-log identity is named on
    the call site, not buried inside a function body. If a refactor
    accidentally drops the audit, the seam refuses to open.
    """
    with _make_client(project_dir):
        from agent_mcp.db.atomic import atomic_with_audit

        with pytest.raises(TypeError):
            # Missing required keyword arg ``operation``.
            with atomic_with_audit(actor="agent-x") as _cursor:  # type: ignore[call-arg]
                pass


# --- Actor optional -----------------------------------------------------


def test_actor_optional_defaults_to_null_agent_id(
    project_dir, reset_globals,
):
    """``actor=None`` is allowed — admin-less audit rows store NULL."""
    with _make_client(project_dir):
        from agent_mcp.db.atomic import atomic_with_audit

        with atomic_with_audit(operation="test.no_actor") as cursor:
            cursor.execute(
                "INSERT OR REPLACE INTO project_context "
                "(context_key, value, last_updated, updated_by, description) "
                "VALUES (?, ?, datetime('now'), ?, ?)",
                ("no_actor_key", '"v"', "system", "test"),
            )

        row = _fetch_action("test.no_actor")
        assert row is not None
        # SQLite stores NULL for the omitted agent_id.
        assert row["agent_id"] is None


# --- Details optional ---------------------------------------------------


def test_details_optional_defaults_to_empty(project_dir, reset_globals):
    """Omitting ``details`` does not crash and stores NULL or '{}'."""
    with _make_client(project_dir):
        from agent_mcp.db.atomic import atomic_with_audit

        with atomic_with_audit(
            operation="test.no_details",
            actor="agent-4",
        ) as cursor:
            cursor.execute(
                "INSERT OR REPLACE INTO project_context "
                "(context_key, value, last_updated, updated_by, description) "
                "VALUES (?, ?, datetime('now'), ?, ?)",
                ("no_details_key", '"v"', "agent-4", "test"),
            )

        row = _fetch_action("test.no_details")
        assert row is not None
        # Either NULL or empty-dict JSON is acceptable — both communicate
        # "no extra context". The contract is "doesn't crash".
        assert row["details"] in (None, "{}", "null")


# --- Task ID propagation ------------------------------------------------


def test_task_id_propagates_to_audit_row_when_passed(
    project_dir, reset_globals,
):
    """``task_id`` is a first-class audit-row field — pass-through works."""
    with _make_client(project_dir):
        from agent_mcp.db.atomic import atomic_with_audit

        with atomic_with_audit(
            operation="test.task_id_passthrough",
            actor="agent-5",
            task_id="task-xyz",
            details={"note": "hi"},
        ) as cursor:
            cursor.execute(
                "INSERT OR REPLACE INTO project_context "
                "(context_key, value, last_updated, updated_by, description) "
                "VALUES (?, ?, datetime('now'), ?, ?)",
                ("task_id_key", '"v"', "agent-5", "test"),
            )

        row = _fetch_action("test.task_id_passthrough")
        assert row is not None
        assert row["task_id"] == "task-xyz"


# --- Connection lifecycle -----------------------------------------------


def test_connection_closed_on_success(project_dir, reset_globals):
    """Underlying connection closes on success — no leak."""
    with _make_client(project_dir):
        from agent_mcp.db import atomic as atomic_mod

        opened: list = []
        original = atomic_mod.get_db_connection

        def _tracking_get_db_connection():
            conn = original()
            opened.append(conn)
            return conn

        atomic_mod.get_db_connection = _tracking_get_db_connection
        try:
            with atomic_mod.atomic_with_audit(
                operation="test.lifecycle_success",
                actor="agent-6",
            ) as cursor:
                cursor.execute("SELECT 1")
        finally:
            atomic_mod.get_db_connection = original

        assert len(opened) == 1
        # A closed sqlite3 connection raises on .execute() — use that
        # as a portable "is closed?" probe.
        import sqlite3
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")


def test_connection_closed_on_exception(project_dir, reset_globals):
    """Underlying connection closes on exception — no leak."""
    with _make_client(project_dir):
        from agent_mcp.db import atomic as atomic_mod

        opened: list = []
        original = atomic_mod.get_db_connection

        def _tracking_get_db_connection():
            conn = original()
            opened.append(conn)
            return conn

        atomic_mod.get_db_connection = _tracking_get_db_connection
        try:
            with pytest.raises(RuntimeError):
                with atomic_mod.atomic_with_audit(
                    operation="test.lifecycle_exception",
                    actor="agent-7",
                ) as _cursor:
                    raise RuntimeError("boom")
        finally:
            atomic_mod.get_db_connection = original

        assert len(opened) == 1
        import sqlite3
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")
