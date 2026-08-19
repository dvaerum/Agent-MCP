"""SEC pentest R2-F2: ``create_task``'s ``priority`` field must be
constrained to the same enum every sibling priority field in this
codebase enforces (``low``/``medium``/``high``).

Before the fix, ``create_task``'s ``inputSchema.properties.priority``
had no ``enum`` — an arbitrary attacker-controlled string persisted
verbatim to the ``tasks.priority`` column (a bare ``Text`` column, no
CHECK constraint) via BOTH the MCP tool and its REST equivalent
(``POST /api/tasks``, a thin adapter over the same
``create_task_tool_impl``/``dispatch_tool_call`` jsonschema-validation
path). Not XSS-exploitable and not crash-inducing (the one downstream
consumer degrades gracefully via ``_PRIORITY_ORDER.get(priority,
default)``) but a genuine data-integrity contract break: any consumer
trusting ``priority in {low, medium, high}`` (dashboard sort/filter,
external integrations) could be handed an arbitrary string.
"""

from __future__ import annotations

import json as _json

import pytest

from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import Invalid
from tests.harness import make_principal, mcp_session

pytestmark = pytest.mark.asyncio


def _operator_principal(user_id: str = "test-operator") -> Principal:
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


def _result_text(result_blocks) -> str:
    parts = [getattr(b, "text", "") for b in result_blocks]
    return "\n".join(p for p in parts if p)


def _is_validation_error(admin, result_blocks) -> bool:
    if not getattr(admin, "_last_is_error", False):
        return False
    return "Input validation error" in _result_text(result_blocks)


def _task_row(task_id: str) -> dict | None:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


_MALICIOUS_PRIORITY = "CRITICAL_XSS_<img src=x onerror=alert(1)>"


# --- RED: MCP tool call path rejects an out-of-enum priority ---

async def test_mcp_create_task_rejects_out_of_enum_priority(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        result = await admin.call(
            "create_task",
            {
                "task_title": "should not be created",
                "priority": _MALICIOUS_PRIORITY,
            },
        )

        assert _is_validation_error(admin, result), (
            "create_task must reject an out-of-enum priority via schema "
            f"validation, same as its 7 sibling priority fields: "
            f"{_result_text(result)}"
        )


# --- RED: REST POST /api/tasks path rejects an out-of-enum priority ---
# The REST route dispatches through ``dispatch_tool_call``, which runs
# the SAME jsonschema validation as the MCP path — so the fix has to
# close both surfaces, not just the MCP one.

async def test_rest_create_task_rejects_out_of_enum_priority(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        resp = admin.post(
            "/api/tasks",
            json={
                "task_title": "should not be created either",
                "priority": _MALICIOUS_PRIORITY,
            },
        )

        assert resp.status_code == 400, resp.text

        # No task row should have been created with the malicious value.
        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM tasks WHERE priority = ?",
                (_MALICIOUS_PRIORITY,),
            )
            assert cur.fetchone() is None, (
                "malicious priority string must never reach the DB"
            )
        finally:
            conn.close()


# --- Defense-in-depth: a direct in-process call of the impl (bypassing
# the jsonschema-validation layer both dispatch_tool_call callers go
# through) is still rejected by the Python-level re-check. ---

async def test_direct_impl_call_rejects_out_of_enum_priority(tmp_path) -> None:
    from agent_mcp.tools.task_tools import create_task_tool_impl

    async with mcp_session(tmp_path):
        result = await create_task_tool_impl(
            {
                "task_title": "bypassing jsonschema entirely",
                "priority": _MALICIOUS_PRIORITY,
            },
            principal=_operator_principal(),
        )
        assert isinstance(result, Invalid), result
        assert result.field == "priority"


# --- GREEN (already passing, pinned so a fix can't regress it): the
# happy-path enum values still work on both surfaces. ---

@pytest.mark.parametrize("priority", ["low", "medium", "high"])
async def test_mcp_create_task_accepts_valid_priority(tmp_path, priority) -> None:
    async with mcp_session(tmp_path) as admin:
        result = await admin.call(
            "create_task",
            {"task_title": f"valid-{priority}", "priority": priority},
        )
        assert not getattr(admin, "_last_is_error", False), (
            _result_text(result)
        )
        task_id = _json.loads(result[-1].text)["task_id"]
        row = _task_row(task_id)
        assert row is not None
        assert row["priority"] == priority


async def test_mcp_create_task_defaults_priority_to_medium(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        result = await admin.call(
            "create_task", {"task_title": "no priority given"}
        )
        assert not getattr(admin, "_last_is_error", False), (
            _result_text(result)
        )
        task_id = _json.loads(result[-1].text)["task_id"]
        row = _task_row(task_id)
        assert row is not None
        assert row["priority"] == "medium"


async def test_rest_create_task_accepts_valid_priority(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        resp = admin.post(
            "/api/tasks",
            json={"task_title": "rest valid priority", "priority": "high"},
        )
        assert resp.status_code == 200, resp.text
        task_id = resp.json()["task_id"]
        row = _task_row(task_id)
        assert row is not None
        assert row["priority"] == "high"


async def test_rest_create_task_defaults_priority_to_medium(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        resp = admin.post(
            "/api/tasks", json={"task_title": "rest no priority"}
        )
        assert resp.status_code == 200, resp.text
        task_id = resp.json()["task_id"]
        row = _task_row(task_id)
        assert row is not None
        assert row["priority"] == "medium"
