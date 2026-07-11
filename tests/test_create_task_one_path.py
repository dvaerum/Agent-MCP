"""E1 (arch-deepening): POST /api/tasks and the create_task MCP tool are
ONE path.

Both surfaces dispatch the same ``create_task_tool_impl`` on the
unit-of-work, so they MUST produce identical effects. This test proves
it end-to-end: the two paths create structurally-identical task rows,
write the same single ``created_task`` audit action, and publish the
same ``task.created`` EventBus event with the same payload shape.

The one field that legitimately differs is ``created_by`` — it names
whoever called (the REST operator vs the MCP admin bearer), which is
correct provenance, not drift.
"""

from __future__ import annotations

import json as _json

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


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


def _audit_actions(task_id: str) -> list[str]:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT action_type FROM agent_actions WHERE task_id = ?",
            (task_id,),
        )
        return [r["action_type"] for r in cur.fetchall()]
    finally:
        conn.close()


# Fields that MUST match byte-for-byte across the two surfaces (all the
# create semantics). ``created_by`` is excluded — it names the caller.
_STRUCTURAL_FIELDS = (
    "title",
    "description",
    "status",
    "priority",
    "assigned_to",
    "parent_task",
    "required_capabilities",
    "child_tasks",
    "depends_on_tasks",
)


async def test_rest_and_mcp_create_task_are_one_path(
    tmp_path, monkeypatch
) -> None:
    async with mcp_session(tmp_path) as admin:
        # Spy on the EventBus publish funnel both paths' uow.emit flows
        # through, so we can prove each fired ``task.created``.
        import agent_mcp.core.repositories._event_bus_shim as _shim

        published: list[tuple] = []
        _orig_publish = _shim.publish

        def _spy(addressee, event_type, payload):
            published.append((addressee, event_type, dict(payload)))
            return _orig_publish(addressee, event_type, payload)

        monkeypatch.setattr(_shim, "publish", _spy)

        body = {
            "task_title": "one path",
            "task_description": "same effects on both surfaces",
        }

        # --- REST: POST /api/tasks ---
        r = admin.client.post(
            "/api/tasks", json={"token": admin.admin_token, **body}
        )
        assert r.status_code == 200, r.text
        assert r.json().get("success") is True, r.json()
        rest_id = r.json()["task_id"]

        # --- MCP: create_task tool ---
        result = await admin.call("create_task", dict(body))
        assert not getattr(admin, "_last_is_error", False), (
            [b.text for b in result]
        )
        # Ok renders [message, json(data)]; the data block carries task_id.
        mcp_id = _json.loads(result[-1].text)["task_id"]
        assert mcp_id and mcp_id != rest_id

        # --- Same row shape (one implementation, not two) ---
        rest_row = _task_row(rest_id)
        mcp_row = _task_row(mcp_id)
        assert rest_row is not None and mcp_row is not None
        for field in _STRUCTURAL_FIELDS:
            assert rest_row[field] == mcp_row[field], (
                f"{field}: REST={rest_row[field]!r} MCP={mcp_row[field]!r}"
            )
        # Both are unassigned top-level tasks named identically.
        assert rest_row["status"] == "unassigned"
        assert rest_row["title"] == "one path"
        # created_by legitimately differs — it names the caller.
        assert rest_row["created_by"] and mcp_row["created_by"]

        # --- Same audit: exactly one 'created_task' DB row each, and NO
        # extra in-memory sink was added (single-sink parity). ---
        assert _audit_actions(rest_id) == ["created_task"]
        assert _audit_actions(mcp_id) == ["created_task"]

        # --- Same event: both published 'task.created' with the same
        # payload shape. ---
        created = [
            payload
            for (_addr, event_type, payload) in published
            if event_type == "task.created"
        ]
        ids = {p["task_id"] for p in created}
        assert rest_id in ids, published
        assert mcp_id in ids, published
        for payload in created:
            assert set(payload.keys()) == {
                "task_id",
                "status",
                "assigned_to",
            }, payload
