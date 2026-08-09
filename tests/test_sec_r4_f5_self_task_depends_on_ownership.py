"""SEC-R4-F5 — ``create_self_task`` ``depends_on`` cross-agent edge injection.

In ``create_self_task`` the PARENT edge is ownership-gated (AZ-R19-1:
a worker may only parent under a task it OWNS), but the ``depends_on``
edge was NOT. A worker could create a self-task depending on a task
owned by a DIFFERENT agent. That edge is load-bearing at runtime:
when the foreign task later completes, ``_advance_dependents_after_
completion`` auto-advances the dependent via a ``system_transition``
(which bypasses the ownership gate by design for legit deps) and
``_wake_task_assignees`` wakes the injector — a cross-principal
completion oracle (the injector learns when the victim finished a task
it can't even see via ``view_tasks``).

Fix mirrors the parent gate exactly: for a non-privileged caller
(``not tasks.assign``), each ``depends_on`` task_id — from BOTH the
direct argument AND an accepted RAG suggestion — must resolve to a row
the caller OWNS (``assigned_to == requesting_agent_id``). A FOREIGN
*or* NONEXISTENT dependency collapses to the SAME phantom ``NotFound``
(no existence oracle). Supervision-tier callers (``tasks.assign``)
keep the ability to depend on any task.
"""

from __future__ import annotations

import datetime as _dt
import json
import secrets

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _seed_task(
    *,
    title: str = "seeded task",
    assigned_to: str | None = None,
) -> str:
    """Insert a task row directly. Returns the task_id."""
    from agent_mcp.db.connection import get_db_connection

    from tests.conftest import existing_root_task_id

    task_id = f"task_{secrets.token_hex(6)}"
    now = _dt.datetime.now().isoformat()

    # R15-BL-1: chain under the single root (first seed = root, rest are
    # children). Dependency-ownership keys on assigned_to, not parentage.
    parent = existing_root_task_id()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tasks (task_id, title, description, status, priority, "
        "assigned_to, created_by, created_at, updated_at, parent_task) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            title,
            "test description",
            "pending",
            "medium",
            assigned_to,
            "admin",
            now,
            now,
            parent,
        ),
    )
    conn.commit()
    conn.close()
    return task_id


def _tasks_depending_on(dep_id: str) -> list[str]:
    """Return task_ids of every row whose depends_on_tasks references dep_id."""
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT task_id, depends_on_tasks FROM tasks"
        ).fetchall()
    finally:
        conn.close()
    hits = []
    for row in rows:
        deps = json.loads(row["depends_on_tasks"] or "[]")
        if dep_id in deps:
            hits.append(row["task_id"])
    return hits


async def _approved_validator(*args, **kwargs):
    """Deterministic RAG stub: always approve placement so the test
    exercises the ownership gate, not the RAG denial path."""
    return {"status": "approved", "suggestions": {}, "message": ""}


# ── R4-F5: cross-agent injection via foreign depends_on ──────────


async def test_worker_self_task_cannot_depend_on_foreign_task(
    tmp_path, monkeypatch
) -> None:
    """A worker self-tasking with ``depends_on`` a task owned by ANOTHER
    agent must get a phantom NotFound AND leave NO persisted edge (the
    injected dependency must not appear on any row)."""
    monkeypatch.setattr(
        "agent_mcp.tools.task_tools.validate_task_placement",
        _approved_validator,
    )
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")

        own_parent = _seed_task(title="alice's task", assigned_to=alice.agent_id)
        bob_dep = _seed_task(title="bob's secret task", assigned_to=bob.agent_id)

        res = await alice.call(
            "create_self_task",
            {
                "task_title": "INJECTED oracle self-task",
                "task_description": "attacker-controlled body",
                "parent_task_id": own_parent,
                "depends_on_tasks": [bob_dep],
            },
        )
        text = res[0].text

        assert "not found" in text.lower(), (
            "worker depending on a foreign task must get a phantom "
            f"NotFound; got {text!r}"
        )
        assert "Unauthorized" not in text, (
            "must not leak via PermissionDenied (existence oracle); "
            f"got {text!r}"
        )
        # Load-bearing: the cross-agent edge must NOT be persisted — no
        # row may depend on bob's task (no completion oracle for alice).
        assert _tasks_depending_on(bob_dep) == [], (
            "worker must NOT be able to inject a depends_on edge on a "
            f"foreign task; got {_tasks_depending_on(bob_dep)!r}"
        )


async def test_worker_self_task_foreign_and_nonexistent_dep_indistinguishable(
    tmp_path, monkeypatch
) -> None:
    """A foreign EXISTING dep and a NONEXISTENT dep must render
    IDENTICALLY (after masking the id) — no existence oracle."""
    monkeypatch.setattr(
        "agent_mcp.tools.task_tools.validate_task_placement",
        _approved_validator,
    )
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")

        own_parent = _seed_task(title="alice's task", assigned_to=alice.agent_id)
        foreign_dep = _seed_task(title="bob's task", assigned_to=bob.agent_id)
        nonexistent_dep = "task_deadbeefdeadbeef"

        foreign_res = await alice.call(
            "create_self_task",
            {
                "task_title": "child A",
                "task_description": "body",
                "parent_task_id": own_parent,
                "depends_on_tasks": [foreign_dep],
            },
        )
        nonexistent_res = await alice.call(
            "create_self_task",
            {
                "task_title": "child A",
                "task_description": "body",
                "parent_task_id": own_parent,
                "depends_on_tasks": [nonexistent_dep],
            },
        )

        masked_foreign = foreign_res[0].text.replace(foreign_dep, "<T>")
        masked_nonexistent = nonexistent_res[0].text.replace(
            nonexistent_dep, "<T>"
        )
        assert masked_foreign == masked_nonexistent, (
            "foreign-existing and nonexistent dep responses must be "
            f"identical after masking; got {masked_foreign!r} vs "
            f"{masked_nonexistent!r}"
        )


async def test_worker_self_task_nonexistent_dep_rejected(
    tmp_path, monkeypatch
) -> None:
    """A NONEXISTENT dependency id is rejected for a worker caller."""
    monkeypatch.setattr(
        "agent_mcp.tools.task_tools.validate_task_placement",
        _approved_validator,
    )
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        own_parent = _seed_task(title="alice's task", assigned_to=alice.agent_id)

        res = await alice.call(
            "create_self_task",
            {
                "task_title": "dangling dep",
                "task_description": "body",
                "parent_task_id": own_parent,
                "depends_on_tasks": ["task_deadbeefdeadbeef"],
            },
        )
        text = res[0].text
        assert "not found" in text.lower(), (
            f"nonexistent dependency must be rejected; got {text!r}"
        )


# ── Regressions ──────────────────────────────────────────────────


async def test_worker_self_task_can_depend_on_own_task(
    tmp_path, monkeypatch
) -> None:
    """Regression: a worker CAN depend on a task it OWNS."""
    monkeypatch.setattr(
        "agent_mcp.tools.task_tools.validate_task_placement",
        _approved_validator,
    )
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        own_parent = _seed_task(title="alice's task", assigned_to=alice.agent_id)
        own_dep = _seed_task(title="alice's dep", assigned_to=alice.agent_id)

        res = await alice.call(
            "create_self_task",
            {
                "task_title": "legit dependent self-task",
                "task_description": "sequencing my own work",
                "parent_task_id": own_parent,
                "depends_on_tasks": [own_dep],
            },
        )
        text = res[0].text
        assert "not found" not in text.lower() and "Unauthorized" not in text, (
            f"worker must be able to depend on its own task; got {text!r}"
        )
        # The edge was persisted onto the new self-task.
        assert _tasks_depending_on(own_dep), (
            "worker's self-task should carry the owned depends_on edge; "
            f"got {_tasks_depending_on(own_dep)!r}"
        )


async def test_admin_self_task_can_depend_on_any_task(
    tmp_path, monkeypatch
) -> None:
    """Regression: a privileged (tasks.assign) caller keeps the ability
    to set a cross-owner dependency."""
    monkeypatch.setattr(
        "agent_mcp.tools.task_tools.validate_task_placement",
        _approved_validator,
    )
    async with mcp_session(tmp_path) as admin:
        bob = await admin.create_worker("bob")
        admin_parent = _seed_task(title="admin's task", assigned_to="admin")
        bob_dep = _seed_task(title="bob's task", assigned_to=bob.agent_id)

        res = await admin.call(
            "create_self_task",
            {
                "task_title": "coordination task with cross-owner dep",
                "task_description": "operator sequencing across agents",
                "parent_task_id": admin_parent,
                "depends_on_tasks": [bob_dep],
            },
        )
        text = res[0].text
        assert "not found" not in text.lower() and "Unauthorized" not in text, (
            f"privileged caller must be able to depend on any task; got {text!r}"
        )
        assert _tasks_depending_on(bob_dep), (
            "privileged caller's cross-owner dependency should persist; "
            f"got {_tasks_depending_on(bob_dep)!r}"
        )
