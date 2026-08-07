"""SEC-R17 — `request_assistance` task-existence oracle + Mode-3
self-claim audit provenance.

AZ-R17-1 (LOW). ``request_assistance_tool_impl`` leaked a
task-existence oracle: a worker requesting assistance for a task
that EXISTS but isn't theirs got ``PermissionDenied`` (403), while a
NONEXISTENT task got ``NotFound`` (404). The 403-vs-404 split let a
worker enumerate which task_ids exist across the project. This is the
last un-swept sibling of the uniform-phantom-NotFound class already
closed in ``_update_single_task`` / ``bulk_task_operations`` /
``add_task_note``. Fix: the ownership-deny branch returns the SAME
phantom ``NotFound`` a nonexistent task returns, so a foreign existing
task is indistinguishable from a nonexistent one. Assignee (owner)
and admin/manager paths are unchanged.

OBS-R17-AZ (LOW). The Mode-3 worker self-claim path
(``_assign_to_existing_tasks``) logged the audit actor hardcoded as
``"admin"`` instead of the real requesting worker's id — a
provenance misattribution in the same family as the round-1 Mode-0
``created_by`` fix. Fix: the audit row records the real principal
actor (the worker for self-claim, the operator/admin when they
assign on a worker's behalf).
"""

from __future__ import annotations

import datetime as _dt
import secrets

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _seed_task(
    *, title: str = "seeded task", assigned_to: str | None = None
) -> str:
    """Insert a task row directly. Returns the task_id."""
    from agent_mcp.db.connection import get_db_connection

    task_id = f"task_{secrets.token_hex(6)}"
    now = _dt.datetime.now().isoformat()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tasks (task_id, title, description, status, priority, "
        "assigned_to, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        ),
    )
    conn.commit()
    conn.close()
    return task_id


def _audit_actor(action_type: str) -> str | None:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT agent_id FROM agent_actions WHERE action_type = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (action_type,),
        ).fetchone()
    finally:
        conn.close()
    return row["agent_id"] if row is not None else None


# ── AZ-R17-1: request_assistance existence oracle ────────────────


async def test_worker_foreign_and_nonexistent_are_indistinguishable(
    tmp_path,
) -> None:
    """A worker requesting assistance for (a) a foreign EXISTING task
    and (b) a NONEXISTENT task must get the IDENTICAL response — same
    error variant AND same rendered text — so the 403/404 split can't
    be used to enumerate task_ids across the project."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")

        # A real task owned by bob — alice must not learn it exists.
        foreign_task = _seed_task(title="bob's task", assigned_to=bob.agent_id)
        nonexistent_task = "task_deadbeefdeadbeef"

        foreign_res = await alice.call(
            "request_assistance",
            {
                "task_id": foreign_task,
                "description": "please help",
            },
        )
        nonexistent_res = await alice.call(
            "request_assistance",
            {
                "task_id": nonexistent_task,
                "description": "please help",
            },
        )

        foreign_text = foreign_res[0].text
        nonexistent_text = nonexistent_res[0].text

        # Both must render as a NotFound — never PermissionDenied /
        # "Unauthorized" for the foreign-existing case (that's the leak).
        assert "Unauthorized" not in foreign_text, (
            "foreign existing task must NOT leak via a PermissionDenied "
            f"(existence oracle); got {foreign_text!r}"
        )
        assert "not found" in foreign_text.lower(), (
            f"foreign existing task should render as NotFound; "
            f"got {foreign_text!r}"
        )
        assert "not found" in nonexistent_text.lower(), (
            f"nonexistent task should render as NotFound; "
            f"got {nonexistent_text!r}"
        )

        # The load-bearing assertion: the two responses must be BYTE
        # IDENTICAL after masking the (necessarily different) task_id
        # each caller supplied, so no residual signal distinguishes a
        # real foreign task from a phantom one.
        masked_foreign = foreign_text.replace(foreign_task, "<TASK>")
        masked_nonexistent = nonexistent_text.replace(
            nonexistent_task, "<TASK>"
        )
        assert masked_foreign == masked_nonexistent, (
            "foreign-existing and nonexistent responses must be identical "
            f"after masking the id; got {masked_foreign!r} vs "
            f"{masked_nonexistent!r}"
        )


async def test_assignee_can_request_assistance_for_own_task(tmp_path) -> None:
    """Regression: the owner (assignee) can still request assistance
    for their own task."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        own_task = _seed_task(title="alice's task", assigned_to=alice.agent_id)

        res = await alice.call(
            "request_assistance",
            {
                "task_id": own_task,
                "description": "stuck on this",
            },
        )
        text = res[0].text
        assert "Unauthorized" not in text and "not found" not in text.lower(), (
            f"assignee must be able to request assistance for own task; "
            f"got {text!r}"
        )


async def test_admin_can_request_assistance_for_any_existing_task(
    tmp_path,
) -> None:
    """Regression: an admin/manager can request assistance for any
    existing task (not just their own)."""
    async with mcp_session(tmp_path) as admin:
        bob = await admin.create_worker("bob")
        foreign_task = _seed_task(title="bob's task", assigned_to=bob.agent_id)

        res = await admin.call(
            "request_assistance",
            {
                "task_id": foreign_task,
                "description": "admin escalating",
            },
        )
        text = res[0].text
        assert "Unauthorized" not in text and "not found" not in text.lower(), (
            f"admin must be able to request assistance for any existing task; "
            f"got {text!r}"
        )


# ── OBS-R17-AZ: Mode-3 self-claim audit provenance ───────────────


async def test_mode3_self_claim_audit_actor_is_worker(tmp_path) -> None:
    """When a worker self-claims an existing unassigned task via Mode 3,
    the audit record must attribute the action to the worker — not the
    hardcoded literal ``"admin"``."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        task_id = _seed_task(title="up for grabs", assigned_to=None)

        res = await alice.call(
            "assign_task",
            {
                "agent_token": alice.token,
                "task_ids": [task_id],
            },
        )
        text = res[0].text
        assert "Unauthorized" not in text, text

        assert _audit_actor("assigned_task") == "alice", (
            "Mode-3 self-claim audit actor must be the worker, not 'admin'"
        )
