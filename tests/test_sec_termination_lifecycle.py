"""Security regression: termination / reserved-name agent lifecycle.

Wave-B pentest findings (owner-authorized, defensive):

1. [MED] Reserved-name guard. Many gates privilege an agent purely by
   NAME — ``agent_id == "admin"`` (messaging, read-any-inbox,
   authorize) and ``agent_id.lower().startswith("admin")``
   (``task_tools``/``state``). An operator must not be able to mint a
   worker-role agent literally named ``admin`` (or ``admin-x``) and
   inherit those name-keyed privileges. The repository is the single
   owner of the agent_id invariant, so the guard lives in
   ``agent_repo.create`` (with a clean ``Invalid`` mirrored in the
   ``register_agent`` tool for operator-facing UX).

2. [MED] Terminate must not orphan tasks. ``terminate_agent`` flips
   the agent row to ``terminated`` but historically left
   ``tasks.assigned_to`` pointing at the dead agent, so active work
   was stranded on an agent that will never run it. Terminate now
   reconciles the agent's *active* tasks back to unassigned in the
   same transaction. Terminal tasks (completed/cancelled/failed) keep
   their attribution — terminate is a soft-delete, the row still
   exists, and reverting a completed task to unassigned would be
   destructive.

3. [LOW-MED] Terminated manager bearer must not be operator-tier on
   the backend REST path. ``deps._is_operator_tier_bearer`` admitted
   any ``manager``/``admin`` role row without a status check, and
   ``get_agent_by_token`` returns terminated rows. Same
   termination-revocation class as #275/#280, on the REST dep path.
"""

from __future__ import annotations

import datetime
import secrets

import pytest

from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import Invalid, Ok
from tests.harness import make_principal, mcp_session

pytestmark = pytest.mark.asyncio


def _operator_principal(project_name: str = "demo-project") -> Principal:
    return make_principal(
        kind="operator_session",
        user_id="test-operator",
        agent_id=None,
        sysadmin=False,
        project_name=project_name,
        project_role="operator",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


def _insert_task(
    *, task_id: str, assigned_to: str, status: str = "pending",
    created_by: str = "admin",
) -> None:
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import Task

    from tests.conftest import existing_root_task_id

    # R15-BL-1: chain under the single root (first seed = root, rest are
    # children). Terminate unassigns by assignee, not by hierarchy.
    parent = existing_root_task_id()

    now = datetime.datetime.now().isoformat()
    with get_session() as session:
        session.add(
            Task(
                task_id=task_id,
                title=f"task {task_id}",
                description=None,
                assigned_to=assigned_to,
                created_by=created_by,
                status=status,
                priority="medium",
                created_at=now,
                updated_at=now,
                parent_task=parent,
            )
        )
        session.commit()


def _task_row(task_id: str) -> dict:
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import Task

    with get_session() as session:
        row = (
            session.query(Task)
            .filter(Task.task_id == task_id)
            .one_or_none()
        )
        assert row is not None, f"task {task_id} vanished"
        return {"assigned_to": row.assigned_to, "status": row.status}


def _insert_agent(
    *, agent_id: str, token: str, status: str = "active",
    agent_role: str = "manager",
) -> None:
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import Agent

    now = datetime.datetime.now().isoformat()
    with get_session() as session:
        session.add(
            Agent(
                token=token,
                agent_id=agent_id,
                created_at=now,
                status=status,
                current_task=None,
                working_directory="/tmp/wd",
                color="#abcdef",
                terminated_at=now if status == "terminated" else None,
                updated_at=now,
                aoe_session_id=None,
                agent_role=agent_role,
            )
        )
        session.commit()


# ── 1. reserved-name guard ───────────────────────────────────────────


@pytest.mark.parametrize("bad_name", ["admin", "admin-x", "adminx"])
async def test_repo_create_rejects_reserved_admin_name(tmp_path, bad_name):
    """``agent_repo.create`` refuses any valid-slug agent_id that starts
    with ``admin`` — those names are privileged by gates that key on the
    agent_id string. (Uppercase variants are already rejected earlier by
    the slug regex, so this pins the reserved-name path specifically.)"""
    from agent_mcp.repositories import agent_repo

    async with mcp_session(tmp_path):
        with pytest.raises(ValueError) as exc:
            agent_repo.create(
                token=secrets.token_hex(8),
                agent_id=bad_name,
                working_directory="/tmp/wd",
                agent_role="worker",
            )
    assert "reserved" in str(exc.value).lower()


async def test_repo_create_allows_normal_name(tmp_path):
    """A non-reserved slug still creates fine — the guard is narrow."""
    from agent_mcp.repositories import agent_repo

    async with mcp_session(tmp_path):
        row = agent_repo.create(
            token=secrets.token_hex(8),
            agent_id="worker-1",
            working_directory="/tmp/wd",
            agent_role="worker",
        )
    assert row["agent_id"] == "worker-1"


async def test_register_agent_rejects_reserved_admin_name(tmp_path):
    """The operator-facing ``register_agent`` tool returns a clean
    ``Invalid`` (not a generic DB failure) for a reserved name."""
    from agent_mcp.tools.admin_tools import register_agent_tool_impl

    async with mcp_session(tmp_path):
        result = await register_agent_tool_impl(
            {"name": "admin", "role": "worker", "host": "https://h.x"},
            principal=_operator_principal(),
        )
    assert isinstance(result, Invalid), f"expected Invalid, got {result!r}"
    assert "reserved" in result.message.lower()


async def test_register_agent_allows_normal_name(tmp_path):
    """Sanity: a normal name still registers through the tool."""
    from agent_mcp.tools.admin_tools import register_agent_tool_impl

    async with mcp_session(tmp_path):
        result = await register_agent_tool_impl(
            {"name": "wkr-ok", "role": "worker", "host": "https://h.x"},
            principal=_operator_principal(),
        )
    assert isinstance(result, Ok), f"expected Ok, got {result!r}"


# ── 2. terminate reconciles assigned tasks ───────────────────────────


async def test_terminate_unassigns_active_tasks(tmp_path):
    """After ``terminate_agent`` the agent's active (pending/in_progress)
    tasks are unassigned — no active task references a terminated
    agent."""
    from agent_mcp.tools.admin_tools import (
        register_agent_tool_impl,
        terminate_agent_tool_impl,
    )

    async with mcp_session(tmp_path):
        reg = await register_agent_tool_impl(
            {"name": "wkr-term", "role": "worker", "host": "https://h.x"},
            principal=_operator_principal(),
        )
        assert isinstance(reg, Ok)
        agent_id = reg.data["agent_id"]

        _insert_task(task_id="t-pending", assigned_to=agent_id,
                     status="pending")
        _insert_task(task_id="t-inprog", assigned_to=agent_id,
                     status="in_progress")

        term = await terminate_agent_tool_impl(
            {"agent_id": agent_id}, principal=_operator_principal(),
        )
        assert isinstance(term, Ok)

        for tid in ("t-pending", "t-inprog"):
            row = _task_row(tid)
            assert row["assigned_to"] is None, (
                f"{tid} still references terminated agent {agent_id!r}"
            )
            assert row["status"] == "unassigned"


async def test_terminate_preserves_completed_task_attribution(tmp_path):
    """Terminal tasks keep their ``assigned_to`` — terminate is a
    soft-delete (the row still exists); reverting a completed task to
    unassigned would be destructive and lose completion history."""
    from agent_mcp.tools.admin_tools import (
        register_agent_tool_impl,
        terminate_agent_tool_impl,
    )

    async with mcp_session(tmp_path):
        reg = await register_agent_tool_impl(
            {"name": "wkr-done", "role": "worker", "host": "https://h.x"},
            principal=_operator_principal(),
        )
        assert isinstance(reg, Ok)
        agent_id = reg.data["agent_id"]

        _insert_task(task_id="t-done", assigned_to=agent_id,
                     status="completed")

        term = await terminate_agent_tool_impl(
            {"agent_id": agent_id}, principal=_operator_principal(),
        )
        assert isinstance(term, Ok)

        row = _task_row("t-done")
        assert row["assigned_to"] == agent_id
        assert row["status"] == "completed"


# ── 3. terminated manager bearer is not operator-tier (REST dep) ─────


async def test_terminated_manager_bearer_not_operator_tier(tmp_path):
    """A terminated manager's bearer must NOT be operator-tier on the
    backend REST dep — same termination-revocation class as #275/#280."""
    from agent_mcp.app.deps import _is_operator_tier_bearer

    token = secrets.token_hex(16)
    async with mcp_session(tmp_path):
        _insert_agent(agent_id="mgr-dead", token=token,
                      status="terminated", agent_role="manager")
        assert _is_operator_tier_bearer(token) is False


async def test_active_manager_bearer_is_operator_tier(tmp_path):
    """Sanity: an active manager bearer stays operator-tier."""
    from agent_mcp.app.deps import _is_operator_tier_bearer

    token = secrets.token_hex(16)
    async with mcp_session(tmp_path):
        _insert_agent(agent_id="mgr-live", token=token,
                      status="active", agent_role="manager")
        assert _is_operator_tier_bearer(token) is True
