"""SEC-R18 — Mode-3 worker self-claim existence oracle (AZ-R18-1) +
terminal-sink on the assign axis (BL-R18-1).

Both findings live in ``_assign_to_existing_tasks`` — the
``assign_task`` Mode-3 worker self-claim path (default-enabled via
``config_allow_worker_self_assign``).

AZ-R18-1 (LOW-MED). For a NON-admin self-claim caller the pre-fix
branches leaked object existence and a foreign owner id:

  * nonexistent task           → ``NotFound``
  * task assigned to someone    → ``Conflict("... assigned to <owner>")``
    else                          (leaks the foreign owner id AND that
                                    the task exists)
  * caps mismatch              → ``PermissionDenied`` (reveals the task
                                    exists)

So a worker could enumerate which task_ids exist and read a foreign
task's assignee. This is the LAST worker-reachable sibling of the
uniform-phantom-NotFound existence-oracle class (AZ-R17-1 et al).

BL-R18-1 (LOW-MED). The same SELECT omitted ``status`` and there was
no terminal-status check, so a TERMINAL but UNASSIGNED task
(reachable by admin-cancelling an unclaimed task, or the BL-R17-2
purge path clearing a terminal task's assignee) was self-CLAIMABLE by
a worker — who would then re-execute finished work. Terminal must be
a sink on the assign axis too.

Unified fix: for a non-admin self-claim caller EVERY non-claimable
outcome (nonexistent / foreign-assigned / terminal-status /
caps-mismatch) collapses to the IDENTICAL phantom ``NotFound`` the
nonexistent branch returns. Only a genuinely claimable task (exists +
unassigned + non-terminal + caps-match) succeeds. Admin/manager
callers (``tasks.assign``) keep the real informative errors
(Conflict-with-owner, PermissionDenied, and a terminal block).
"""

from __future__ import annotations

import datetime as _dt
import secrets

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _seed_task(
    *,
    title: str = "seeded task",
    assigned_to: str | None = None,
    status: str = "pending",
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
            status,
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


def _task_state(task_id: str) -> tuple[str | None, str | None]:
    """Return (assigned_to, status) straight from the DB."""
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT assigned_to, status FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return (None, None)
    return (row["assigned_to"], row["status"])


# ── AZ-R18-1: worker self-claim existence oracle ─────────────────


async def test_worker_self_claim_outcomes_are_indistinguishable(
    tmp_path,
) -> None:
    """A worker self-claiming (a) a NONEXISTENT task and (b) a FOREIGN
    ASSIGNED task must get the IDENTICAL response — same error variant
    AND same rendered text after masking the supplied id — so the
    differential response can't be used to enumerate task_ids or read a
    foreign owner id.

    (PR5 retired the structured capability-tag gate, so the former
    caps-mismatch arm of this oracle no longer exists — a task with no
    assignee and no terminal status is simply claimable now.)"""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")

        nonexistent = "task_deadbeefdeadbeef"
        foreign = _seed_task(title="bob's task", assigned_to=bob.agent_id)

        async def self_claim(task_id: str) -> str:
            res = await alice.call(
                "assign_task",
                {"agent_token": alice.token, "task_ids": [task_id]},
            )
            return res[0].text

        nonexistent_text = await self_claim(nonexistent)
        foreign_text = await self_claim(foreign)

        # None may leak existence via Conflict/PermissionDenied, and the
        # foreign owner id must never appear.
        assert bob.agent_id not in foreign_text, (
            "foreign task's owner id must NOT leak; "
            f"got {foreign_text!r}"
        )
        for label, text in [
            ("nonexistent", nonexistent_text),
            ("foreign", foreign_text),
        ]:
            assert "Unauthorized" not in text, (
                f"{label} self-claim must not render PermissionDenied "
                f"(existence oracle); got {text!r}"
            )
            assert "not found" in text.lower(), (
                f"{label} self-claim should render as NotFound; "
                f"got {text!r}"
            )

        # Load-bearing: both must be BYTE IDENTICAL after masking the
        # (necessarily different) task_id each supplied.
        masked = [
            nonexistent_text.replace(nonexistent, "<TASK>"),
            foreign_text.replace(foreign, "<TASK>"),
        ]
        assert masked[0] == masked[1], (
            "nonexistent / foreign-assigned self-claim responses must be "
            f"identical after masking the id; got {masked!r}"
        )

        # The foreign task must be untouched.
        assert _task_state(foreign) == (bob.agent_id, "pending")


# ── BL-R18-1: terminal-sink on the assign axis ───────────────────


@pytest.mark.parametrize("terminal_status", ["completed", "cancelled", "failed"])
async def test_worker_cannot_self_claim_terminal_task(
    tmp_path, terminal_status: str
) -> None:
    """A worker must NOT be able to self-claim a TERMINAL but unassigned
    task (which would let them re-execute finished work). It renders as
    NotFound (indistinguishable phantom) and the task stays terminal +
    unassigned."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        terminal_task = _seed_task(
            title="already done",
            assigned_to=None,
            status=terminal_status,
        )

        res = await alice.call(
            "assign_task",
            {"agent_token": alice.token, "task_ids": [terminal_task]},
        )
        text = res[0].text

        assert "not found" in text.lower(), (
            f"terminal task self-claim must render as NotFound; got {text!r}"
        )
        # Not claimed: still unassigned, still terminal.
        assert _task_state(terminal_task) == (None, terminal_status), (
            f"terminal task must not be claimed; got {_task_state(terminal_task)!r}"
        )


# ── Regression: the genuinely-claimable path still works ─────────


async def test_worker_self_claim_claimable_task_succeeds(tmp_path) -> None:
    """A worker self-claiming a task that genuinely exists, is
    unassigned, is non-terminal, and whose required_capabilities the
    worker satisfies (here: none required) must still succeed."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        task_id = _seed_task(title="up for grabs", assigned_to=None)

        res = await alice.call(
            "assign_task",
            {"agent_token": alice.token, "task_ids": [task_id]},
        )
        text = res[0].text
        assert "Unauthorized" not in text and "not found" not in text.lower(), (
            f"claimable self-claim must succeed; got {text!r}"
        )
        assert _task_state(task_id) == (alice.agent_id, "pending")


# ── Admin/manager keep informative errors (no phantom collapse) ──


async def test_admin_gets_informative_conflict_for_foreign_task(
    tmp_path,
) -> None:
    """An admin (tasks.assign) assigning an already-assigned task still
    gets the real informative Conflict — the phantom-NotFound collapse
    is ONLY for the non-admin self-claim oracle."""
    async with mcp_session(tmp_path) as admin:
        bob = await admin.create_worker("bob")
        carol = await admin.create_worker("carol")
        foreign = _seed_task(title="bob's task", assigned_to=bob.agent_id)

        res = await admin.call(
            "assign_task",
            {"agent_token": carol.token, "task_ids": [foreign]},
        )
        text = res[0].text
        assert "not found" not in text.lower(), (
            f"admin must get the informative Conflict, not phantom NotFound; "
            f"got {text!r}"
        )
        assert "already assigned" in text.lower(), (
            f"admin should see the informative already-assigned Conflict; "
            f"got {text!r}"
        )


# PR5 retired the structured capability-tag gate, so
# ``test_admin_gets_informative_permission_denied_for_caps`` was removed —
# there is no caps-mismatch PermissionDenied to assert any more.


async def test_admin_blocked_from_assigning_terminal_task_informatively(
    tmp_path,
) -> None:
    """Terminal is a sink on the assign axis for admins too — but with
    an INFORMATIVE error (not the phantom NotFound). Mirrors how the
    status-axis terminal guard blocks everyone."""
    async with mcp_session(tmp_path) as admin:
        eve = await admin.create_worker("eve")
        terminal_task = _seed_task(
            title="already done",
            assigned_to=None,
            status="completed",
        )

        res = await admin.call(
            "assign_task",
            {"agent_token": eve.token, "task_ids": [terminal_task]},
        )
        text = res[0].text
        assert "not found" not in text.lower(), (
            f"admin terminal-block must be informative, not phantom NotFound; "
            f"got {text!r}"
        )
        assert "terminal" in text.lower(), (
            f"admin should see an informative terminal-state error; "
            f"got {text!r}"
        )
        # Untouched.
        assert _task_state(terminal_task) == (None, "completed")
