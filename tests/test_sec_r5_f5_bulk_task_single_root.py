"""R5-F5: bulk/unassigned task-creation paths never ran the shared
``_single_root_conflict(cursor)`` guard before INSERTing, unlike the 3
sibling task-creation paths that all check it
(``create_task_tool_impl``, ``create_self_task_tool_impl``, and
``assign_task`` Mode 1 / single).

The DB's partial UNIQUE index (``idx_tasks_single_root``, migration
0023) then threw an ``IntegrityError`` on any 2nd parentless task in
the same call (or the 1st, when a root already existed — the normal
steady state of a working project). That was caught by the generic
``except Exception`` in both bulk helpers and rendered as the OPAQUE
static string "Operation failed" (SEC-R8-1's deliberate error-hiding)
instead of a clean, actionable ``Conflict`` like ``create_task``
gives.

LIVE-confirmed repro:

    assign_task {"tasks":[{"title":"t1","description":"d1"},
                           {"title":"t2","description":"d2"}]}
    -> {"isError":true,"content":[{"type":"text","text":
        "Error: Operation failed"}]}

Two call sites, both fixed the same way (pre-loop guard mirroring
``_single_root_conflict``, extended to reason about the WHOLE batch
since these are the only bulk create paths):

* ``_create_unassigned_tasks`` (Mode 0, "file unassigned task(s)")
* ``_create_and_assign_multiple_tasks`` (Mode 2, "create N tasks and
  assign to an agent")

Batch semantics pinned here (mirrors what the DB's partial UNIQUE
index would enforce — first commit wins, second raises — but caught
BEFORE the DB write instead of after):

* a batch with >=1 parentless task while a root ALREADY exists in the
  DB -> Conflict (existing-root reason, mirrors ``create_task``'s
  message shape);
* a batch with >=2 parentless tasks while NO root exists yet ->
  Conflict (batch-internal double-root);
* a batch with exactly ONE parentless task and NO existing root ->
  succeeds (that task becomes the root);
* a batch where EVERY task specifies a parent -> succeeds regardless
  of whether a root already exists.
"""

from __future__ import annotations

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


def _text(blocks) -> str:
    return "\n".join(
        b.text for b in blocks if isinstance(getattr(b, "text", None), str)
    )


async def _make_root(admin) -> str:
    """Seed an existing root task via the (already-guarded) single
    create_task path, so these tests exercise "root already exists"
    without depending on the bulk paths under test."""
    result = await admin.call(
        "create_task", {"task_title": "existing root", "task_description": "d"}
    )
    assert not getattr(admin, "_last_is_error", False)
    import json as _json

    return _json.loads(result[-1].text)["task_id"]


# ── Mode 0: _create_unassigned_tasks ────────────────────────────────


async def test_mode0_two_parentless_tasks_same_batch_is_clean_conflict(
    tmp_path,
) -> None:
    """LIVE repro: a 2-item tasks array, neither specifying a parent,
    with NO existing root. Must be a clean Conflict, never the opaque
    'Operation failed' Failed-fallthrough."""
    async with mcp_session(tmp_path) as admin:
        assert _root_count() == 0
        result = await admin.call(
            "assign_task",
            {
                "tasks": [
                    {"title": "t1", "description": "d1"},
                    {"title": "t2", "description": "d2"},
                ]
            },
        )
        text = _text(result)
        assert getattr(admin, "_last_is_error", False), (
            f"a batch with 2 parentless tasks must be rejected; got: {text}"
        )
        assert "Operation failed" not in text, (
            f"must not fall through to the opaque Failed path; got: {text}"
        )
        assert "conflict" in text.lower(), (
            f"expected a clean Conflict message; got: {text}"
        )
        # Rolled back cleanly — no half-created batch.
        assert _root_count() == 0


async def test_mode0_one_parentless_task_when_root_exists_is_clean_conflict(
    tmp_path,
) -> None:
    """A SINGLE parentless task in the batch, but a root already exists
    in the DB (the normal steady state of a working project). Must be
    a clean Conflict, not the opaque Failed fallthrough."""
    async with mcp_session(tmp_path) as admin:
        await _make_root(admin)
        assert _root_count() == 1

        result = await admin.call(
            "assign_task",
            {"tasks": [{"title": "t1", "description": "d1"}]},
        )
        text = _text(result)
        assert getattr(admin, "_last_is_error", False), (
            f"a parentless task must be rejected once a root exists; got: {text}"
        )
        assert "Operation failed" not in text, (
            f"must not fall through to the opaque Failed path; got: {text}"
        )
        assert "conflict" in text.lower()
        assert _root_count() == 1


async def test_mode0_single_parentless_task_no_existing_root_succeeds(
    tmp_path,
) -> None:
    """Happy path: exactly ONE parentless task in the batch, no root
    exists yet -> becomes the root, succeeds."""
    async with mcp_session(tmp_path) as admin:
        assert _root_count() == 0
        result = await admin.call(
            "assign_task",
            {"tasks": [{"title": "t1", "description": "d1"}]},
        )
        text = _text(result)
        assert not getattr(admin, "_last_is_error", False), (
            f"a single parentless task with no existing root must succeed; got: {text}"
        )
        assert _root_count() == 1


async def test_mode0_all_tasks_have_parent_succeeds_regardless_of_root(
    tmp_path,
) -> None:
    """Happy path: every task in the batch specifies a parent -> must
    succeed even though a root already exists."""
    async with mcp_session(tmp_path) as admin:
        root_id = await _make_root(admin)
        assert _root_count() == 1

        result = await admin.call(
            "assign_task",
            {
                "tasks": [
                    {
                        "title": "child1",
                        "description": "d1",
                        "parent_task_id": root_id,
                    },
                    {
                        "title": "child2",
                        "description": "d2",
                        "parent_task_id": root_id,
                    },
                ]
            },
        )
        text = _text(result)
        assert not getattr(admin, "_last_is_error", False), (
            f"a batch where every task has a parent must succeed; got: {text}"
        )
        assert _root_count() == 1


# ── Mode 2: _create_and_assign_multiple_tasks ───────────────────────


async def test_mode2_two_parentless_tasks_same_batch_is_clean_conflict(
    tmp_path,
) -> None:
    """Same LIVE repro shape, but create-and-assign (Mode 2, admin
    supplies agent_token + tasks array)."""
    async with mcp_session(tmp_path) as admin:
        bob = await admin.create_worker("bob")
        assert _root_count() == 0

        result = await admin.call(
            "assign_task",
            {
                "agent_token": bob.token,
                "tasks": [
                    {"title": "t1", "description": "d1"},
                    {"title": "t2", "description": "d2"},
                ],
            },
        )
        text = _text(result)
        assert getattr(admin, "_last_is_error", False), (
            f"a batch with 2 parentless tasks must be rejected; got: {text}"
        )
        assert "Operation failed" not in text, (
            f"must not fall through to the opaque Failed path; got: {text}"
        )
        assert "conflict" in text.lower(), (
            f"expected a clean Conflict message; got: {text}"
        )
        assert _root_count() == 0

        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        try:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM tasks WHERE assigned_to = ?",
                (bob.agent_id,),
            ).fetchone()["c"]
        finally:
            conn.close()
        assert n == 0, "no partial task should have been created + assigned"


async def test_mode2_one_parentless_task_when_root_exists_is_clean_conflict(
    tmp_path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        bob = await admin.create_worker("bob")
        await _make_root(admin)
        assert _root_count() == 1

        result = await admin.call(
            "assign_task",
            {
                "agent_token": bob.token,
                "tasks": [{"title": "t1", "description": "d1"}],
            },
        )
        text = _text(result)
        assert getattr(admin, "_last_is_error", False), (
            f"a parentless task must be rejected once a root exists; got: {text}"
        )
        assert "Operation failed" not in text, (
            f"must not fall through to the opaque Failed path; got: {text}"
        )
        assert "conflict" in text.lower()
        assert _root_count() == 1


async def test_mode2_single_parentless_task_no_existing_root_succeeds(
    tmp_path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        bob = await admin.create_worker("bob")
        assert _root_count() == 0

        result = await admin.call(
            "assign_task",
            {
                "agent_token": bob.token,
                "tasks": [{"title": "t1", "description": "d1"}],
            },
        )
        text = _text(result)
        assert not getattr(admin, "_last_is_error", False), (
            f"a single parentless task with no existing root must succeed; got: {text}"
        )
        assert _root_count() == 1


async def test_mode2_all_tasks_have_parent_succeeds_regardless_of_root(
    tmp_path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        bob = await admin.create_worker("bob")
        root_id = await _make_root(admin)
        assert _root_count() == 1

        result = await admin.call(
            "assign_task",
            {
                "agent_token": bob.token,
                "tasks": [
                    {
                        "title": "child1",
                        "description": "d1",
                        "parent_task_id": root_id,
                    },
                    {
                        "title": "child2",
                        "description": "d2",
                        "parent_task_id": root_id,
                    },
                ],
            },
        )
        text = _text(result)
        assert not getattr(admin, "_last_is_error", False), (
            f"a batch where every task has a parent must succeed; got: {text}"
        )
        assert _root_count() == 1
