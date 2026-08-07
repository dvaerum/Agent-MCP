"""Round-3 business-logic regressions for the task write surface.

Two confirmed-live findings, both scoped to
``agent_mcp/tools/task_tools.py`` + ``agent_mcp/app/routers/tasks.py``:

BL-1 — task create/delete must honour the repository's
    cache-reconciliation contract. ``task_repo.create``/``delete`` on the
    ``connection=`` path defer the ``g.tasks`` cache write + EventBus
    publish to the caller (see ``task_repository.py`` docstrings). Two
    paths skipped that duty:

      * ``delete_task_tool_impl`` deleted rows with raw SQL and never
        evicted them from ``g.tasks`` nor published ``task.deleted`` — a
        deleted task kept showing in ``view_tasks``.
      * ``create_task_api_route`` (REST ``POST /api/tasks``) created the
        row but never upserted it into ``g.tasks`` nor published
        ``task.created`` / woke the assignee — a REST-created task was
        absent from ``view_tasks``.

BL-2 — task creation must maintain the parent's ``child_tasks``
    back-reference, and ``delete_task``'s force-cascade must enumerate
    children authoritatively from the ``parent_task`` FK column (not the
    ``child_tasks`` JSON mirror). Without the mirror, deleting a parent
    with ``force_delete=True`` missed its children and hit the
    ``tasks.parent_task`` self-FK → ``FOREIGN KEY constraint failed``.
"""

from __future__ import annotations

import datetime as _dt
import json
import secrets

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


# --- helpers ---------------------------------------------------------------


def _capture_publishes(monkeypatch) -> list:
    """Record every ``task.*`` event the publish shim emits.

    Patches ``_event_bus_shim.publish`` (the single funnel both the repo
    and the create/delete reconcile paths call) so we observe the
    addressee + event type without racing the real bus adapters.
    """
    published: list = []

    def _capture(agent_id, event_type, payload=None):
        published.append((agent_id, event_type, payload))

    monkeypatch.setattr(
        "agent_mcp.core.event_bus_shim.publish", _capture
    )
    return published


def _capture_inbox_notifications(monkeypatch) -> list:
    """Record every ``notify_agent_inbox(agent_id)`` the router/tools fire.

    Patches the symbol on ``agent_mcp.core.globals`` (the module the
    router looks the name up on at call time)."""
    notified: list = []
    monkeypatch.setattr(
        "agent_mcp.core.globals.notify_agent_inbox",
        lambda agent_id: notified.append(agent_id),
    )
    return notified


def _seed_task(
    *,
    title: str = "seeded",
    status: str = "pending",
    assigned_to: str | None = None,
    parent_task: str | None = None,
    created_by: str = "admin",
) -> str:
    """Insert a task row directly (bypassing the tool surface) for setup."""
    from agent_mcp.db.connection import get_db_connection

    task_id = f"task_{secrets.token_hex(6)}"
    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO tasks (task_id, title, description, status, "
            "priority, assigned_to, created_by, created_at, updated_at, "
            "parent_task, child_tasks) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                title,
                "seeded description",
                status,
                "medium",
                assigned_to,
                created_by,
                now,
                now,
                parent_task,
                json.dumps([]),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return task_id


def _task_field(task_id: str, field: str):
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        row = conn.execute(
            f"SELECT {field} FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    return row[field] if row is not None else None


def _child_tasks(task_id: str) -> list:
    raw = _task_field(task_id, "child_tasks")
    return json.loads(raw or "[]")


def _first_text(result) -> str:
    if not result:
        return ""
    return getattr(result[0], "text", "") or ""


# --- BL-1: REST create reconciles the g.tasks cache -----------------------


async def test_rest_create_task_shows_in_view_tasks(tmp_path) -> None:
    """A task created via ``POST /api/tasks`` must appear in
    ``view_tasks`` (which reads ``g.tasks``) immediately."""
    async with mcp_session(tmp_path) as admin:
        from agent_mcp.core import globals as g

        r = admin.post(
            "/api/tasks",
            json={
                "task_title": "rest created task",
                "task_description": "made over REST",
            },
        )
        assert r.status_code == 200, r.text
        task_id = r.json()["task_id"]

        # g.tasks is the read source for view_tasks — it must carry the
        # freshly-created row.
        assert task_id in g.tasks, (
            "REST-created task must be reconciled into the g.tasks cache"
        )

        res = await admin.call("view_tasks", {})
        text = _first_text(res)
        assert "rest created task" in text, (
            "REST-created task must be visible in view_tasks; "
            f"got {text!r}"
        )


async def test_rest_create_assigned_publishes_and_notifies(
    tmp_path, monkeypatch
) -> None:
    """An assigned REST-create must publish ``task.created`` AND wake the
    assignee's inbox (so a blocked ``wait_for_events`` returns)."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")

        published = _capture_publishes(monkeypatch)
        notified = _capture_inbox_notifications(monkeypatch)

        r = admin.post(
            "/api/tasks",
            json={
                "task_title": "assigned rest task",
                "task_description": "d",
                "assigned_to": "alice",
            },
        )
        assert r.status_code == 200, r.text

        events = {e[1] for e in published}
        assert "task.created" in events, (
            f"assigned REST-create must publish task.created; saw {events}"
        )
        assert "alice" in notified, (
            "assigned REST-create must wake the assignee's inbox; "
            f"saw {notified}"
        )


# --- BL-1: delete evicts the cache + publishes ----------------------------


async def test_delete_mcp_task_evicts_cache_and_publishes(
    tmp_path, monkeypatch
) -> None:
    """Deleting an MCP-created task must drop it from ``g.tasks`` (so
    ``view_tasks`` stops showing it) and publish ``task.deleted``."""
    async with mcp_session(tmp_path) as admin:
        from agent_mcp.core import globals as g

        # Create via the Mode-0 (unassigned) MCP path, which upserts the
        # row into g.tasks.
        create = await admin.call(
            "assign_task",
            {
                "task_title": "delete me soon",
                "task_description": "d",
            },
        )
        import re

        m = re.search(r"task_[a-f0-9_]+", _first_text(create))
        assert m, f"no task id in create response: {_first_text(create)!r}"
        task_id = m.group(0)
        assert task_id in g.tasks, "sanity: created task should be cached"

        published = _capture_publishes(monkeypatch)
        res = await admin.call(
            "delete_task", {"task_id": task_id, "force_delete": True}
        )

        text = _first_text(res)
        assert not getattr(admin, "_last_is_error", False), text

        assert task_id not in g.tasks, (
            "deleted task must be evicted from the g.tasks cache"
        )
        view = await admin.call("view_tasks", {})
        assert task_id not in _first_text(view), (
            "deleted task must not appear in view_tasks"
        )
        deleted = [e for e in published if e[1] == "task.deleted"]
        assert any(e[2].get("task_id") == task_id for e in deleted), (
            f"delete must publish task.deleted for {task_id}; saw {published}"
        )


# --- BL-2: parent child_tasks mirror + authoritative cascade --------------


async def test_assign_task_child_maintains_parent_child_tasks(
    tmp_path, monkeypatch
) -> None:
    """``assign_task`` creating a child under a parent must append the
    child id to the parent's ``child_tasks`` mirror."""
    # RAG placement is non-deterministic under the mock embedder; disable
    # it so the Mode-1 create is a straight insert.
    monkeypatch.setattr(
        "agent_mcp.tools.task_tools.ENABLE_TASK_PLACEMENT_RAG", False
    )
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        parent_id = _seed_task(title="parent", parent_task=None)

        create = await admin.call(
            "assign_task",
            {
                "agent_token": alice.token,
                "task_title": "child task",
                "task_description": "d",
                "parent_task_id": parent_id,
            },
        )
        text = _first_text(create)
        assert not getattr(admin, "_last_is_error", False), text
        import re

        ids = re.findall(r"task_[a-f0-9_]+", text)
        child_id = next((i for i in ids if i != parent_id), None)
        assert child_id, f"no child task id in response: {text!r}"

        assert child_id in _child_tasks(parent_id), (
            "parent's child_tasks mirror must contain the new child; "
            f"got {_child_tasks(parent_id)!r}"
        )


async def test_force_delete_parent_cascades_child_no_fk_error(
    tmp_path, monkeypatch
) -> None:
    """``delete_task(parent, force_delete=True)`` must cascade-delete the
    child (enumerated authoritatively from the parent_task FK) without a
    FOREIGN KEY constraint failure."""
    monkeypatch.setattr(
        "agent_mcp.tools.task_tools.ENABLE_TASK_PLACEMENT_RAG", False
    )
    async with mcp_session(tmp_path) as admin:
        parent_id = _seed_task(title="parent", parent_task=None)

        # Mode-0 (unassigned) child under the parent. Keeping it
        # unassigned isolates the BL-2 finding (the ``tasks.parent_task``
        # self-FK) from the separate ``agents.current_task`` FK that an
        # assigned child would introduce.
        create = await admin.call(
            "assign_task",
            {
                "task_title": "child task",
                "task_description": "d",
                "parent_task_id": parent_id,
            },
        )
        ctext = _first_text(create)
        import re

        ids = re.findall(r"task_[a-f0-9_]+", ctext)
        child_id = next((i for i in ids if i != parent_id), None)
        assert child_id, f"no child task id in response: {ctext!r}"

        res = await admin.call(
            "delete_task", {"task_id": parent_id, "force_delete": True}
        )
        text = _first_text(res)
        assert not getattr(admin, "_last_is_error", False), (
            f"force_delete of parent must succeed; got {text!r}"
        )
        assert "FOREIGN KEY" not in text, text

        # Both rows are gone.
        assert _task_field(parent_id, "task_id") is None, (
            "parent must be deleted"
        )
        assert _task_field(child_id, "task_id") is None, (
            "child must be cascade-deleted, not orphaned"
        )
