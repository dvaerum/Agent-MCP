"""SEC-R2: terminate-reconcile TOCTOU across the RAG await.

Background
----------
Both ``assign_task`` (Mode 1: create-single-task-and-assign) and
``create_self_task`` route the new task through the
``validate_task_placement`` RAG pre-check. That call is ``await``-ed —
it yields the event loop. A concurrent ``terminate_agent`` can commit
in that window, *after* the tool verified the target agent was
assignable but *before* the task row is INSERTed.

The result is a task assigned to a terminated agent: unreachable work
that also attributes to a revoked identity. Classic time-of-check /
time-of-use.

The fix re-runs the assignability gate (``_agent_assignable``)
in-transaction, immediately before the INSERT, in both paths — closing
the window the RAG yield opens. Mode 1 is also made to run the
``_agent_assignable`` gate explicitly (previously it only did an inline
``status != 'terminated'`` DB lookup on the not-in-memory branch, so a
terminated-but-warm agent skipped the status check entirely).

These tests reproduce the race deterministically: a stand-in for
``validate_task_placement`` commits a terminate of the target agent
during its ``await`` (via a *separate* DB connection, exactly as a
concurrent ``terminate_agent`` request would), then returns
``approved``. The tool must reject the assignment and persist no task.

Consistency item covered here too: an operator/manager caller must not
be able to forge ``created_by`` by smuggling ``_worker_created_by``
into the raw arguments dict (Mode 0). The authorizer pops it on the
admin/manager branch.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


# --- Helpers ---------------------------------------------------------------


def _terminate_in_db(agent_id: str) -> None:
    """Commit ``status='terminated'`` for ``agent_id`` on a *fresh*
    connection — mirrors a concurrent ``terminate_agent`` landing while
    the calling tool is parked on the RAG await."""
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE agents SET status = 'terminated' WHERE agent_id = ?",
            (agent_id,),
        )
        conn.commit()
    finally:
        conn.close()

    # Also drop the in-memory caches a real terminate would clear, so
    # the scenario is faithful (the DB gate is what the fix relies on,
    # but leaving stale cache entries would be an unrealistic setup).
    from agent_mcp.core import globals as g

    g.agent_working_dirs.pop(agent_id, None)
    for tkn, data in list(g.active_agents.items()):
        if data.get("agent_id") == agent_id:
            g.active_agents.pop(tkn, None)


def _make_terminating_validator(victim_agent_id: str) -> Any:
    """Async stand-in for ``validate_task_placement`` that terminates
    ``victim_agent_id`` *during* its await, then returns ``approved``
    so the tool proceeds toward the INSERT."""

    async def _fake_validate(
        title: str,
        description: str,
        parent_task_id: str | None,
        depends_on_tasks: list[str] | None,
        created_by: str,
        auth_token: str,
    ) -> Dict[str, Any]:
        # The concurrent terminate lands here, in the RAG yield window.
        _terminate_in_db(victim_agent_id)
        return {
            "status": "approved",
            "suggestions": {
                "parent_task": None,
                "dependencies": [],
                "reasoning": "(approved)",
            },
            "duplicates": [],
            "message": "Placement approved.",
        }

    return _fake_validate


def _make_approving_validator() -> Any:
    """Async stand-in that approves without side effects."""

    async def _fake_validate(
        title: str,
        description: str,
        parent_task_id: str | None,
        depends_on_tasks: list[str] | None,
        created_by: str,
        auth_token: str,
    ) -> Dict[str, Any]:
        return {
            "status": "approved",
            "suggestions": {
                "parent_task": None,
                "dependencies": [],
                "reasoning": "(approved)",
            },
            "duplicates": [],
            "message": "Placement approved.",
        }

    return _fake_validate


async def _seed_root(admin, holder_token: str) -> str:
    """Create the single root task (assigned to ``holder_token``) and
    return its task_id — a parent for the victim tasks the tests file."""
    result = await admin.assert_tool_succeeds(
        "assign_task",
        {
            "agent_token": holder_token,
            "task_title": "root task",
            "task_description": "the one legitimate root",
        },
    )
    return re.search(r"task_[a-f0-9]+", result[0].text).group(0)


def _first_text(result: List[Any]) -> str:
    if not result:
        return ""
    return getattr(result[0], "text", "") or ""


def _count_tasks_for(agent_id: str, title: str) -> int:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM tasks "
            "WHERE assigned_to = ? AND title = ?",
            (agent_id, title),
        )
        return cur.fetchone()["n"]
    finally:
        conn.close()


# --- assign_task Mode 1: TOCTOU across the RAG await -----------------------


async def test_assign_task_mode1_rejects_terminate_during_rag_await(
    tmp_path, monkeypatch,
) -> None:
    """A target agent terminated during the RAG await must NOT receive
    the task. The in-transaction recheck before INSERT closes the
    window; no task row assigned to the terminated agent is persisted.
    """
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        parent_id = await _seed_root(admin, alice.token)

        bob = await admin.create_worker("bob")
        monkeypatch.setattr(
            "agent_mcp.tools.task_tools.validate_task_placement",
            _make_terminating_validator("bob"),
        )

        victim_title = "task racing a terminate"
        result = await admin.call(
            "assign_task",
            {
                "agent_token": bob.token,
                "task_title": victim_title,
                "task_description": "bob is terminated mid-flight",
                "parent_task_id": parent_id,
            },
        )
        text = _first_text(result)

        # Must be rejected (isError / Error text), NOT a silent success.
        assert getattr(admin, "_last_is_error", False) or text.startswith(
            "Error:"
        ), f"expected rejection, got: {text!r}"

        # Authoritative check: no task assigned to the terminated agent.
        assert _count_tasks_for("bob", victim_title) == 0, (
            "a task was assigned to an agent terminated during the RAG "
            "await — the TOCTOU window is still open"
        )


async def test_assign_task_mode1_rejects_terminated_agent_explicit_gate(
    tmp_path, monkeypatch,
) -> None:
    """Mode 1 must run ``_agent_assignable`` explicitly: an agent whose
    DB row is ``terminated`` (but which is still warm in memory) must be
    rejected up-front, independent of the RAG race."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        parent_id = await _seed_root(admin, alice.token)

        bob = await admin.create_worker("bob")
        # Warm the in-memory presence set so the legacy not-in-memory
        # inline DB lookup would be skipped, then terminate only in DB.
        from agent_mcp.core import globals as g

        g.agent_working_dirs["bob"] = "/tmp"
        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE agents SET status = 'terminated' WHERE agent_id = ?",
                ("bob",),
            )
            conn.commit()
        finally:
            conn.close()

        monkeypatch.setattr(
            "agent_mcp.tools.task_tools.validate_task_placement",
            _make_approving_validator(),
        )

        victim_title = "task for a terminated-but-warm agent"
        result = await admin.call(
            "assign_task",
            {
                "agent_token": bob.token,
                "task_title": victim_title,
                "task_description": "should be rejected up-front",
                "parent_task_id": parent_id,
            },
        )
        text = _first_text(result)
        assert getattr(admin, "_last_is_error", False) or text.startswith(
            "Error:"
        ), f"expected rejection of terminated agent, got: {text!r}"
        assert _count_tasks_for("bob", victim_title) == 0, (
            "Mode 1 assigned a task to a terminated agent that was still "
            "warm in memory — the explicit _agent_assignable gate is "
            "missing"
        )


# --- create_self_task: TOCTOU across the RAG await -------------------------


async def test_create_self_task_rejects_terminate_during_rag_await(
    tmp_path, monkeypatch,
) -> None:
    """The self-task path must also re-check assignability before the
    INSERT: an agent terminated during its own create_self_task RAG
    await must not end up with a freshly-created task."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        parent_id = await _seed_root(admin, alice.token)

        monkeypatch.setattr(
            "agent_mcp.tools.task_tools.validate_task_placement",
            _make_terminating_validator("alice"),
        )

        victim_title = "self-task racing a terminate"
        result = await alice.call(
            "create_self_task",
            {
                "task_title": victim_title,
                "task_description": "alice terminated mid-flight",
                "parent_task_id": parent_id,
            },
        )
        text = _first_text(result)
        assert getattr(alice, "_last_is_error", False) or text.startswith(
            "Error:"
        ), f"expected rejection, got: {text!r}"
        assert _count_tasks_for("alice", victim_title) == 0, (
            "create_self_task persisted a task for an agent terminated "
            "during its RAG await — the TOCTOU window is still open"
        )


# --- Consistency: manager/admin cannot forge _worker_created_by -----------


async def test_admin_cannot_forge_worker_created_by(tmp_path) -> None:
    """An operator/manager caller must not be able to smuggle a forged
    ``_worker_created_by`` into the arguments dict to falsify the
    ``created_by`` provenance on a Mode-0 unassigned task. The authorizer
    pops the key on the admin/manager branch, so attribution stays
    ``admin``."""
    from agent_mcp.tools.task_tools import assign_task_tool_impl
    from agent_mcp.core.principal import Principal

    async with mcp_session(tmp_path) as admin:
        forged_title = "unassigned task with forged provenance"
        principal = Principal(
            kind="agent_bearer",
            user_id="op",
            agent_id="admin",
            sysadmin=True,
            project_name="harness",
            project_role="operator",
            agent_role="manager",
            can_wake_loop=False,
            source_token=admin.admin_token,
        )
        result = await assign_task_tool_impl(
            {
                "token": admin.admin_token,
                # No agent_token → Mode 0 (unassigned task creation).
                "task_title": forged_title,
                "task_description": "operator smuggling _worker_created_by",
                # Forged provenance the authorizer must strip.
                "_worker_created_by": "victim",
            },
            principal=principal,
        )
        # Should succeed (root task creation by operator).
        text = getattr(result, "message", None) or (
            result[0].text if isinstance(result, list) else str(result)
        )
        assert "Error" not in (text or ""), (
            f"operator Mode-0 create should succeed; got: {text!r}"
        )

        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT created_by FROM tasks WHERE title = ?",
                (forged_title,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None, "the unassigned task should have persisted"
        assert row["created_by"] == "admin", (
            "operator caller forged created_by via _worker_created_by; "
            f"expected 'admin', got {row['created_by']!r}"
        )
