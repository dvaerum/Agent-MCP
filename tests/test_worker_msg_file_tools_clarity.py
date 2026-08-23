"""Worker-facing message clarity for the file tools.

Pins the *wording* of the messages a worker agent sees from
``agent_mcp/tools/file_management_tools.py`` and
``agent_mcp/tools/file_metadata_tools.py`` so a future refactor
can't silently regress them back to the terse / leaky variants:

1. ``update_file_status`` claim conflict — mirrors
   ``check_file_status`` (holder id + timestamp, already public via
   that tool) and states the advisory-lock / no-auto-expiry
   semantic plus how to make progress.
2. ``update_file_metadata`` operator-only denial — tells a worker
   *why* it's denied and what to do (ask an operator; you can still
   read).
3. ``view_file_metadata`` no-row — a benign ``Ok`` with an empty
   payload ("nothing recorded yet"), not a ``NotFound`` that reads
   as broken.
4. INFO-LEAK: the DB / unexpected error arms must NOT interpolate
   the raw exception (or SQL) into the worker-facing text — that
   detail belongs only in the server log.
"""

from __future__ import annotations

import sqlite3

import pytest

from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import Conflict, Failed, Ok
from agent_mcp.tools.registry import dispatch_tool_call
from tests.harness import dispatch_expecting_denial, make_principal, mcp_session

pytestmark = pytest.mark.asyncio


# ── Principal builders ───────────────────────────────────────────


def _worker(agent_id: str, *, bearer: str) -> Principal:
    """agent_bearer with the worker bundle (carries ``files.use``)."""
    return make_principal(
        kind="agent_bearer",
        user_id=None,
        agent_id=agent_id,
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role="worker",  # type: ignore[arg-type]
        can_wake_loop=False,
        source_token=bearer,
    )


def _operator(user_id: str = "op") -> Principal:
    """operator_session carrying ``system.config.write`` (sysadmin)."""
    return make_principal(
        kind="operator_session",
        user_id=user_id,
        agent_id=None,
        sysadmin=True,
        project_name="demo",
        project_role="operator",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


# ── #1: update_file_status claim conflict wording ────────────────


async def test_update_file_status_conflict_message_is_advisory_and_actionable(
    tmp_path,
) -> None:
    """A second agent claiming a held file gets a Conflict whose reason
    names the holder + timestamp + status and explains the advisory
    lock has no auto-expiry, plus how to proceed."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")
        filepath = "/tmp/worker-msg-conflict.txt"

        await dispatch_tool_call(
            "update_file_status",
            {"filepath": filepath, "status": "editing"},
            principal=_worker("alice", bearer=alice.token),
        )
        result = await dispatch_tool_call(
            "update_file_status",
            {"filepath": filepath, "status": "editing"},
            principal=_worker("bob", bearer=bob.token),
        )

        assert isinstance(result, Conflict), f"expected Conflict, got {result!r}"
        reason = result.reason
        # Holder disclosed (already public via check_file_status).
        assert "alice" in reason
        assert "already claimed by agent 'alice'" in reason
        # Advisory-lock / no-auto-expiry semantic spelled out.
        assert "advisory lock with no auto-expiry" in reason
        assert "frees only when 'alice' releases it" in reason
        # Timestamp + status disclosed.
        assert "since " in reason
        assert "status: editing" in reason
        # Actionable guidance.
        assert "check_file_status" in reason
        assert "work on a different file" in reason
        # Old terse phrasing gone.
        assert "already being used" not in reason
        assert "Cannot" not in reason


# ── #2: update_file_metadata operator-only denial wording ────────


async def test_update_file_metadata_worker_denial_explains_and_guides(
    tmp_path,
) -> None:
    """A worker denied from updating metadata gets a message that says
    it's operator-only, to ask an operator, and that read still works."""
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("alice")
        # Phase 2 (Finding A): the gate is now
        # ``@requires_capability("system.config.write", reason=...)`` on
        # the impl, so the denial raises ``AuthRejected``. The wording
        # below is carried verbatim on the decorator's ``reason=`` — the
        # whole point of that kwarg is that moving a single-capability
        # gate out of the body must not cost the worker its guidance.
        reason = await dispatch_expecting_denial(
            "update_file_metadata",
            {"filepath": "/tmp/x.txt", "metadata": {"k": "v"}},
            principal=_worker("alice", bearer=worker.token),
        )
        assert "operator-only action" in reason
        assert "a worker agent cannot record file metadata" in reason
        assert "Ask a project operator to set it" in reason
        assert "view_file_metadata" in reason


# ── #3: view_file_metadata no-row is benign Ok, not NotFound ─────


async def test_view_file_metadata_no_row_is_benign_ok(tmp_path) -> None:
    """No recorded metadata surfaces as Ok with an empty payload and a
    "nothing recorded yet" message — never NotFound."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        result = await dispatch_tool_call(
            "view_file_metadata",
            {"filepath": "/tmp/worker-msg-never-recorded.txt"},
            principal=_worker("alice", bearer=alice.token),
        )

        assert isinstance(result, Ok), f"expected Ok, got {result!r}"
        assert result.data["metadata"] is None
        msg = result.message or ""
        assert "No metadata has been recorded" in msg
        assert "optional and operator-managed" in msg
        assert "an empty result here is normal" in msg


# ── #4: INFO-LEAK — raw exception / SQL never in caller text ─────


_SECRET_SQL = "no such column: agents.secret_token_hash"


def _raising_conn():
    """A fake DB connection whose cursor.execute raises a sqlite3 error
    carrying a would-be-leaked column name."""

    class _Cursor:
        def execute(self, *_a, **_k):
            raise sqlite3.OperationalError(_SECRET_SQL)

    class _Conn:
        def cursor(self):
            return _Cursor()

        def close(self):
            pass

    return _Conn()


def _raising_unit_of_work():
    """A unit_of_work replacement that raises a sqlite3 error on enter,
    carrying a would-be-leaked column name."""

    def _factory():
        raise sqlite3.OperationalError(_SECRET_SQL)

    return _factory


async def test_view_file_metadata_db_error_does_not_leak_exception(
    tmp_path, monkeypatch
) -> None:
    """A DB error while viewing metadata yields a generic Failed whose
    caller-facing message contains neither the raw exception nor the
    SQL column name."""
    import agent_mcp.tools.file_metadata_tools as mod

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        monkeypatch.setattr(mod, "get_db_connection", _raising_conn)

        result = await dispatch_tool_call(
            "view_file_metadata",
            {"filepath": "/tmp/worker-msg-dberr.txt"},
            principal=_worker("alice", bearer=alice.token),
        )

        assert isinstance(result, Failed), f"expected Failed, got {result!r}"
        msg = result.message or ""
        assert _SECRET_SQL not in msg
        assert "no such column" not in msg
        assert "secret_token_hash" not in msg
        assert "sqlite" not in msg.lower()
        assert "A database error occurred; it has been logged." in msg


async def test_update_file_metadata_db_error_does_not_leak_exception(
    tmp_path, monkeypatch
) -> None:
    """A DB error while updating metadata yields a generic Failed whose
    caller-facing message contains neither the raw exception nor the
    SQL column name."""
    import agent_mcp.tools.file_metadata_tools as mod

    async with mcp_session(tmp_path):
        monkeypatch.setattr(mod, "unit_of_work", _raising_unit_of_work())

        result = await dispatch_tool_call(
            "update_file_metadata",
            {"filepath": "/tmp/worker-msg-dberr.txt", "metadata": {"k": "v"}},
            principal=_operator("op-claire"),
        )

        assert isinstance(result, Failed), f"expected Failed, got {result!r}"
        msg = result.message or ""
        assert _SECRET_SQL not in msg
        assert "no such column" not in msg
        assert "secret_token_hash" not in msg
        assert "sqlite" not in msg.lower()
        assert "A database error occurred; it has been logged." in msg
