"""Wave 6 PR 1 — E2E coverage of the small-tool family migration.

Pins the Principal + ToolResult contract end-to-end for each
file that was migrated in Wave 6 PR 1:

* ``tools/task_notes_tools.py`` — ``edit_task_note``,
  ``delete_task_note`` (``add_task_note`` was the PR 0 demo)
* ``tools/rag_tools.py`` — ``ask_project_rag``
* ``tools/file_management_tools.py`` — ``check_file_status``,
  ``update_file_status``
* ``tools/file_metadata_tools.py`` — ``view_file_metadata``,
  ``update_file_metadata``

Each test drives a real :class:`Principal` (operator or
agent_bearer) through ``dispatch_tool_call`` to assert the
typed-return contract. The pre-existing wire-level coverage
(``test_phase2_wave3_permission_matrix.py``,
``test_sqlalchemy_task_note.py``, ``test_wave3_admin_token_removal.py``)
still exercises the MCP-handler path through
``WorkerSession.call`` — the tests in this file pin the new
:data:`ToolResult` shape *directly* so a future PR that
re-orders the dispatch bridge can't silently regress the typed
contract while keeping the rendered text identical.

Per the PR 1 brief: one happy-path test per migrated tool plus
enough policy tests to demonstrate the auth surface; the
existing test suites cover the deeper edge cases.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import pytest

from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import (
    Invalid,
    NotFound,
    Ok,
    PermissionDenied,
)
from tests.harness import dispatch_expecting_denial, make_principal, mcp_session

pytestmark = pytest.mark.asyncio


# ── Helpers ──────────────────────────────────────────────────────


def _operator_principal(user_id: str = "alice") -> Principal:
    """Build an operator_session Principal for test calls.

    Mirrors what the REST middleware would synthesise from a
    cookie-authenticated dashboard request post-Wave-6. Carries
    enough identity that audit log lines and per-tool policy
    decisions resolve correctly without needing the harness's
    legacy ContextVar stamps.
    """
    return make_principal(
        kind="operator_session",
        user_id=user_id,
        agent_id=None,
        sysadmin=False,
        project_name="demo",
        project_role="operator",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


def _agent_principal(
    agent_id: str,
    *,
    bearer: str,
    role: str | None = None,
) -> Principal:
    """Build an agent_bearer Principal.

    Matches what the MCP middleware would synthesise from an
    ``Authorization: Bearer <token>`` header. ``role`` mirrors the
    ``agents.agent_role`` column; pass ``"manager"`` for
    supervision-tier tests.
    """
    return make_principal(
        kind="agent_bearer",
        user_id=None,
        agent_id=agent_id,
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role=role,  # type: ignore[arg-type]
        can_wake_loop=False,
        source_token=bearer,
    )


def _insert_task(task_id: str) -> None:
    """Seed a minimal task row so notes can FK-attach."""
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO tasks "
            "(task_id, title, description, status, created_at, "
            "updated_at, priority, parent_task, child_tasks, "
            "depends_on_tasks, notes, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id, "demo", "", "pending", now, now, "medium",
                None, "[]", "[]", "[]", "admin",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _set_file_metadata(filepath: str, metadata: dict[str, Any]) -> None:
    """Insert a file_metadata row for view tests."""
    import json

    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO file_metadata "
            "(filepath, metadata, last_updated, updated_by) "
            "VALUES (?, ?, ?, ?)",
            (filepath, json.dumps(metadata), now, "seed"),
        )
        conn.commit()
    finally:
        conn.close()


# ── task_notes_tools.py: edit + delete ───────────────────────────


async def test_edit_task_note_returns_ok_for_author(tmp_path) -> None:
    """A worker editing their own note returns ``Ok(data={"note_id": N})``."""
    from agent_mcp.db.actions import task_notes_db
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path) as admin:
        _insert_task("pr1-task-edit-ok")
        alice = await admin.create_worker("alice")
        note_id = task_notes_db.add_note(
            "pr1-task-edit-ok", "alice", "v1",
        )

        p = _agent_principal("alice", bearer=alice.token)
        result = await dispatch_tool_call(
            "edit_task_note",
            {"note_id": note_id, "text": "v2"},
            principal=p,
        )

        assert isinstance(result, Ok), f"expected Ok, got {result!r}"
        assert result.data == {"note_id": note_id}
        assert task_notes_db.get_note(note_id)["text"] == "v2"


async def test_edit_task_note_permission_denied_for_non_author(
    tmp_path,
) -> None:
    """A worker editing someone else's note is rejected.

    PF-1 (round 4): the rejection is a typed :class:`NotFound`
    indistinguishable from a nonexistent note — closing the
    note-existence oracle — and must never leak the authoring agent's
    id (the DB layer's ``"owned by 'alice'"`` string). (Previously this
    surfaced as PermissionDenied naming the author.)"""
    from agent_mcp.db.actions import task_notes_db
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path) as admin:
        _insert_task("pr1-task-edit-denied")
        # Seed Alice as the note's author via direct DB write — we
        # don't need her bearer for this test, only Bob's.
        await admin.create_worker("alice")
        bob = await admin.create_worker("bob")
        note_id = task_notes_db.add_note(
            "pr1-task-edit-denied", "alice", "v1",
        )

        # Bob (worker, non-author) attempts to edit Alice's note.
        p = _agent_principal("bob", bearer=bob.token)
        result = await dispatch_tool_call(
            "edit_task_note",
            {"note_id": note_id, "text": "hijack"},
            principal=p,
        )

        assert isinstance(result, NotFound), (
            f"expected NotFound, got {result!r}"
        )
        assert "alice" not in repr(result), (
            f"author id must not leak; got {result!r}"
        )
        # Note unchanged.
        assert task_notes_db.get_note(note_id)["text"] == "v1"


async def test_edit_task_note_not_found(tmp_path) -> None:
    """Editing a non-existent note returns :class:`NotFound`.

    Pins the typed-variant classification on the DB-error path so a
    REST consumer would see 404, not 500."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        p = _agent_principal("alice", bearer=alice.token)
        result = await dispatch_tool_call(
            "edit_task_note",
            {"note_id": 999999, "text": "ghost"},
            principal=p,
        )

        assert isinstance(result, NotFound), (
            f"expected NotFound, got {result!r}"
        )
        assert result.resource == "task note"
        assert result.identifier == "999999"


async def test_edit_task_note_invalid_input(tmp_path) -> None:
    """Missing required ``text`` field returns :class:`Invalid`."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        p = _agent_principal("alice", bearer=alice.token)
        result = await dispatch_tool_call(
            "edit_task_note",
            {"note_id": 1, "text": ""},
            principal=p,
        )

        assert isinstance(result, Invalid), (
            f"expected Invalid, got {result!r}"
        )
        assert result.field == "text"


async def test_delete_task_note_operator_can_delete_worker_note(
    tmp_path,
) -> None:
    """An operator-session principal (manager-tier) can delete a
    worker-authored note via the same is_admin=True path that
    manager-role agents use."""
    from agent_mcp.db.actions import task_notes_db
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path) as admin:
        _insert_task("pr1-task-delete-op")
        # Seed Alice's row so the agents-table FK is satisfied; the
        # operator does the deletion, so we don't need her bearer.
        await admin.create_worker("alice")
        note_id = task_notes_db.add_note(
            "pr1-task-delete-op", "alice", "soon to die",
        )

        # Operator session — dashboard moderation path.
        p = _operator_principal("op-bob")
        result = await dispatch_tool_call(
            "delete_task_note",
            {"note_id": note_id},
            principal=p,
        )

        assert isinstance(result, Ok), f"expected Ok, got {result!r}"
        assert result.data == {"note_id": note_id}
        assert task_notes_db.get_note(note_id) is None


# ── rag_tools.py: ask_project_rag ────────────────────────────────


async def test_ask_project_rag_ok_for_agent(tmp_path) -> None:
    """``ask_project_rag`` admits an agent_bearer principal and
    returns ``Ok(data={"answer": <text>})``.

    The mock-ollama transport in the harness returns deterministic
    zero-vector embeddings; ``query_rag_system`` falls back to a
    no-result prose answer, which the tool wraps as Ok. We don't
    assert on the prose text — just the variant + shape.
    """
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        # SEC Wave-B: ask_project_rag now requires the ``rag.query``
        # capability, so the principal must carry the worker role (the
        # bundle that grants it) — a role=None bearer is denied.
        p = _agent_principal("alice", bearer=alice.token, role="worker")
        result = await dispatch_tool_call(
            "ask_project_rag",
            {"query": "what does the project do?"},
            principal=p,
        )

        assert isinstance(result, Ok), f"expected Ok, got {result!r}"
        assert "answer" in (result.data or {})
        assert isinstance(result.data["answer"], str)


async def test_ask_project_rag_rejects_operator(tmp_path) -> None:
    """``ask_project_rag`` is agent-only by design — operator
    sessions get :class:`PermissionDenied`.

    Preserves the pre-Wave-6 semantic (the old ``@requires("any")``
    decorator required a resolvable agent token). A later UX-driven
    PR can widen to admit operators; PR 1's job is signature
    migration, not behaviour change.
    """
    async with mcp_session(tmp_path):
        await dispatch_expecting_denial(
            "ask_project_rag",
            {"query": "hello"},
            principal=_operator_principal(),
        )


async def test_ask_project_rag_invalid_query(tmp_path) -> None:
    """Empty query returns :class:`Invalid` with field="query"."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        # SEC Wave-B: needs ``rag.query`` (worker bundle) to reach the
        # query-validation branch; a role=None bearer 403s first.
        p = _agent_principal("alice", bearer=alice.token, role="worker")
        result = await dispatch_tool_call(
            "ask_project_rag",
            {"query": ""},
            principal=p,
        )

        assert isinstance(result, Invalid), (
            f"expected Invalid, got {result!r}"
        )
        assert result.field == "query"


# ── file_management_tools.py: check + update ─────────────────────


async def test_check_file_status_returns_not_in_use(tmp_path) -> None:
    """A path nobody has claimed surfaces as Ok(in_use=False)."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        # SEC round-2: check_file_status now requires the ``files.use``
        # capability, so the principal must carry the worker role so it
        # has files.use — a role=None bearer is denied.
        p = _agent_principal("alice", bearer=alice.token, role="worker")
        result = await dispatch_tool_call(
            "check_file_status",
            {"filepath": "/tmp/never-claimed-by-anyone.txt"},
            principal=p,
        )

        assert isinstance(result, Ok), f"expected Ok, got {result!r}"
        assert result.data["in_use"] is False
        assert result.data["filepath"].endswith("/never-claimed-by-anyone.txt")


async def test_update_file_status_claim_then_release(tmp_path) -> None:
    """Round-trip: claim a file, observe it via check, release it."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        # SEC round-2: update_file_status now requires the ``files.use``
        # capability, so the principal must carry the worker role so it
        # has files.use — a role=None bearer is denied.
        p = _agent_principal("alice", bearer=alice.token, role="worker")
        filepath = "/tmp/wave6-pr1-file.txt"

        # Claim for editing.
        claim = await dispatch_tool_call(
            "update_file_status",
            {"filepath": filepath, "status": "editing"},
            principal=p,
        )
        assert isinstance(claim, Ok), claim
        assert claim.data["status"] == "editing"
        assert claim.data["agent_id"] == "alice"

        # Observe via check.
        observe = await dispatch_tool_call(
            "check_file_status",
            {"filepath": filepath},
            principal=p,
        )
        assert isinstance(observe, Ok), observe
        assert observe.data["in_use"] is True
        assert observe.data["agent_id"] == "alice"
        assert observe.data["status"] == "editing"

        # Release.
        release = await dispatch_tool_call(
            "update_file_status",
            {"filepath": filepath, "status": "released"},
            principal=p,
        )
        assert isinstance(release, Ok), release
        assert release.data["status"] == "released"


async def test_update_file_status_conflict_when_claimed_by_other(
    tmp_path,
) -> None:
    """Claiming a file held by a different agent returns
    :class:`Conflict` (state invariant, REST → 409). Distinct from
    PermissionDenied — the principal is valid; the file map is the
    blocker."""
    from agent_mcp.core.tool_result import Conflict
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")
        filepath = "/tmp/wave6-pr1-conflict.txt"

        # SEC round-2: update_file_status now requires the ``files.use``
        # capability, so both principals must carry the worker role so
        # they have files.use — a role=None bearer is denied.
        # Alice claims.
        p_alice = _agent_principal("alice", bearer=alice.token, role="worker")
        await dispatch_tool_call(
            "update_file_status",
            {"filepath": filepath, "status": "editing"},
            principal=p_alice,
        )

        # Bob attempts to claim same file.
        p_bob = _agent_principal("bob", bearer=bob.token, role="worker")
        result = await dispatch_tool_call(
            "update_file_status",
            {"filepath": filepath, "status": "editing"},
            principal=p_bob,
        )

        assert isinstance(result, Conflict), (
            f"expected Conflict, got {result!r}"
        )
        assert "alice" in result.reason


async def test_check_file_status_rejects_operator(tmp_path) -> None:
    """File-claim verbs are agent-keyed; operator session is
    rejected (pre-Wave-6 semantic preserved)."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        p = _operator_principal()
        result = await dispatch_tool_call(
            "check_file_status",
            {"filepath": "/tmp/anything.txt"},
            principal=p,
        )

        assert isinstance(result, PermissionDenied), (
            f"expected PermissionDenied, got {result!r}"
        )


# ── file_metadata_tools.py: view + update ────────────────────────


async def test_view_file_metadata_no_row_returns_ok_benign(tmp_path) -> None:
    """A filepath with no recorded metadata surfaces as a benign
    :class:`Ok` with an empty payload — not :class:`NotFound`.

    File metadata is optional and operator-managed, so "nothing
    recorded yet" is a normal state a worker should read as benign,
    not a broken/404 lookup. See ``test_worker_msg_file_tools_clarity``
    for the message-wording assertions."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        # SEC round-2: view_file_metadata now requires the ``files.use``
        # capability, so the principal must carry the worker role so it
        # has files.use — a role=None bearer is denied.
        p = _agent_principal("alice", bearer=alice.token, role="worker")
        result = await dispatch_tool_call(
            "view_file_metadata",
            {"filepath": "/tmp/never-recorded.txt"},
            principal=p,
        )

        assert isinstance(result, Ok), f"expected Ok, got {result!r}"
        assert result.data["metadata"] is None
        assert result.data["filepath"].endswith("/never-recorded.txt")


async def test_view_file_metadata_ok_returns_parsed_data(tmp_path) -> None:
    """Seed a file_metadata row, then view returns Ok(data=...)
    with the metadata parsed back to a dict."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        filepath = "/tmp/wave6-pr1-seeded.txt"
        _set_file_metadata(filepath, {"purpose": "demo", "owner": "alice"})

        # SEC round-2: view_file_metadata now requires the ``files.use``
        # capability, so the principal must carry the worker role so it
        # has files.use — a role=None bearer is denied.
        p = _agent_principal("alice", bearer=alice.token, role="worker")
        result = await dispatch_tool_call(
            "view_file_metadata",
            {"filepath": filepath},
            principal=p,
        )

        assert isinstance(result, Ok), f"expected Ok, got {result!r}"
        assert result.data["metadata"] == {
            "purpose": "demo", "owner": "alice",
        }
        assert result.data["filepath"] == filepath


async def test_update_file_metadata_requires_operator(tmp_path) -> None:
    """Worker agents are rejected; only operator-tier passes."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("alice")
        p_worker = _agent_principal("alice", bearer=worker.token)
        result = await dispatch_tool_call(
            "update_file_metadata",
            {
                "filepath": "/tmp/x.txt",
                "metadata": {"k": "v"},
            },
            principal=p_worker,
        )
        assert isinstance(result, PermissionDenied), (
            f"expected PermissionDenied for worker, got {result!r}"
        )


async def test_update_file_metadata_operator_writes_row(tmp_path) -> None:
    """Happy path: operator writes a metadata row; the row reads
    back with the operator's user_id as updated_by."""
    from agent_mcp.db.connection import get_db_connection
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        p = _operator_principal("op-claire")
        filepath = "/tmp/wave6-pr1-update-meta.txt"
        result = await dispatch_tool_call(
            "update_file_metadata",
            {
                "filepath": filepath,
                "metadata": {"purpose": "verify write"},
            },
            principal=p,
        )

        assert isinstance(result, Ok), f"expected Ok, got {result!r}"
        assert result.data["updated_by"] == "op-claire"

        # Authoritative read-back.
        conn = get_db_connection()
        try:
            row = conn.execute(
                "SELECT updated_by, metadata FROM file_metadata WHERE filepath = ?",
                (result.data["filepath"],),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["updated_by"] == "op-claire"
        # JSON round-trip survived.
        import json as _json
        assert _json.loads(row["metadata"]) == {"purpose": "verify write"}


async def test_update_file_metadata_invalid_non_serializable(tmp_path) -> None:
    """Metadata that the schema admits as an object but contains a
    non-JSON-serialisable value is rejected with :class:`Invalid`.

    ``set`` is a dict but ``json.dumps`` rejects it — exercises the
    impl's own ``Invalid`` branch (the jsonschema gate upstream
    only checks type, not serialisability).
    """
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        p = _operator_principal()
        # A dict containing a set — schema admits (object); json.dumps
        # rejects (set is not JSON-serialisable).
        result = await dispatch_tool_call(
            "update_file_metadata",
            {
                "filepath": "/tmp/x.txt",
                "metadata": {"tags": {"unhashable", "set"}},
            },
            principal=p,
        )

        assert isinstance(result, Invalid), (
            f"expected Invalid, got {result!r}"
        )
        assert result.field == "metadata"


# ── Cross-cutting: principal=None rejects every migrated tool ────


@pytest.mark.parametrize(
    "tool_name,arguments",
    [
        ("edit_task_note", {"note_id": 1, "text": "x"}),
        ("delete_task_note", {"note_id": 1}),
        ("ask_project_rag", {"query": "hi"}),
        ("check_file_status", {"filepath": "/tmp/x"}),
        ("update_file_status", {"filepath": "/tmp/x", "status": "editing"}),
        ("view_file_metadata", {"filepath": "/tmp/x"}),
        (
            "update_file_metadata",
            {"filepath": "/tmp/x", "metadata": {}},
        ),
    ],
)
async def test_migrated_tools_reject_anonymous_principal(
    tmp_path,
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    """Every migrated tool rejects an anonymous caller.

    Wave 6 PR 6: the legacy ContextVar bridge is gone. The dispatcher
    falls back to synthesizing an ``agent_bearer`` Principal from
    ``arguments["token"]`` when no explicit Principal is supplied; we
    omit the token here so the fallback also returns None and the
    tool's policy gate is what we observe.
    Phase 2 (Finding A): these gates moved from an in-body
    ``return PermissionDenied`` to a registration-time ``@requires_*``
    decorator, which ``dispatch_tool_call`` evaluates as a raised
    ``AuthRejected`` before schema validation. ``dispatch_expecting_denial``
    accepts either carrier — what is pinned is that an anonymous caller
    is DENIED (never reaches the tool body), which is the security
    property. Without it ``ask_project_rag``'s query_rag_system on the
    no-OpenAI mock would return error prose inside an ``Ok``, masking a
    missing auth check.
    """
    async with mcp_session(tmp_path):
        await dispatch_expecting_denial(tool_name, arguments)


