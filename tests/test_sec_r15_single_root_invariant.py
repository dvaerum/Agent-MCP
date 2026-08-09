"""R15-BL-1: the single-root-task invariant must hold on EVERY create
path, plus a DB-level structural backstop.

Background
----------
Every project (one SQLite ``mcp_state.db`` per project) must have AT
MOST ONE root task — a task with ``parent_task IS NULL``. The
validator documents this as a "hard structural constraint"
(``features/task_placement/validator.py``) and the rest of the code
(hierarchy reads, metrics, the delete cascade) relies on it.

The pentest finding (R15-BL-1): ``create_task_tool_impl`` — the
canonical impl behind BOTH the REST ``POST /api/<slug>/tasks`` route
AND the MCP ``create_task`` tool — never ran the
``SELECT COUNT(*) ... WHERE parent_task IS NULL`` guard its siblings
(``assign_task``, ``create_self_task``) run. LIVE-confirmed by creating
a SERIAL THIRD root through it, all ``success:true``.

These tests pin:

* ``create_task`` rejects a second (and serial third) root — the
  guard is now PRESENT, not just racy;
* the happy paths (FIRST root, and a child with ``parent_task`` set)
  still succeed;
* both surfaces (MCP tool + REST route) reject the second root
  identically (one implementation, not two).
"""

from __future__ import annotations

import json as _json

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _root_count() -> int:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE parent_task IS NULL"
        )
        return cur.fetchone()["n"]
    finally:
        conn.close()


async def _create_task(admin, title: str, parent: str | None = None):
    """Call the create_task MCP tool; return (task_id_or_None, is_error)."""
    args: dict = {"task_title": title, "task_description": "desc"}
    if parent is not None:
        args["parent_task"] = parent
    result = await admin.call("create_task", args)
    is_error = getattr(admin, "_last_is_error", False)
    task_id = None
    if not is_error and result:
        try:
            task_id = _json.loads(result[-1].text)["task_id"]
        except (KeyError, IndexError, ValueError, AttributeError):
            task_id = None
    return task_id, is_error


async def test_create_task_first_root_succeeds(tmp_path) -> None:
    """The FIRST root task (no parent) must still be creatable."""
    async with mcp_session(tmp_path) as admin:
        assert _root_count() == 0
        tid, is_error = await _create_task(admin, "the one root")
        assert not is_error, "first root creation must succeed"
        assert tid
        assert _root_count() == 1


async def test_create_task_rejects_second_root(tmp_path) -> None:
    """Once a root exists, create_task with parent_task=None is rejected."""
    async with mcp_session(tmp_path) as admin:
        tid, is_error = await _create_task(admin, "first root")
        assert not is_error and tid
        assert _root_count() == 1

        # Second root must be REJECTED (was silently allowed pre-fix).
        tid2, is_error2 = await _create_task(admin, "illegal second root")
        assert is_error2, "create_task allowed a SECOND root task"
        assert tid2 is None
        assert _root_count() == 1, "a second root leaked into the DB"


async def test_create_task_rejects_serial_third_root(tmp_path) -> None:
    """The pentest repro: three serial root-creates through create_task.

    Pre-fix ALL THREE succeeded (ROOTS=3), proving the guard was ABSENT
    (not merely racy). Post-fix only the first survives.
    """
    async with mcp_session(tmp_path) as admin:
        t1, e1 = await _create_task(admin, "root #1")
        _t2, e2 = await _create_task(admin, "root #2")
        _t3, e3 = await _create_task(admin, "root #3")

        assert not e1 and t1, "first root should succeed"
        assert e2, "SECOND serial root should be rejected"
        assert e3, "THIRD serial root should be rejected"
        assert _root_count() == 1, (
            f"expected exactly one root, got {_root_count()}"
        )


async def test_create_task_child_still_works(tmp_path) -> None:
    """A child task (parent_task set) must still be creatable after the
    single root exists — the guard only fires for parentless creates."""
    async with mcp_session(tmp_path) as admin:
        root_id, e = await _create_task(admin, "root")
        assert not e and root_id

        child_id, ec = await _create_task(
            admin, "a child", parent=root_id
        )
        assert not ec, "creating a child under the root must succeed"
        assert child_id

        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT parent_task FROM tasks WHERE task_id = ?",
                (child_id,),
            )
            assert cur.fetchone()["parent_task"] == root_id
        finally:
            conn.close()
        assert _root_count() == 1


async def test_rest_route_rejects_second_root(tmp_path) -> None:
    """The REST ``POST /api/tasks`` adapter over the same impl must also
    reject a second root — one implementation, one invariant."""
    async with mcp_session(tmp_path) as admin:
        r1 = admin.post("/api/tasks", json={"task_title": "rest root"})
        assert r1.status_code == 200, r1.text
        assert r1.json().get("success") is True
        assert _root_count() == 1

        r2 = admin.post(
            "/api/tasks", json={"task_title": "rest second root"}
        )
        # The REST adapter maps Conflict → 409 with an {"error": ...} body.
        assert r2.status_code == 409, (
            f"REST allowed a second root: {r2.status_code} {r2.text}"
        )
        assert "root task already exists" in r2.json().get("error", "")
        assert _root_count() == 1
