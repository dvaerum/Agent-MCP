"""Unit-of-work migration of the admin + file-metadata tool mutations
(architecture-deepening D3).

D0 (PR #400) introduced the ``unit_of_work()`` seam and migrated
``delete_task``. D3 migrates the remaining raw-sqlite tool mutations:

* ``admin_tools.register_agent_tool_impl`` — agent INSERT + its
  ``registered_agent`` DB-audit row commit atomically on ``u.cursor``;
  the cache upsert and the in-memory ``register_agent`` audit are
  registered post-commit (emit-iff-commit).
* ``admin_tools.terminate_agent_tool_impl`` — status flip + task
  unassign + ``terminated_agent`` audit atomic; cache eviction /
  reconcile / in-memory ``terminate_agent`` audit post-commit.
* ``file_metadata_tools.update_file_metadata_tool_impl`` — the
  ``file_metadata`` upsert + its ``updated_file_metadata`` DB-audit row
  commit atomically on ``u.cursor``.

Two guarantees per mutation, mirroring ``tests/test_unit_of_work.py``:

1. **committed path** — a clean call writes the row(s) AND both audit
   sinks (DB ``agent_actions`` + in-memory ``g.audit_log``) AND runs the
   post-commit cache hooks.
2. **rollback fires zero side effects** — an exception before commit
   rolls back every DB write and runs NO post-commit hook. (The
   file-metadata in-memory ``log_audit`` is intentionally *pre-scope* —
   it is not a uow-registered effect — so only the DB writes are pinned
   to zero there.)
"""

from __future__ import annotations

import pytest

from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import Failed, Ok
from tests.harness import make_principal, mcp_session

pytestmark = pytest.mark.asyncio


# --- helpers ---------------------------------------------------------------


def _operator_principal(user_id: str = "d3-operator") -> Principal:
    return make_principal(
        kind="operator_session",
        user_id=user_id,
        agent_id=None,
        sysadmin=False,
        project_name=None,
        project_role="operator",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


def _agent_row_exists(agent_id: str) -> bool:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM agents WHERE agent_id = ?", (agent_id,)
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def _action_rows(action_type: str, agent_id: str) -> int:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_actions "
            "WHERE action_type = ? AND agent_id = ?",
            (action_type, agent_id),
        ).fetchone()
    finally:
        conn.close()
    return row["n"]


def _file_meta_row(filepath: str):
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        return conn.execute(
            "SELECT metadata, updated_by FROM file_metadata WHERE filepath = ?",
            (filepath,),
        ).fetchone()
    finally:
        conn.close()


def _audit_actions_since(before: int) -> list[str]:
    from agent_mcp.core import globals as g

    return [e.get("action") for e in g.audit_log[before:]]


# --- register_agent -------------------------------------------------------


async def test_register_agent_committed_writes_and_both_audit_sinks(tmp_path):
    """A clean register commits the agent row + its DB-audit row and
    flushes the post-commit cache upsert + in-memory audit."""
    async with mcp_session(tmp_path):
        from agent_mcp.core import globals as g
        from agent_mcp.tools.admin_tools import register_agent_tool_impl

        audit_before = len(g.audit_log)

        result = await register_agent_tool_impl(
            {"agent_id": "d3-worker"},
            principal=_operator_principal(),
        )

        assert isinstance(result, Ok), f"expected Ok, got {result!r}"
        token = result.data["token"]

        # DB row committed (authoritative read).
        assert _agent_row_exists("d3-worker")
        # DB audit sink — past-tense action_type.
        assert _action_rows("registered_agent", "d3-operator") >= 1, (
            "register must write a registered_agent agent_actions row"
        )
        # In-memory audit sink — present-tense action, kept distinct.
        assert "register_agent" in _audit_actions_since(audit_before), (
            "register must append a register_agent g.audit_log entry"
        )
        # Post-commit cache upsert ran.
        assert token in g.active_agents, (
            "register must upsert the new agent into g.active_agents"
        )
        assert "d3-worker" in g.agent_working_dirs


async def test_register_agent_rollback_fires_zero_side_effects(
    tmp_path, monkeypatch
):
    """If the in-transaction DB-audit write raises before commit, the
    uow rolls back: no agent row, no agent_actions row, no g.audit_log
    entry, no cache upsert — and the tool returns Failed."""
    async with mcp_session(tmp_path):
        from agent_mcp.core import globals as g
        from agent_mcp.tools import admin_tools

        audit_before = len(g.audit_log)

        def _boom(*args, **kwargs):
            raise RuntimeError("audit sink exploded mid-transaction")

        # log_agent_action_to_db runs AFTER the agent INSERT and BEFORE
        # commit / on_commit registration — the perfect rollback trigger.
        monkeypatch.setattr(admin_tools, "log_agent_action_to_db", _boom)

        result = await admin_tools.register_agent_tool_impl(
            {"agent_id": "d3-rollback"},
            principal=_operator_principal(),
        )

        assert isinstance(result, Failed), f"expected Failed, got {result!r}"
        # DB write rolled back.
        assert not _agent_row_exists("d3-rollback"), (
            "rolled-back register must not leave an agents row"
        )
        # No DB audit row (the raising write itself was undone).
        assert _action_rows("registered_agent", "d3-operator") == 0
        # No in-memory audit (on_commit never registered).
        assert _audit_actions_since(audit_before) == []
        # No cache upsert.
        assert "d3-rollback" not in g.agent_working_dirs


# --- terminate_agent ------------------------------------------------------


async def test_terminate_agent_committed_writes_and_audits(tmp_path):
    """Terminating a registered agent commits the status flip + audit
    row and flushes the post-commit cache eviction + in-memory audit."""
    async with mcp_session(tmp_path):
        from agent_mcp.core import globals as g
        from agent_mcp.tools.admin_tools import (
            register_agent_tool_impl,
            terminate_agent_tool_impl,
        )

        reg = await register_agent_tool_impl(
            {"agent_id": "d3-term"}, principal=_operator_principal(),
        )
        assert isinstance(reg, Ok)
        token = reg.data["token"]
        assert token in g.active_agents

        audit_before = len(g.audit_log)
        result = await terminate_agent_tool_impl(
            {"agent_id": "d3-term"}, principal=_operator_principal(),
        )

        assert isinstance(result, Ok), f"expected Ok, got {result!r}"
        # DB audit sink.
        assert _action_rows("terminated_agent", "admin") >= 1, (
            "terminate must write a terminated_agent agent_actions row"
        )
        # In-memory audit sink.
        assert "terminate_agent" in _audit_actions_since(audit_before), (
            "terminate must append a terminate_agent g.audit_log entry"
        )
        # Post-commit cache eviction ran.
        assert token not in g.active_agents, (
            "terminate must evict the agent from g.active_agents"
        )


# --- update_file_metadata -------------------------------------------------


async def test_update_file_metadata_committed_writes_and_audit(tmp_path):
    """A clean update commits the file_metadata row + its DB-audit row,
    and records the in-memory audit."""
    async with mcp_session(tmp_path):
        from agent_mcp.core import globals as g
        from agent_mcp.tools.file_metadata_tools import (
            update_file_metadata_tool_impl,
        )

        filepath = "/tmp/d3-uow-meta.txt"
        audit_before = len(g.audit_log)

        result = await update_file_metadata_tool_impl(
            {"filepath": filepath, "metadata": {"purpose": "d3"}},
            principal=_operator_principal("op-d3"),
        )

        assert isinstance(result, Ok), f"expected Ok, got {result!r}"
        normalized = result.data["filepath"]

        # Row committed (authoritative read).
        row = _file_meta_row(normalized)
        assert row is not None
        import json as _json

        assert _json.loads(row["metadata"]) == {"purpose": "d3"}
        # DB audit sink — past-tense action_type.
        assert _action_rows("updated_file_metadata", row["updated_by"]) >= 1
        # In-memory audit sink — present-tense action.
        assert "update_file_metadata" in _audit_actions_since(audit_before)


async def test_update_file_metadata_rollback_fires_zero_db_effects(
    tmp_path, monkeypatch
):
    """If the in-transaction DB-audit write raises before commit, the
    uow rolls back the file_metadata upsert AND the audit row. (The
    in-memory log_audit is intentionally pre-scope, so it still fired —
    that is asserted to pin the deliberate design.)"""
    async with mcp_session(tmp_path):
        from agent_mcp.core import globals as g
        from agent_mcp.tools import file_metadata_tools

        filepath = "/tmp/d3-uow-meta-rollback.txt"
        audit_before = len(g.audit_log)

        def _boom(*args, **kwargs):
            raise RuntimeError("audit sink exploded mid-transaction")

        monkeypatch.setattr(
            file_metadata_tools, "log_agent_action_to_db", _boom
        )

        result = await file_metadata_tools.update_file_metadata_tool_impl(
            {"filepath": filepath, "metadata": {"purpose": "nope"}},
            principal=_operator_principal("op-d3"),
        )

        assert isinstance(result, Failed), f"expected Failed, got {result!r}"

        # Resolve the normalized path the tool would have used.
        from agent_mcp.tools.file_metadata_tools import _normalize_filepath

        normalized = _normalize_filepath(filepath, None)

        # DB write rolled back — no row, no DB-audit row.
        assert _file_meta_row(normalized) is None, (
            "rolled-back update must not leave a file_metadata row"
        )
        assert _action_rows("updated_file_metadata", "op-d3") == 0
        # The in-memory audit is pre-scope by design (behavior-preserving)
        # — it fired before the uow opened, so it is present.
        assert "update_file_metadata" in _audit_actions_since(audit_before), (
            "the in-memory audit is intentionally pre-scope and must fire"
        )
