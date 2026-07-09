"""Wave 6 PR 0 — E2E coverage of the Principal + ToolResult seam.

Pins the new dispatch contract end-to-end through:

* The MCP wire path: ``mcp_call_tool_handler → dispatch_tool_call``
  with bridge-derived Principal, ``ToolResult`` rendered back to
  ``list[TextContent]`` via :func:`render_as_text_content`.
* The REST adapter path:
  ``_dispatch_through_tool → dispatch_tool_call → ToolResult``
  match block, mapping each variant to the correct HTTP status code.
* The bridge: an unmigrated tool returning ``list[TextContent]``
  still works via the auto-wrap to ``Ok(message=...)``.

Why end-to-end here (not just a unit test of the value types): the
seam crosses three module boundaries (middleware → registry → tool
impl → response renderer). The whole point of Wave 6 is to make
"who is the caller?" flow through that seam as one typed value
instead of five ContextVar derivations; a unit test of
``Principal`` alone wouldn't catch a bug where the bridge picks
the wrong derivation order, or where the REST adapter forgets a
variant. These tests exercise the full path.

The harness already stamps the legacy ContextVars at session
setup (see ``mcp_session`` in ``tests/harness.py``); we drive
through ``dispatch_tool_call`` and ``_dispatch_through_tool``
directly to assert on the Principal-aware return shape and the
HTTP-shaped envelope respectively.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import Ok
from tests.harness import mcp_session, with_principal

pytestmark = pytest.mark.asyncio


def _insert_task(task_id: str, *, assigned_to: str | None = None) -> None:
    """Seed a task row so add_note's FK is satisfied.

    Mirrors the helper in ``tests/test_sqlalchemy_task_note.py`` —
    the side-table FK rejects orphan notes, so each test that adds
    a note has to seed the parent first. Kept inline rather than
    imported so this file is self-contained when other tests
    reference it.

    SEC Wave-B: ``add_task_note`` gates authorship on task ownership;
    a worker authoring a note passes ``assigned_to=<worker_agent_id>``.
    """
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO tasks "
            "(task_id, title, description, status, created_at, updated_at, "
            "priority, parent_task, child_tasks, depends_on_tasks, notes, "
            "created_by, assigned_to) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                "demo task",
                "",
                "pending",
                now,
                now,
                "medium",
                None,
                "[]",
                "[]",
                "[]",
                "admin",
                assigned_to,
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ── REST adapter path (operator-session) ─────────────────────────


async def test_add_task_note_via_dispatch_returns_ok_with_data(tmp_path) -> None:
    """The migrated ``add_task_note`` returns
    ``Ok(data={"note_id": ..., "task_id": ...}, message=...)`` for
    an operator-session principal. Proves the new return shape
    flows through the dispatch bridge unchanged.
    """
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        _insert_task("wave6-demo-1")

        p = Principal(
            kind="operator_session",
            user_id="alice",
            agent_id=None,
            sysadmin=False,
            project_name="demo",
            project_role="operator",
            agent_role=None,
            can_wake_loop=False,
            source_token=None,
        )
        result = await dispatch_tool_call(
            "add_task_note",
            {"task_id": "wave6-demo-1", "text": "from alice"},
            principal=p,
        )

    assert isinstance(result, Ok), f"expected Ok, got {result!r}"
    assert isinstance(result.data, dict)
    assert result.data["task_id"] == "wave6-demo-1"
    assert isinstance(result.data["note_id"], int)
    assert "added" in (result.message or "").lower()


async def test_rest_adapter_maps_ok_with_data_to_200(tmp_path) -> None:
    """``_dispatch_through_tool`` maps an ``Ok(data=..., message=...)``
    return onto a 200 JSON envelope carrying the data field. This
    is the path the dashboard would take if it had a REST route
    for add_task_note.
    """
    from agent_mcp.app._dispatch_helpers import _dispatch_through_tool

    async with mcp_session(tmp_path) as admin:  # noqa: F841 (lifespan)
        _insert_task("wave6-demo-rest-1")
        response = await _dispatch_through_tool(
            "add_task_note",
            {"task_id": "wave6-demo-rest-1", "text": "rest path"},
            bearer_token=None,
            operator_session=True,
            operator_user_id="alice",
        )

    assert response.status_code == 200, response.body
    import json as _json
    body = _json.loads(response.body)
    assert body["success"] is True
    assert body["data"]["task_id"] == "wave6-demo-rest-1"
    assert isinstance(body["data"]["note_id"], int)


async def test_rest_adapter_maps_invalid_to_400(tmp_path) -> None:
    """``Invalid(field=..., message=...)`` from a tool surfaces as
    400 with the field name in the envelope. Pinned so the
    operator can see which input was rejected without parsing a
    free-form error string.

    Uses an empty ``text`` (schema admits empty strings; the tool's
    own validation rejects them) so we exercise the tool's
    :class:`Invalid` return rather than the upstream jsonschema
    ``ToolInputValidationError`` path.
    """
    from agent_mcp.app._dispatch_helpers import _dispatch_through_tool

    async with mcp_session(tmp_path) as admin:  # noqa: F841
        _insert_task("wave6-demo-rest-invalid")
        response = await _dispatch_through_tool(
            "add_task_note",
            {"task_id": "wave6-demo-rest-invalid", "text": ""},
            bearer_token=None,
            operator_session=True,
            operator_user_id="alice",
        )

    assert response.status_code == 400, response.body
    import json as _json
    body = _json.loads(response.body)
    assert body["error"] == "invalid"
    assert body["field"] == "text"


# ── MCP wire path (agent bearer) ─────────────────────────────────


async def test_add_task_note_via_mcp_wire_renders_text_content(tmp_path) -> None:
    """An agent-bearer call through the registered MCP handler
    receives a ``[TextContent(text="Note ... added to task ...")]``
    response. Proves the renderer at
    :func:`agent_mcp.core.tool_result.render_as_text_content`
    correctly converts ``Ok(message=...)`` back to the legacy MCP
    wire shape so MCP clients see no behavioural change.
    """
    async with mcp_session(tmp_path) as admin:
        _insert_task("wave6-demo-mcp-1")
        result = await admin.assert_tool_succeeds(
            "add_task_note",
            {"task_id": "wave6-demo-mcp-1", "text": "via mcp wire"},
        )
        text = result[0].text
        assert "added" in text.lower()
        assert "wave6-demo-mcp-1" in text


async def test_add_task_note_worker_bearer_admits_as_agent(tmp_path) -> None:
    """Workers are agent_bearer principals; the migrated tool's
    policy admits any agent_bearer (or operator). Pins that the
    bridge derives the worker's Principal correctly from
    ``request_auth_token``.
    """
    async with mcp_session(tmp_path) as admin:
        _insert_task("wave6-demo-worker-1", assigned_to="alice")
        alice = await admin.create_worker("alice")
        result = await alice.assert_tool_succeeds(
            "add_task_note",
            {"task_id": "wave6-demo-worker-1", "text": "from worker"},
        )
        text = result[0].text
        assert "added" in text.lower()

        from agent_mcp.db.actions import task_notes_db
        notes = task_notes_db.list_notes_for_task("wave6-demo-worker-1")
        # The author is "alice" because the bridge derives an
        # agent_bearer Principal from the worker's bearer token.
        assert len(notes) == 1
        assert notes[0]["author"] == "alice"


# ── Bridge: unmigrated tool still works ──────────────────────────


async def test_migrated_edit_task_note_returns_ok_through_renderer(tmp_path) -> None:
    """The migrated ``edit_task_note`` returns :class:`Ok`; the
    renderer at :func:`render_as_text_content` converts the
    ``Ok(message=...)`` back into the legacy ``list[TextContent]``
    shape MCP clients consume. Wave 6 PR 6: the legacy
    ``list[TextContent]`` bridge is gone — every tool returns a
    typed :data:`ToolResult` and the renderer is the sole conversion
    surface for the MCP wire.

    Drives through the MCP-wire path (admin.call) so the bearer
    flows through the existing Q6e fallback into the tool's
    ``arguments["token"]``; this is the same path real MCP clients
    take.
    """
    from agent_mcp.db.actions import task_notes_db

    async with mcp_session(tmp_path) as admin:
        _insert_task("wave6-demo-bridge-1")
        # Seed a note.
        seed = await admin.assert_tool_succeeds(
            "add_task_note",
            {"task_id": "wave6-demo-bridge-1", "text": "v1"},
        )
        assert "added" in seed[0].text.lower()
        notes = task_notes_db.list_notes_for_task("wave6-demo-bridge-1")
        note_id = notes[0]["note_id"]

        result = await admin.assert_tool_succeeds(
            "edit_task_note",
            {"note_id": note_id, "text": "v2"},
        )

        assert "updated" in result[0].text.lower(), result[0].text
        # Re-fetch (still inside mcp_session) to confirm the tool
        # actually performed its update.
        updated = task_notes_db.get_note(note_id)
        assert updated is not None and updated["text"] == "v2"


# ── with_principal harness helper ────────────────────────────────


async def test_with_principal_helper_stamps_request_principal(tmp_path) -> None:
    """The ``with_principal()`` helper stamps the
    :data:`request_principal` ContextVar so any in-process surface
    that reads it (e.g. the MCP-wire handler) sees the same identity.

    Wave 6 PR 6: ``dispatch_tool_call`` requires an explicit
    ``principal=`` kwarg; the helper now stamps
    :data:`request_principal` for surfaces that derive identity from
    the request context, but the dispatcher itself never reads from
    a ContextVar fallback.
    """
    from agent_mcp.tools.registry import dispatch_tool_call, request_principal

    from agent_mcp.db.actions import task_notes_db

    async with mcp_session(tmp_path):
        _insert_task("wave6-demo-helper-1")

        p = Principal(
            kind="operator_session",
            user_id="bob",
            agent_id=None,
            sysadmin=False,
            project_name="demo",
            project_role="operator",
            agent_role=None,
            can_wake_loop=False,
            source_token=None,
        )
        with with_principal(p):
            # The helper stamps request_principal — surfaces that
            # consult it (the MCP handler) see ``bob`` as the caller.
            assert request_principal.get() is p
            # The dispatcher itself requires explicit principal — no
            # ContextVar bridge in PR 6 — so we pass it through.
            result = await dispatch_tool_call(
                "add_task_note",
                {"task_id": "wave6-demo-helper-1", "text": "from bob"},
                principal=p,
            )

        assert isinstance(result, Ok)
        assert result.data["task_id"] == "wave6-demo-helper-1"

        notes = task_notes_db.list_notes_for_task("wave6-demo-helper-1")
        # Author resolved from principal.user_id via the migrated tool.
        assert notes[0]["author"] == "bob"
