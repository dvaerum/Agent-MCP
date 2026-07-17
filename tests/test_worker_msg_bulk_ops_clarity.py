"""bulk_task_operations worker-facing message clarity (round 2).

The single-task paths already split their worker-facing denials
(unassigned → actionable "claim it first"; foreign → phantom-404; config
gate names the toggle + who enables). The BULK surface lagged:

- F2: the per-op ownership gate collapsed UNASSIGNED (claimable pool) and
  FOREIGN-owned into the identical "Task not found", denying a worker the
  "claim it first" guidance the single path gives for a pool task it can
  already see in view_tasks. FOREIGN must STILL phantom (PF-1 oracle).
- F3: the config_allow_worker_update_own_status denial named the toggle
  but not who can enable it.
- F4/F4b: priority / reassign refusals said "requires admin privileges"
  (reads like a per-task auth failure the worker should retry) rather
  than "this is an operator/manager-only field/action".
"""

from __future__ import annotations

import datetime as _dt
import secrets

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _seed_task(task_id: str, title: str, *, assigned_to, status="pending") -> None:
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, title, description, status, priority, "
            " assigned_to, created_by, created_at, updated_at, parent_task, "
            " child_tasks, depends_on_tasks, notes) "
            "VALUES (?, ?, 'd', ?, 'medium', ?, 'admin', ?, ?, NULL, "
            "        '[]', '[]', '[]')",
            (task_id, title, status, assigned_to, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    g.tasks[task_id] = {
        "task_id": task_id, "title": title, "description": "d",
        "status": status, "priority": "medium", "assigned_to": assigned_to,
        "created_by": "admin", "created_at": now, "updated_at": now,
        "parent_task": None, "child_tasks": [], "depends_on_tasks": [],
        "notes": [],
    }


def _set_config(key: str, json_value: str) -> None:
    """Write a project_settings toggle directly (the store _get_config_bool
    reads). ``value`` is JSON-encoded, e.g. 'false'."""
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO project_settings (context_key, value, updated_at, "
            " updated_by) VALUES (?, ?, ?, 'test') "
            "ON CONFLICT(context_key) DO UPDATE SET value=excluded.value",
            (key, json_value, now),
        )
        conn.commit()
    finally:
        conn.close()


def _text(blocks) -> str:
    return "\n".join(
        b.text for b in blocks if isinstance(getattr(b, "text", None), str)
    )


async def _bulk(worker, ops):
    return _text(await worker.call("bulk_task_operations", {"operations": ops}))


# ── F2: unassigned → guidance, foreign → phantom ─────────────────────


async def test_bulk_unassigned_task_gets_claim_guidance(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        pool = f"task_{secrets.token_hex(6)}"
        _seed_task(pool, "claimable", assigned_to=None)
        text = (await _bulk(
            alice, [{"type": "update_status", "task_id": pool, "status": "in_progress"}]
        )).lower()
        assert "claim it first" in text or "unassigned" in text, (
            f"bulk op on an unassigned pool task should give claim guidance; got: {text}"
        )
        assert "not found" not in text, (
            f"an unassigned (pool-visible) task must NOT phantom in bulk; got: {text}"
        )


async def test_bulk_foreign_task_still_phantom(tmp_path) -> None:
    """SECURITY: a FOREIGN-owned task must STILL collapse to 'not found'
    in the bulk path — no owner leak, no exists-vs-not signal."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await admin.create_worker("bob")
        bobs = f"task_{secrets.token_hex(6)}"
        _seed_task(bobs, "bob's", assigned_to="bob", status="in_progress")
        text = (await _bulk(
            alice, [{"type": "update_status", "task_id": bobs, "status": "completed"}]
        )).lower()
        assert "not found" in text, f"foreign task must phantom in bulk; got: {text}"
        assert "bob" not in text, f"must not leak owner id; got: {text}"
        assert "claim it first" not in text


async def test_bulk_nonexistent_still_phantom(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        ghost = f"task_{secrets.token_hex(6)}"
        text = (await _bulk(
            alice, [{"type": "update_status", "task_id": ghost, "status": "completed"}]
        )).lower()
        assert "not found" in text, f"nonexistent task must phantom; got: {text}"


# ── F3: config gate names who-enables ────────────────────────────────


async def test_bulk_status_config_off_names_who_enables(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        _set_config("config_allow_worker_update_own_status", "false")
        mine = f"task_{secrets.token_hex(6)}"
        _seed_task(mine, "mine", assigned_to="alice", status="pending")
        text = (await _bulk(
            alice, [{"type": "update_status", "task_id": mine, "status": "in_progress"}]
        )).lower()
        assert "config_allow_worker_update_own_status" in text
        assert "dashboard settings" in text or "ask an admin" in text, (
            f"config-gate denial should say who enables it; got: {text}"
        )


# ── F4 / F4b: priority + reassign are role limits, not per-task auth ──


async def test_bulk_priority_is_role_limit_not_retry(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        mine = f"task_{secrets.token_hex(6)}"
        _seed_task(mine, "mine", assigned_to="alice")
        text = (await _bulk(
            alice, [{"type": "update_priority", "task_id": mine, "priority": "high"}]
        )).lower()
        assert "operator/manager-only" in text or "ask a supervisor" in text, (
            f"priority denial should read as a role limit; got: {text}"
        )
        assert "requires admin privileges" not in text


async def test_bulk_reassign_is_role_limit(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await admin.create_worker("carol")
        mine = f"task_{secrets.token_hex(6)}"
        _seed_task(mine, "mine", assigned_to="alice")
        text = (await _bulk(
            alice, [{"type": "reassign", "task_id": mine, "assigned_to": "carol"}]
        )).lower()
        assert "operator/manager-only" in text or "ask a supervisor" in text, (
            f"reassign denial should read as a role limit; got: {text}"
        )
        assert "requires admin privileges" not in text
