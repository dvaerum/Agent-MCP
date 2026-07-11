"""Unit-of-work migration of the project_context write tools
(architecture-deepening arch-r4 candidate #6).

Before this migration, ``agent_mcp.tools.project_context_tools`` was
the ONLY tool module still opening raw ``SessionLocal()`` ORM sessions
in its write bodies and reaching THROUGH the session to grab the
underlying DBAPI cursor (``session.connection().connection.cursor()``)
so ``log_agent_action_to_db`` could land in the same transaction as the
content write — five call sites doing the identical drill-through
(``_single_update_inline``, ``_bulk_update_inline``,
``_create_context_inline``, ``backup_project_context_tool_impl``,
``delete_project_context_tool_impl``). This migrates all five onto
``unit_of_work()`` + the new ``project_context_repo`` (parameterized
SQL on the scope's cursor), mirroring the D3 migration in
``tests/test_uow_other_tools.py`` / ``test_uow_task_tools.py`` /
``test_uow_messaging.py``.

Two guarantees per mutation, mirroring those D3 tests:

1. **committed path** — a clean call writes the row AND the DB-audit
   sink (``agent_actions``).
2. **rollback fires zero DB effects** — an exception raised
   mid-transaction (after the content write, before the scope commits)
   rolls back BOTH the content write and the DB-audit row. Old code
   also avoided a *black-box-observable* partial write here (verified
   empirically: SQLAlchemy's ``Session.close()`` performs an implicit
   rollback on an uncommitted session even without an explicit
   ``session.rollback()`` call), so these are not bug-fix regression
   tests — they pin an invariant that is now STRUCTURAL (impossible to
   violate by construction, per ``unit_of_work``'s emit-iff-commit
   design) rather than incidental on SQLAlchemy internals a future
   edit could silently break.

``bulk_update_project_context`` additionally gets an
authorization-denial atomicity test extending
``tests/test_project_context_ownership.py::test_12`` with an
``agent_actions`` assertion the original test didn't make.
"""

from __future__ import annotations

import pytest

from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import Conflict, Failed, NotFound, Ok, PermissionDenied
from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


# --- helpers -----------------------------------------------------------


def _operator_principal(user_id: str = "r4-6-operator") -> Principal:
    return Principal(
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


def _worker_principal(agent_id: str) -> Principal:
    return Principal(
        kind="agent_bearer",
        user_id=None,
        agent_id=agent_id,
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role="worker",
        can_wake_loop=False,
        source_token=None,
    )


def _row(key: str) -> dict | None:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        r = conn.execute(
            "SELECT * FROM project_context WHERE context_key = ?", (key,)
        ).fetchone()
    finally:
        conn.close()
    return dict(r) if r else None


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


def _audit_actions_since(before: int) -> list[str]:
    from agent_mcp.core import globals as g

    return [e.get("action") for e in g.audit_log[before:]]


# --- update_project_context (single) -----------------------------------


async def test_update_single_committed_writes_row_and_db_audit(tmp_path):
    async with mcp_session(tmp_path):
        from agent_mcp.tools.project_context_tools import (
            update_project_context_tool_impl,
        )

        result = await update_project_context_tool_impl(
            {"context_key": "r4-6-key", "context_value": "v1"},
            principal=_operator_principal("op-a"),
        )

        assert isinstance(result, Ok), f"expected Ok, got {result!r}"
        row = _row("r4-6-key")
        assert row is not None
        assert row["value"] == '"v1"'
        assert row["created_by"] == "op-a"
        assert _action_rows("updated_context", "op-a") >= 1, (
            "update must write an updated_context agent_actions row"
        )


async def test_update_single_rollback_fires_zero_db_effects(
    tmp_path, monkeypatch
):
    """If the in-transaction DB-audit write raises after the upsert but
    before commit, the uow rolls back: no project_context row, no
    agent_actions row — and the tool returns Failed."""
    async with mcp_session(tmp_path):
        from agent_mcp.tools import project_context_tools as pctx_mod

        def _boom(*args, **kwargs):
            raise RuntimeError("audit sink exploded mid-transaction")

        monkeypatch.setattr(pctx_mod, "log_agent_action_to_db", _boom)

        result = await pctx_mod.update_project_context_tool_impl(
            {"context_key": "r4-6-rollback", "context_value": "v1"},
            principal=_operator_principal("op-rollback"),
        )

        assert isinstance(result, Failed), f"expected Failed, got {result!r}"
        assert _row("r4-6-rollback") is None, (
            "rolled-back update must not leave a project_context row"
        )
        assert _action_rows("updated_context", "op-rollback") == 0


# --- create_project_context ---------------------------------------------


async def test_create_committed_writes_row_and_db_audit(tmp_path):
    async with mcp_session(tmp_path):
        from agent_mcp.tools.project_context_tools import (
            create_project_context_tool_impl,
        )

        result = await create_project_context_tool_impl(
            {"context_key": "r4-6-create", "context_value": "v1"},
            principal=_operator_principal("op-b"),
        )

        assert isinstance(result, Ok), f"expected Ok, got {result!r}"
        row = _row("r4-6-create")
        assert row is not None
        assert row["created_by"] == "op-b"
        assert _action_rows("created_memory", "op-b") >= 1, (
            "create must write a created_memory agent_actions row"
        )


async def test_create_conflict_leaves_no_row_and_no_audit(tmp_path):
    """An INSERT-only conflict (key already exists) must not touch the
    audit sink — no write happened."""
    async with mcp_session(tmp_path):
        from agent_mcp.tools.project_context_tools import (
            create_project_context_tool_impl,
        )

        first = await create_project_context_tool_impl(
            {"context_key": "r4-6-dupe", "context_value": "v1"},
            principal=_operator_principal("op-c"),
        )
        assert isinstance(first, Ok)

        second = await create_project_context_tool_impl(
            {"context_key": "r4-6-dupe", "context_value": "v2"},
            principal=_operator_principal("op-c"),
        )
        assert isinstance(second, Conflict), f"expected Conflict, got {second!r}"
        row = _row("r4-6-dupe")
        assert row is not None and row["value"] == '"v1"', (
            "conflict must not overwrite the existing row"
        )
        assert _action_rows("created_memory", "op-c") == 1, (
            "the rejected duplicate create must not add a second audit row"
        )


async def test_create_rollback_fires_zero_db_effects(tmp_path, monkeypatch):
    async with mcp_session(tmp_path):
        from agent_mcp.tools import project_context_tools as pctx_mod

        def _boom(*args, **kwargs):
            raise RuntimeError("audit sink exploded mid-transaction")

        monkeypatch.setattr(pctx_mod, "log_agent_action_to_db", _boom)

        result = await pctx_mod.create_project_context_tool_impl(
            {"context_key": "r4-6-create-rollback", "context_value": "v1"},
            principal=_operator_principal("op-rollback2"),
        )

        assert isinstance(result, Failed), f"expected Failed, got {result!r}"
        assert _row("r4-6-create-rollback") is None, (
            "rolled-back create must not leave a project_context row"
        )
        assert _action_rows("created_memory", "op-rollback2") == 0


# --- delete_project_context ----------------------------------------------


async def test_delete_committed_removes_row_and_writes_db_audit(tmp_path):
    async with mcp_session(tmp_path):
        from agent_mcp.tools.project_context_tools import (
            create_project_context_tool_impl,
            delete_project_context_tool_impl,
        )

        await create_project_context_tool_impl(
            {"context_key": "r4-6-del", "context_value": "v1"},
            principal=_operator_principal("op-d"),
        )
        assert _row("r4-6-del") is not None

        result = await delete_project_context_tool_impl(
            {"context_key": "r4-6-del"},
            principal=_operator_principal("op-d"),
        )

        assert isinstance(result, Ok), f"expected Ok, got {result!r}"
        assert _row("r4-6-del") is None
        assert _action_rows("deleted_context", "op-d") >= 1, (
            "delete must write a deleted_context agent_actions row"
        )


async def test_delete_missing_key_leaves_no_audit(tmp_path):
    async with mcp_session(tmp_path):
        from agent_mcp.tools.project_context_tools import (
            delete_project_context_tool_impl,
        )

        result = await delete_project_context_tool_impl(
            {"context_key": "r4-6-does-not-exist"},
            principal=_operator_principal("op-e"),
        )
        assert isinstance(result, NotFound), f"expected NotFound, got {result!r}"
        assert _action_rows("deleted_context", "op-e") == 0


async def test_delete_rollback_fires_zero_db_effects(tmp_path, monkeypatch):
    """If the in-transaction DB-audit write raises after the DELETE but
    before commit, the uow rolls back the DELETE too — the row must
    still exist afterward."""
    async with mcp_session(tmp_path):
        from agent_mcp.tools import project_context_tools as pctx_mod

        await pctx_mod.create_project_context_tool_impl(
            {"context_key": "r4-6-del-rollback", "context_value": "v1"},
            principal=_operator_principal("op-rollback3"),
        )
        assert _row("r4-6-del-rollback") is not None

        def _boom(*args, **kwargs):
            raise RuntimeError("audit sink exploded mid-transaction")

        monkeypatch.setattr(pctx_mod, "log_agent_action_to_db", _boom)

        result = await pctx_mod.delete_project_context_tool_impl(
            {"context_key": "r4-6-del-rollback"},
            principal=_operator_principal("op-rollback3"),
        )

        assert isinstance(result, Failed), f"expected Failed, got {result!r}"
        assert _row("r4-6-del-rollback") is not None, (
            "rolled-back delete must NOT remove the row"
        )
        assert _action_rows("deleted_context", "op-rollback3") == 0


# --- bulk_update_project_context: authorization-denial atomicity ---------


async def test_bulk_auth_denial_leaves_no_audit_rows(tmp_path):
    """Extends test_project_context_ownership.py::test_12 (which pins
    "neither row mutated") with the agent_actions assertion that file
    doesn't make: an unauthorized entry anywhere in the batch must
    leave the DB-audit sink untouched too — the whole batch (including
    the authorized entries) is atomic on the AUTHORIZATION phase.
    """
    async with mcp_session(tmp_path):
        from agent_mcp.tools.project_context_tools import (
            bulk_update_project_context_tool_impl,
            update_project_context_tool_impl,
        )

        # worker-B creates 'second'; worker-A doesn't own it.
        await update_project_context_tool_impl(
            {"context_key": "r4-6-bulk-second", "context_value": "B-val"},
            principal=_worker_principal("r4-6-worker-B"),
        )

        result = await bulk_update_project_context_tool_impl(
            {
                "updates": [
                    {"context_key": "r4-6-bulk-first", "context_value": "A-val"},
                    {
                        "context_key": "r4-6-bulk-second",
                        "context_value": "A-overwrite",
                    },
                ],
            },
            principal=_worker_principal("r4-6-worker-A"),
        )

        assert isinstance(result, PermissionDenied), (
            f"expected PermissionDenied, got {result!r}"
        )
        assert _row("r4-6-bulk-first") is None, (
            "atomic reject must not insert 'r4-6-bulk-first'"
        )
        assert _action_rows("bulk_updated_context", "r4-6-worker-A") == 0, (
            "atomic reject must not write ANY bulk_updated_context audit "
            "row, including for the entry that would have been authorized"
        )


async def test_bulk_committed_writes_rows_and_db_audit_per_item(tmp_path):
    async with mcp_session(tmp_path):
        from agent_mcp.tools.project_context_tools import (
            bulk_update_project_context_tool_impl,
        )

        result = await bulk_update_project_context_tool_impl(
            {
                "updates": [
                    {"context_key": "r4-6-bulk-k1", "context_value": "v1"},
                    {"context_key": "r4-6-bulk-k2", "context_value": "v2"},
                ],
            },
            principal=_worker_principal("r4-6-worker-C"),
        )

        assert isinstance(result, Ok), f"expected Ok, got {result!r}"
        assert _row("r4-6-bulk-k1") is not None
        assert _row("r4-6-bulk-k2") is not None
        assert _action_rows("bulk_updated_context", "r4-6-worker-C") == 2
