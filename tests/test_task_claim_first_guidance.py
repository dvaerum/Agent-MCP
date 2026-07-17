"""A worker that acts on an UNASSIGNED (claimable-pool) task must be
told the remedy — self-claim it first — not stonewalled with a phantom
404.

Reported live symptom. A worker finished the work for two pool tasks
but could not mark them completed:

    "not yet marked completed — I lack permission on unassigned tasks."

Root cause. After the #515 read-visibility fix a worker can SEE
unassigned tasks via ``view_tasks`` / ``search_tasks``, but the WRITE
ownership gate still requires ``assigned_to == requesting_agent_id``.
So ``update_task_status`` on an unassigned task denied with the
phantom-404 ``"Task 'X' not found"`` — confusing (the worker just saw
it in the pool) and, worse, silent about the remedy: self-claim the
task first (``assign_task`` Mode 3), then update it.

The fix — split UNASSIGNED from FOREIGN-OWNED at the ownership-deny
branch:

- UNASSIGNED (``assigned_to`` NULL/empty): return actionable guidance
  naming ``assign_task`` and telling the worker to claim it. The pool
  task has no owner to hide and is already publicly listed, so this
  leaks nothing new.
- FOREIGN-OWNED (``assigned_to`` == another agent): KEEP the phantom
  ``"not found"`` (PF-1 / AZ-R17-1 existence-oracle control) — a worker
  must not tell "exists but not yours" from "doesn't exist", nor learn
  the owner's id.

The security invariant (case 2 below) MUST stay green.
"""

from __future__ import annotations

import datetime as _dt
import secrets

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


def _seed_task(
    task_id: str,
    title: str,
    *,
    assigned_to: str | None,
    status: str = "pending",
    description: str = "task in the pool",
) -> None:
    """Populate BOTH the tasks table (so ``assign_task`` Mode 3 / the
    ``update_task_status`` DB read see it) and ``g.tasks`` (the in-memory
    cache the read tools query)."""
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO tasks "
            "(task_id, title, description, status, priority, assigned_to, "
            " created_by, created_at, updated_at, parent_task, child_tasks, "
            " depends_on_tasks, notes) "
            "VALUES (?, ?, ?, ?, 'medium', ?, 'admin', ?, ?, NULL, "
            "        '[]', '[]', '[]')",
            (task_id, title, description, status, assigned_to, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    g.tasks[task_id] = {
        "task_id": task_id,
        "title": title,
        "description": description,
        "status": status,
        "priority": "medium",
        "assigned_to": assigned_to,
        "created_by": "admin",
        "created_at": now,
        "updated_at": now,
        "parent_task": None,
        "child_tasks": [],
        "depends_on_tasks": [],
        "notes": [],
    }


def _text(blocks) -> str:
    return "\n".join(
        b.text for b in blocks if isinstance(getattr(b, "text", None), str)
    )


# --- 1. UNASSIGNED: guidance, not a bare 404 --------------------------


async def test_worker_update_unassigned_task_is_guided_to_claim(
    tmp_path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        pool_id = f"task_{secrets.token_hex(6)}"
        _seed_task(pool_id, "pool work to complete", assigned_to=None)

        text = _text(
            await alice.call(
                "update_task_status",
                {
                    "token": alice.token,
                    "task_id": pool_id,
                    "status": "completed",
                },
            )
        )
        low = text.lower()
        # Actionable guidance — names the remedy and the tool.
        assert "claim" in low, (
            f"worker updating an unassigned task must be told to CLAIM it; "
            f"got {text!r}"
        )
        assert "assign_task" in low, (
            f"guidance must name the assign_task tool as the remedy; "
            f"got {text!r}"
        )
        # NOT the confusing phantom-404 the worker got before the fix.
        assert "not found" not in low, (
            f"unassigned task must NOT render as a phantom 'not found'; "
            f"got {text!r}"
        )


# --- 2. SECURITY (must stay GREEN): foreign-owned stays phantom-404 ---


async def test_worker_update_foreign_task_stays_phantom_not_found(
    tmp_path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")
        bobs_id = f"task_{secrets.token_hex(6)}"
        _seed_task(bobs_id, "bob's secret work", assigned_to=bob.agent_id)

        text = _text(
            await alice.call(
                "update_task_status",
                {
                    "token": alice.token,
                    "task_id": bobs_id,
                    "status": "completed",
                },
            )
        )
        low = text.lower()
        # PF-1 / AZ-R17-1: no owner disclosure, no claim hint, no leak.
        assert "not found" in low, (
            f"foreign-owned task must stay a phantom NotFound; got {text!r}"
        )
        assert "claim" not in low and "assign_task" not in low, (
            f"foreign-owned deny must NOT hint at claiming (owner-existence "
            f"leak); got {text!r}"
        )
        assert "unauthorized" not in low, (
            f"foreign-owned deny must NOT surface as a distinct 403 "
            f"(existence oracle); got {text!r}"
        )
        assert bob.agent_id not in text, (
            f"foreign-owned deny must never leak the owner's id; got {text!r}"
        )


# --- 3. OWN task: no regression ---------------------------------------


async def test_worker_updates_own_task_succeeds(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        own_id = f"task_{secrets.token_hex(6)}"
        _seed_task(own_id, "alice's own task", assigned_to=alice.agent_id)

        text = _text(
            await alice.call(
                "update_task_status",
                {
                    "token": alice.token,
                    "task_id": own_id,
                    "status": "completed",
                },
            )
        )
        low = text.lower()
        assert "not found" not in low and "unauthorized" not in low, (
            f"worker must be able to update its OWN task; got {text!r}"
        )


# --- 4. END-TO-END: claim the pool task, THEN update it ---------------


async def test_worker_claims_then_updates_pool_task(tmp_path) -> None:
    """The guided remedy actually works: after self-claiming the
    unassigned task (Mode 3) the worker can update its status."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        pool_id = f"task_{secrets.token_hex(6)}"
        _seed_task(pool_id, "claim then complete", assigned_to=None)

        # follow the guidance: self-claim first
        claim = _text(
            await alice.call(
                "assign_task",
                {"agent_token": alice.token, "task_ids": [pool_id]},
            )
        )
        assert "Unauthorized" not in claim, claim

        # now the update lands
        text = _text(
            await alice.call(
                "update_task_status",
                {
                    "token": alice.token,
                    "task_id": pool_id,
                    "status": "completed",
                },
            )
        )
        low = text.lower()
        assert "not found" not in low and "unauthorized" not in low, (
            f"after self-claim the worker must be able to update the task; "
            f"got {text!r}"
        )

        # authoritative check: DB shows the terminal status
        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        row = conn.execute(
            "SELECT status, assigned_to FROM tasks WHERE task_id = ?",
            (pool_id,),
        ).fetchone()
        conn.close()
        assert row is not None and row["status"] == "completed", (
            f"guided remedy did not land; status={row and row['status']!r}"
        )
        assert row["assigned_to"] == alice.agent_id


# --- 5. add_task_note sibling: unassigned pool task is guided ---------


async def test_worker_note_on_unassigned_task_is_guided_to_claim(
    tmp_path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        pool_id = f"task_{secrets.token_hex(6)}"
        _seed_task(pool_id, "pool note target", assigned_to=None)

        text = _text(
            await alice.call(
                "add_task_note",
                {
                    "token": alice.token,
                    "task_id": pool_id,
                    "text": "progress note",
                },
            )
        )
        low = text.lower()
        assert "claim" in low and "assign_task" in low, (
            f"noting on an unassigned task must guide the worker to claim it; "
            f"got {text!r}"
        )
        assert "not found" not in low, (
            f"unassigned note target must NOT render as phantom 'not found'; "
            f"got {text!r}"
        )


async def test_worker_note_on_foreign_task_stays_phantom_not_found(
    tmp_path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")
        bobs_id = f"task_{secrets.token_hex(6)}"
        _seed_task(bobs_id, "bob's note target", assigned_to=bob.agent_id)

        text = _text(
            await alice.call(
                "add_task_note",
                {
                    "token": alice.token,
                    "task_id": bobs_id,
                    "text": "sneaky note",
                },
            )
        )
        low = text.lower()
        assert "not found" in low, (
            f"foreign-owned note target must stay phantom NotFound; "
            f"got {text!r}"
        )
        assert "claim" not in low and bob.agent_id not in text, (
            f"foreign-owned note deny must not hint/leak; got {text!r}"
        )


# --- 6. request_assistance sibling: unassigned pool task is guided ----


async def test_worker_assist_on_unassigned_task_is_guided_to_claim(
    tmp_path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        pool_id = f"task_{secrets.token_hex(6)}"
        _seed_task(pool_id, "pool assist target", assigned_to=None)

        text = _text(
            await alice.call(
                "request_assistance",
                {
                    "token": alice.token,
                    "task_id": pool_id,
                    "description": "need help here",
                },
            )
        )
        low = text.lower()
        assert "claim" in low and "assign_task" in low, (
            f"requesting assistance on an unassigned task must guide the "
            f"worker to claim it; got {text!r}"
        )
        assert "not found" not in low, (
            f"unassigned assist target must NOT render as phantom "
            f"'not found'; got {text!r}"
        )
