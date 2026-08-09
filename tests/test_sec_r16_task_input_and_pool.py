"""R16-F1 + R16-F2 — task-input normalization and the claimable pool.

Two findings, one PR (they share the task create/read files).

R16-F1 (LOW) — empty-string / whitespace ``parent_task`` must collapse
to ``None`` at the earliest point on EVERY create path, so it can never
reach the self-FK INSERT and surface as a generic 500. An empty parent
is semantically "no parent": it exercises the single-root guard + the
partial UNIQUE index (a second root → clean 409/Conflict) and a
genuinely nonexistent parent → clean 404/NotFound — never a 500.

Swept siblings (all in ``task_tools.py``):

* ``create_task_tool_impl``  — REST ``POST /api/tasks`` + MCP ``create_task``
* ``assign_task_tool_impl``  — MCP ``assign_task`` (admin single-create)
* ``create_self_task_tool_impl`` — the PRIVILEGED (``tasks.assign``)
  branch (the worker path is already covered by the AZ-R19-1 gate)

R16-F2 (LOW-MED) — the read/discovery "claimable/unassigned pool" must
apply the SAME terminal-status sink the write side (``_assign_to_existing_tasks``)
enforces, so it can never advertise finished work nobody can claim. One
canonical predicate (``assigned_to IS NULL/"" AND status NOT IN
TERMINAL``) at all three read surfaces:

* REST ``GET /api/tasks?unassigned=true`` (router ``_keep``)
* ``TaskQueryEngine`` (``filters.unassigned`` + the worker self-claim
  ``include_unassigned`` pool)
* the wake seam ``_collect_unassigned_task_events_for``

Plus the ``update_task`` ordering gap: a combined
``{status:"completed", assigned_to:null}`` must NOT fire the
unassigned-fanout on the now-terminal task.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


# --- shared helpers ---------------------------------------------------


def _seed_task(
    task_id: str,
    *,
    assigned_to: str | None = None,
    created_by: str = "admin",
    status: str = "pending",
) -> None:
    """Insert a task row directly, chaining under the single root.

    First parentless seed becomes the sole root; every later seed parents
    under it (R15-BL-1 partial-unique-index compliant).
    """
    from agent_mcp.db.connection import get_db_connection
    from tests.conftest import existing_root_task_id

    parent = existing_root_task_id()
    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, title, description, assigned_to, "
            "created_by, status, priority, created_at, updated_at, "
            "parent_task) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, task_id, "seeded", assigned_to, created_by, status,
             "medium", now, now, parent),
        )
        conn.commit()
    finally:
        conn.close()


def _parent_empty_rows() -> int:
    """Count task rows whose ``parent_task`` is the empty string — the
    FK-violating artefact an un-normalized empty parent would try to
    write (and which the DB rejects with a 500-surfacing IntegrityError)."""
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE parent_task = ''"
        ).fetchone()
        return row["n"]
    finally:
        conn.close()


def _root_count() -> int:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE parent_task IS NULL"
        ).fetchone()["n"]
    finally:
        conn.close()


def _text(result) -> str:
    parts = []
    for block in result or []:
        t = getattr(block, "text", None)
        if isinstance(t, str):
            parts.append(t)
    return "\n".join(parts)


def _assert_clean_conflict_not_generic_failure(text: str) -> None:
    """The MCP wire renders a clean ``Conflict`` as ``"Error: conflict:
    …"`` and a generic ``Failed`` (the pre-fix FK-IntegrityError path) as
    ``"Error: Operation failed"``. A normalized empty parent must produce
    the former, never the latter."""
    assert "Operation failed" not in text, (
        f"empty parent surfaced a generic failure (500-equivalent): {text}"
    )
    assert "conflict" in text.lower(), (
        f"expected a clean single-root conflict, got: {text}"
    )


# ======================================================================
# R16-F1 — empty/whitespace parent must not 500
# ======================================================================


async def test_rest_create_empty_parent_first_root_ok(tmp_path) -> None:
    """REST: empty ``parent_task`` with NO existing root collapses to
    None → the FIRST root is created (200), never a 500."""
    async with mcp_session(tmp_path) as admin:
        r = admin.post(
            "/api/tasks", json={"task_title": "x", "parent_task": ""}
        )
        assert r.status_code == 200, r.text
        assert r.json().get("success") is True
        assert _root_count() == 1
        assert _parent_empty_rows() == 0


async def test_rest_create_empty_parent_second_root_conflict(tmp_path) -> None:
    """REST live repro: with a root present, empty ``parent_task`` →
    clean 409 (single-root conflict), NOT the pre-fix 500."""
    async with mcp_session(tmp_path) as admin:
        r1 = admin.post("/api/tasks", json={"task_title": "root"})
        assert r1.status_code == 200, r1.text

        r2 = admin.post(
            "/api/tasks", json={"task_title": "x", "parent_task": ""}
        )
        assert r2.status_code == 409, (
            f"empty parent must be a clean 409, got {r2.status_code}: {r2.text}"
        )
        assert "root task already exists" in r2.json().get("error", "")
        assert _parent_empty_rows() == 0
        assert _root_count() == 1


async def test_rest_create_whitespace_parent_conflict(tmp_path) -> None:
    """REST: a whitespace-only ``parent_task`` normalizes exactly like
    empty (collapses to None)."""
    async with mcp_session(tmp_path) as admin:
        admin.post("/api/tasks", json={"task_title": "root"})
        r = admin.post(
            "/api/tasks", json={"task_title": "x", "parent_task": "   "}
        )
        assert r.status_code == 409, r.text
        assert _parent_empty_rows() == 0


async def test_rest_create_nonexistent_parent_404(tmp_path) -> None:
    """REST: a well-formed but NONEXISTENT parent → clean 404, not 500."""
    async with mcp_session(tmp_path) as admin:
        admin.post("/api/tasks", json={"task_title": "root"})
        r = admin.post(
            "/api/tasks",
            json={"task_title": "x", "parent_task": "task_does_not_exist"},
        )
        assert r.status_code == 404, r.text


async def test_mcp_create_task_empty_parent_no_500(tmp_path) -> None:
    """MCP ``create_task``: empty parent with a root present → clean
    Conflict (isError), never a generic 500 marker, no ``parent=''`` row."""
    async with mcp_session(tmp_path) as admin:
        await admin.call("create_task", {"task_title": "root"})
        result = await admin.call(
            "create_task", {"task_title": "x", "parent_task": ""}
        )
        assert admin._last_is_error is True
        _assert_clean_conflict_not_generic_failure(_text(result))
        assert _parent_empty_rows() == 0
        assert _root_count() == 1


async def test_mcp_assign_task_empty_parent_no_500(tmp_path) -> None:
    """MCP ``assign_task`` (admin single-create): empty ``parent_task_id``
    with a root present must NOT 500 and must NOT write a ``parent=''`` row."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.call("create_task", {"task_title": "root"})

        result = await admin.call(
            "assign_task",
            {
                "agent_id": "alice",
                "task_title": "child via assign",
                "task_description": "d",
                "parent_task_id": "",
                "auto_suggest_parent": False,
            },
        )
        _assert_clean_conflict_not_generic_failure(_text(result))
        assert _parent_empty_rows() == 0


async def test_mcp_create_self_task_privileged_empty_parent_no_500(
    tmp_path,
) -> None:
    """MCP ``create_self_task`` PRIVILEGED (tasks.assign) branch: empty
    ``parent_task_id`` with a root present must NOT 500 nor write a
    ``parent=''`` row (the privileged branch skips the AZ-R19-1 gate that
    already protects the worker path)."""
    async with mcp_session(tmp_path) as admin:
        await admin.call("create_task", {"task_title": "root"})
        result = await admin.call(
            "create_self_task",
            {
                "task_title": "self",
                "task_description": "d",
                "parent_task_id": "",
            },
        )
        _assert_clean_conflict_not_generic_failure(_text(result))
        assert _parent_empty_rows() == 0
        assert _root_count() == 1


async def test_rest_happy_path_child_still_works(tmp_path) -> None:
    """Regression guard: a real (non-empty) parent still creates a child."""
    async with mcp_session(tmp_path) as admin:
        r1 = admin.post("/api/tasks", json={"task_title": "root"})
        root_id = r1.json()["task_id"]
        r2 = admin.post(
            "/api/tasks",
            json={"task_title": "child", "parent_task": root_id},
        )
        assert r2.status_code == 200, r2.text
        assert r2.json().get("success") is True


# ======================================================================
# R16-F2 — claimable pool must exclude TERMINAL tasks
# ======================================================================

_TERMINAL = ("completed", "cancelled", "failed")


async def test_rest_unassigned_excludes_terminal(tmp_path) -> None:
    """REST ``?unassigned=true`` must exclude unassigned-but-TERMINAL
    tasks (the write side refuses to (re)claim them)."""
    async with mcp_session(tmp_path) as admin:
        _seed_task("t_open", assigned_to=None, status="pending")
        for st in _TERMINAL:
            _seed_task(f"t_{st}", assigned_to=None, status=st)

        r = admin.client.get("/api/tasks?unassigned=true")
        assert r.status_code == 200, r.text
        ids = sorted(row["task_id"] for row in r.json())
        assert ids == ["t_open"], ids


async def test_engine_unassigned_excludes_terminal() -> None:
    """``TaskQueryEngine`` ``filters.unassigned`` excludes terminal rows."""
    from agent_mcp.features.task_queries import TaskFilterSpec, TaskQueryEngine

    def _row(tid, status, assignee=None):
        return {
            "task_id": tid, "title": tid, "status": status,
            "priority": "medium", "assigned_to": assignee,
            "created_by": "admin", "created_at": "2025-01-01T00:00:00",
            "updated_at": "2025-01-01T00:00:00", "parent_task": None,
            "child_tasks": [], "depends_on_tasks": [], "notes": [],
        }

    snap = {
        "open": _row("open", "pending"),
        "done": _row("done", "completed"),
        "cx": _row("cx", "cancelled"),
        "fail": _row("fail", "failed"),
        "assigned": _row("assigned", "pending", "alice"),
    }
    engine = TaskQueryEngine(task_source=lambda: snap)
    result = engine.query(filters=TaskFilterSpec(unassigned=True))
    ids = {t["task_id"] for t in result.tasks}
    assert ids == {"open"}, ids


async def test_engine_worker_pool_excludes_terminal_but_keeps_own() -> None:
    """The worker self-claim pool (``include_unassigned``) drops TERMINAL
    unassigned rows but keeps the worker's OWN terminal tasks visible."""
    from agent_mcp.features.task_queries import TaskFilterSpec, TaskQueryEngine

    def _row(tid, status, assignee=None):
        return {
            "task_id": tid, "title": tid, "status": status,
            "priority": "medium", "assigned_to": assignee,
            "created_by": "admin", "created_at": "2025-01-01T00:00:00",
            "updated_at": "2025-01-01T00:00:00", "parent_task": None,
            "child_tasks": [], "depends_on_tasks": [], "notes": [],
        }

    snap = {
        "pool_open": _row("pool_open", "pending"),
        "pool_done": _row("pool_done", "completed"),
        "mine_open": _row("mine_open", "in_progress", "alice"),
        "mine_done": _row("mine_done", "completed", "alice"),
        "foreign": _row("foreign", "pending", "bob"),
    }
    engine = TaskQueryEngine(task_source=lambda: snap)
    result = engine.query(
        filters=TaskFilterSpec(agent_id="alice", include_unassigned=True)
    )
    ids = {t["task_id"] for t in result.tasks}
    # own tasks (terminal or not) + the NON-terminal claimable pool;
    # NOT the terminal pool row, NOT the foreign row.
    assert ids == {"pool_open", "mine_open", "mine_done"}, ids


async def test_wake_seam_excludes_terminal(tmp_path) -> None:
    """The wake seam ``_collect_unassigned_task_events_for`` must not emit
    ``unassigned_task_appeared`` for a TERMINAL unassigned task."""
    async with mcp_session(tmp_path):
        from tests.harness import seed_agent_rows

        seed_agent_rows("waker")
        _seed_task("t_open", assigned_to=None, status="pending")
        _seed_task("t_done", assigned_to=None, status="completed")

        from agent_mcp.tools.agent_communication_tools import (
            _collect_unassigned_task_events_for,
        )

        events = _collect_unassigned_task_events_for("waker", None)
        refs = {e["ref_id"] for e in events}
        assert "t_open" in refs
        assert "t_done" not in refs, (
            f"wake seam advertised a terminal task: {refs}"
        )


async def test_update_complete_and_unassign_no_fanout(
    tmp_path, monkeypatch
) -> None:
    """``update_task`` ordering gap: a combined
    ``{status:"completed", assigned_to:null}`` must NOT fire the
    unassigned-fanout — the task is terminal, not claimable."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = admin.post(
            "/api/tasks", json={"task_title": "root", "assigned_to": "alice"}
        )
        tid = r.json()["task_id"]

        fired: list[str] = []
        import agent_mcp.core.globals as _g

        monkeypatch.setattr(
            _g, "notify_unassigned_task_appeared",
            lambda task_id, *a, **k: fired.append(task_id),
        )

        await admin.assert_tool_succeeds(
            "update_task",
            {"task_id": tid, "status": "completed", "assigned_to": ""},
        )
        assert tid not in fired, (
            "complete+unassign fired a spurious unassigned-task fanout"
        )


async def test_update_plain_unassign_still_fans_out(
    tmp_path, monkeypatch
) -> None:
    """Control: a plain unassign of a NON-terminal task still fires the
    fanout (the fix must not silence legitimate claimable transitions)."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = admin.post(
            "/api/tasks", json={"task_title": "root", "assigned_to": "alice"}
        )
        tid = r.json()["task_id"]

        fired: list[str] = []
        import agent_mcp.core.globals as _g

        monkeypatch.setattr(
            _g, "notify_unassigned_task_appeared",
            lambda task_id, *a, **k: fired.append(task_id),
        )

        await admin.assert_tool_succeeds(
            "update_task", {"task_id": tid, "assigned_to": ""}
        )
        assert tid in fired, "plain unassign must still fan out as claimable"


# ======================================================================
# Class-sweep: OverflowError on int(<request-value>) guards
# ======================================================================


def test_int_overflow_is_the_swept_exception() -> None:
    """``int(float('inf'))`` raises ``OverflowError`` — NOT
    ``ValueError``/``TypeError`` — so a guard catching only the latter two
    would still 500 on a ``{"n": 1e400}`` body (JSON parses ``1e400`` to
    ``inf``). Documents WHY ``OverflowError`` was added to the numeric
    guards in ``agent_communication_tools``."""
    with pytest.raises(OverflowError):
        int(float("inf"))
    assert not isinstance(OverflowError(), (ValueError, TypeError))


def test_read_default_timeout_guard_includes_overflowerror() -> None:
    """The three swept guards now list ``OverflowError``. Verify against
    the real source so the class-sweep can't silently regress."""
    import inspect

    import agent_mcp.tools.agent_communication_tools as act

    src = inspect.getsource(act)
    # All three ``except`` sites that wrap an ``int(<request-value>)`` now
    # include OverflowError (the two request-value guards + the env guard
    # swept for consistency).
    assert src.count("except (ValueError, TypeError, OverflowError)") >= 1
    assert src.count("except (TypeError, ValueError, OverflowError)") >= 2
