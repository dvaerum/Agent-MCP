"""Round-9 security: type-confusion 400-not-500 on direct-SQL REST handlers.

The dashboard REST handlers ``create_task`` (POST /api/tasks),
``create_memory`` (POST /api/memories), ``edit_agent``
(POST /api/agents/<id>/edit) and ``update_task_details``
(POST /api/update-task-dashboard) write SQLite / repo rows directly,
bypassing the schema-validating MCP tool dispatch. A structured JSON
type (dict / list) supplied in a string-typed field used to reach a
SQL bind and surface as an uncaught 500 — or, worse, be stored as bad
data behind a misleading ``200 {"success": true}`` (capabilities into
the task-claim authz cache; a dict field into ``update-task-dashboard``
that silently no-ops).

These tests pin the fix: every user-supplied field that flows to a DB
bind / repo write / cache is type-guarded up front, returning a clean
400 (never a 500, never a silent success) while valid input keeps
working.

RED against origin/main (500s / silent-200s); GREEN after the guards.
"""

from __future__ import annotations

import json as _json

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


# Structured JSON values that must never reach a SQL bind untyped.
_BAD_VALUES = ({"nested": 1}, ["a", "list"])


def _row(table: str, where_sql: str, params: tuple) -> dict | None:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM {table} WHERE {where_sql}", params)
        r = cursor.fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


# ==================== create_task (POST /api/tasks) ====================


@pytest.mark.parametrize("field", ["parent_task", "priority", "assigned_to",
                                    "description"])
@pytest.mark.parametrize("bad", _BAD_VALUES)
async def test_create_task_rejects_structured_field(tmp_path, field, bad) -> None:
    async with mcp_session(tmp_path) as admin:
        body = {
            "token": admin.admin_token,
            "task_title": "type-confusion probe",
        }
        # description supplies its own key name in the request body.
        request_key = "task_description" if field == "description" else field
        body[request_key] = bad
        r = admin.client.post("/api/tasks", json=body)
        assert r.status_code == 400, (
            f"{field}={bad!r} should be 400 not {r.status_code}: {r.text}"
        )


async def test_create_task_rejects_structured_required_capabilities(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/api/tasks",
            json={
                "token": admin.admin_token,
                "task_title": "caps probe",
                "required_capabilities": {"code_edit": True},
            },
        )
        assert r.status_code == 400, r.text


async def test_create_task_description_dict_not_stored(tmp_path) -> None:
    """A dict description must be rejected, never persisted into the column."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/api/tasks",
            json={
                "token": admin.admin_token,
                "task_title": "desc probe",
                "task_description": {"evil": "dict"},
            },
        )
        assert r.status_code == 400, r.text
        # No task should have been created with that dict description.
        listing = admin.client.get("/api/tasks").json()
        assert "evil" not in _json.dumps(listing)


async def test_create_task_valid_still_works(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = admin.client.post(
            "/api/tasks",
            json={
                "token": admin.admin_token,
                "task_title": "valid task",
                "task_description": "a description",
                "priority": "high",
                "assigned_to": "alice",
                "required_capabilities": ["code_edit", "file_read"],
            },
        )
        assert r.status_code == 200, r.text
        assert r.json().get("success") is True


# ================== create_memory (POST /api/memories) =================


@pytest.mark.parametrize("bad", _BAD_VALUES)
async def test_create_memory_rejects_structured_context_key(tmp_path, bad) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/api/memories",
            json={
                "token": admin.admin_token,
                "context_key": bad,
                "context_value": {"ok": 1},
            },
        )
        assert r.status_code == 400, (
            f"context_key={bad!r} should be 400 not {r.status_code}: {r.text}"
        )


@pytest.mark.parametrize("bad", _BAD_VALUES)
async def test_create_memory_rejects_structured_description(tmp_path, bad) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/api/memories",
            json={
                "token": admin.admin_token,
                "context_key": "mem.desc.probe",
                "context_value": {"ok": 1},
                "description": bad,
            },
        )
        assert r.status_code == 400, r.text


async def test_create_memory_valid_still_works(tmp_path) -> None:
    """context_value is arbitrary JSON (dict/list are legitimate here)."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/api/memories",
            json={
                "token": admin.admin_token,
                "context_key": "mem.valid",
                "context_value": {"structured": ["value", "is", "fine"]},
                "description": "a string description",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json().get("success") is True


# ============= edit_agent (POST /api/agents/<id>/edit) ================


@pytest.mark.parametrize("field", ["color", "working_directory"])
@pytest.mark.parametrize("bad", _BAD_VALUES)
async def test_edit_agent_rejects_structured_string_field(tmp_path, field, bad) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = admin.client.post(
            "/api/agents/alice/edit",
            json={"token": admin.admin_token, field: bad},
        )
        assert r.status_code == 400, (
            f"{field}={bad!r} should be 400 not {r.status_code}: {r.text}"
        )


async def test_edit_agent_rejects_dict_capabilities(tmp_path) -> None:
    """capabilities feeds the task-claim authz cache; a dict must not land
    there behind a misleading 200."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = admin.client.post(
            "/api/agents/alice/edit",
            json={"token": admin.admin_token, "capabilities": {"admin": True}},
        )
        assert r.status_code == 400, r.text
        # The cache / row must NOT have absorbed the dict's keys as caps.
        row = _row("agents", "agent_id = ?", ("alice",))
        assert row is not None
        assert _json.loads(row["capabilities"] or "[]") != ["admin"]


async def test_edit_agent_rejects_capabilities_with_nonstr_element(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = admin.client.post(
            "/api/agents/alice/edit",
            json={
                "token": admin.admin_token,
                "capabilities": ["ok", {"nested": 1}],
            },
        )
        assert r.status_code == 400, r.text


async def test_edit_agent_capabilities_list_of_str_still_works(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = admin.client.post(
            "/api/agents/alice/edit",
            json={
                "token": admin.admin_token,
                "capabilities": ["code_edit", "file_read"],
            },
        )
        assert r.status_code == 200, r.text
        row = _row("agents", "agent_id = ?", ("alice",))
        assert _json.loads(row["capabilities"]) == ["code_edit", "file_read"]


async def test_edit_agent_valid_color_still_works(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = admin.client.post(
            "/api/agents/alice/edit",
            json={"token": admin.admin_token, "color": "#abcdef"},
        )
        assert r.status_code == 200, r.text


# ========= update_task_details (POST /api/update-task-dashboard) =======


async def _create_task(admin) -> str:
    r = admin.client.post(
        "/api/tasks",
        json={"token": admin.admin_token, "task_title": "upd probe"},
    )
    assert r.status_code == 200, r.text
    return r.json()["task_id"]


@pytest.mark.parametrize("field", ["status", "title", "description",
                                    "priority", "assigned_to"])
@pytest.mark.parametrize("bad", _BAD_VALUES)
async def test_update_task_details_rejects_structured_field(tmp_path, field, bad) -> None:
    """A dict/list field must 400 — never the misleading silent-200 no-op
    origin/main returns."""
    async with mcp_session(tmp_path) as admin:
        task_id = await _create_task(admin)
        r = admin.client.post(
            "/api/update-task-dashboard",
            json={"token": admin.admin_token, "task_id": task_id, field: bad},
        )
        assert r.status_code == 400, (
            f"{field}={bad!r} should be 400 not {r.status_code}: {r.text}"
        )


async def test_update_task_details_valid_still_works(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        task_id = await _create_task(admin)
        r = admin.client.post(
            "/api/update-task-dashboard",
            json={
                "token": admin.admin_token,
                "task_id": task_id,
                "title": "renamed",
                "priority": "high",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json().get("success") is True
