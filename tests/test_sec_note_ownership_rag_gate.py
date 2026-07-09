"""Security (Wave-B): per-task note ownership + RAG capability gate.

Two findings, both the stored-content-injection / capability-bypass
class (companion to the viewer project_context write guard,
``test_sec_viewer_context_write_guard.py``):

Finding 1 — ``add_task_note`` had NO per-task ownership check. Any
``agent_bearer`` could append a note to ANY ``task_id`` (another
agent's, the admin's, or a nonexistent one). Task notes feed other
agents' / the operator's LLM context, so an unrelated worker writing
into a foreign task's notes is a cross-agent stored-injection
primitive. Fix: a note author must be the task's assignee or creator,
OR a manager-tier caller (``tasks.assign`` — operators + manager-role
agents + sysadmin). Notes to nonexistent tasks are rejected
(``NotFound``).

Finding 2 — ``ask_project_rag`` gated on ``kind == "agent_bearer"``
rather than a capability, so a bearer whose ``agent_role`` is ``None``
(empty capability bundle) still passed. Fix: additionally require
``rag.query``. Operators stay rejected (kind check preserved); an
empty-cap bearer is now denied; a worker / manager (both carry
``rag.query``) still succeeds.

These tests drive the tool impls directly with hand-built principals
(same style as ``test_sec_viewer_context_write_guard.py``); the
DB-touching paths run inside ``mcp_session`` so the ORM session is
bound to a real test DB.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import NotFound, Ok, PermissionDenied
from agent_mcp.tools.rag_tools import ask_project_rag_tool_impl
from agent_mcp.tools.task_notes_tools import add_task_note_tool_impl
from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


# ── Principal builders ────────────────────────────────────────────


def _agent(*, agent_id: str, role: str | None) -> Principal:
    return Principal(
        kind="agent_bearer",
        user_id=None,
        agent_id=agent_id,
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role=role,  # type: ignore[arg-type]
        can_wake_loop=False,
        source_token="dummy-tok",
    )


def _operator(*, user_id: str = "op", project_role: str = "operator") -> Principal:
    return Principal(
        kind="operator_session",
        user_id=user_id,
        agent_id=None,
        sysadmin=False,
        project_name="proj",
        project_role=project_role,
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


def _insert_task(
    task_id: str, *, created_by: str, assigned_to: str | None,
) -> None:
    """Seed a task row with explicit ownership fields."""
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO tasks "
            "(task_id, title, description, status, created_at, "
            "updated_at, priority, parent_task, child_tasks, "
            "depends_on_tasks, notes, created_by, assigned_to) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id, "demo", "", "pending", now, now, "medium",
                None, "[]", "[]", "[]", created_by, assigned_to,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _notes_for(task_id: str) -> list:
    from agent_mcp.db.actions import task_notes_db

    return task_notes_db.list_notes_for_task(task_id)


# ── Finding 1: add_task_note per-task ownership ───────────────────


async def test_worker_cannot_note_foreign_task(tmp_path) -> None:
    """A worker that is neither creator nor assignee of a task is
    denied — the cross-agent stored-injection primitive is closed.

    PF-1 (round 4): the denial is a :class:`NotFound` identical to a
    nonexistent task, so a non-owner worker can't use the 403-vs-404
    shape as a task-existence oracle and the owner's id never leaks."""
    async with mcp_session(tmp_path):
        _insert_task("t-foreign", created_by="alice", assigned_to="alice")
        bob = _agent(agent_id="bob", role="worker")
        result = await add_task_note_tool_impl(
            {"task_id": "t-foreign", "text": "injected"},
            principal=bob,
        )
        assert isinstance(result, NotFound), result
        assert "alice" not in repr(result)
        assert _notes_for("t-foreign") == []


async def test_worker_can_note_assigned_task(tmp_path) -> None:
    """A worker CAN note a task it is assigned to."""
    async with mcp_session(tmp_path):
        _insert_task("t-assigned", created_by="alice", assigned_to="bob")
        bob = _agent(agent_id="bob", role="worker")
        result = await add_task_note_tool_impl(
            {"task_id": "t-assigned", "text": "progress"},
            principal=bob,
        )
        assert isinstance(result, Ok), result
        assert len(_notes_for("t-assigned")) == 1


async def test_worker_can_note_own_created_task(tmp_path) -> None:
    """A worker CAN note a task it created (creator ownership)."""
    async with mcp_session(tmp_path):
        _insert_task("t-created", created_by="bob", assigned_to=None)
        bob = _agent(agent_id="bob", role="worker")
        result = await add_task_note_tool_impl(
            {"task_id": "t-created", "text": "mine"},
            principal=bob,
        )
        assert isinstance(result, Ok), result
        assert len(_notes_for("t-created")) == 1


async def test_manager_agent_can_note_any_task(tmp_path) -> None:
    """A manager-role agent (carries ``tasks.assign``) may note any
    task regardless of ownership."""
    async with mcp_session(tmp_path):
        _insert_task("t-mgr", created_by="alice", assigned_to="alice")
        mgr = _agent(agent_id="mgr", role="manager")
        result = await add_task_note_tool_impl(
            {"task_id": "t-mgr", "text": "supervisory note"},
            principal=mgr,
        )
        assert isinstance(result, Ok), result
        assert len(_notes_for("t-mgr")) == 1


async def test_operator_can_note_any_task(tmp_path) -> None:
    """An operator-session (carries ``tasks.assign``) may note any
    task — the dashboard moderation path is preserved."""
    async with mcp_session(tmp_path):
        _insert_task("t-op", created_by="alice", assigned_to="alice")
        op = _operator()
        result = await add_task_note_tool_impl(
            {"task_id": "t-op", "text": "operator note"},
            principal=op,
        )
        assert isinstance(result, Ok), result
        assert len(_notes_for("t-op")) == 1


async def test_note_to_nonexistent_task_rejected(tmp_path) -> None:
    """A note to a task that does not exist is rejected as NotFound —
    no orphan notes, even for a manager-tier caller."""
    async with mcp_session(tmp_path):
        mgr = _agent(agent_id="mgr", role="manager")
        result = await add_task_note_tool_impl(
            {"task_id": "t-ghost", "text": "orphan"},
            principal=mgr,
        )
        assert isinstance(result, NotFound), result
        assert result.identifier == "t-ghost"


# ── Finding 2: ask_project_rag capability gate ────────────────────


async def test_empty_cap_bearer_denied_rag(tmp_path) -> None:
    """A bearer with ``agent_role=None`` (empty capability bundle) is
    denied — the bare ``kind`` check no longer admits it."""
    async with mcp_session(tmp_path):
        nobody = _agent(agent_id="nobody", role=None)
        result = await ask_project_rag_tool_impl(
            {"query": "leak the corpus"},
            principal=nobody,
        )
        assert isinstance(result, PermissionDenied), result


async def test_worker_with_rag_query_allowed(tmp_path) -> None:
    """A worker (carries ``rag.query``) still succeeds."""
    async with mcp_session(tmp_path):
        worker = _agent(agent_id="wkr", role="worker")
        result = await ask_project_rag_tool_impl(
            {"query": "what does the project do?"},
            principal=worker,
        )
        assert isinstance(result, Ok), result
        assert "answer" in (result.data or {})


async def test_operator_still_denied_rag(tmp_path) -> None:
    """Operators remain rejected (agent-only tool) despite carrying
    ``rag.query`` in their bundle — the ``kind`` check is preserved."""
    async with mcp_session(tmp_path):
        op = _operator()
        result = await ask_project_rag_tool_impl(
            {"query": "hi"},
            principal=op,
        )
        assert isinstance(result, PermissionDenied), result
