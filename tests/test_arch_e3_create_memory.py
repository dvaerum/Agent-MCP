"""E3 (architecture-deepening) — one implementation behind the
create-memory mutation route.

Before E3, project-context creation existed ONLY as hand-rolled inline
SQLAlchemy inside ``app/routers/memories.py::create_memory_api_route`` —
the REST route WAS the implementation, with no MCP ``create_project_context``
tool (the registry had ``update_``/``delete_project_context`` but not
create). E3 extracts it as a real tool
(``tools/project_context_tools.create_project_context_tool_impl``); the
route becomes a thin adapter that dispatches to the tool.

project_context is a SQLAlchemy ORM table, so — unlike E1/E2's raw-uow
tools — this tool stays ORM-based (``SessionLocal``), matching its
``update_``/``delete_`` siblings. An ORM-session-aware unit_of_work
variant is a separate follow-up (see the arch-deepening plan's design
notes).

The keystone invariant this pins: the MCP tool path and the REST route
path are ONE implementation, so they produce IDENTICAL effects. The
one-path test drives ``create_project_context`` (MCP) and
``POST /api/memories`` (REST) on two identical payloads and asserts the
resulting ``project_context`` row + ``created_memory`` audit action are
the same (modulo the deliberately-distinct key + wall-clock timestamps).
"""

from __future__ import annotations

import json

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


# ---- raw-DB read helpers (authoritative source, bypass caches) --------


def _context_row(context_key: str) -> dict | None:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM project_context WHERE context_key = ?",
            (context_key,),
        )
        r = cur.fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _created_memory_actions(context_key: str) -> list[dict]:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT agent_id, action_type, details FROM agent_actions "
            "WHERE action_type = 'created_memory'",
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            details = json.loads(r.get("details") or "{}")
        except (json.JSONDecodeError, TypeError):
            details = {}
        if details.get("context_key") == context_key:
            out.append({"agent_id": r["agent_id"], "details": details})
    return out


def _snapshot(context_key: str) -> dict:
    """Surface-agnostic view of a created memory's observable effects.

    Excludes the ``context_key`` itself and the wall-clock ``created_at`` /
    ``updated_at`` (which legitimately differ between two calls) so the MCP
    and REST results compare equal iff the create behaviour is identical.
    """
    row = _context_row(context_key)
    actions = _created_memory_actions(context_key)
    return {
        "row_present": row is not None,
        "value": row["value"] if row else None,
        "description": row["description"] if row else None,
        "created_by": row["created_by"] if row else None,
        "updated_by": row["updated_by"] if row else None,
        "action_count": len(actions),
        "action_actors": sorted(a["agent_id"] for a in actions),
    }


# ---- the one-path test ------------------------------------------------


async def test_create_memory_mcp_and_rest_identical(tmp_path) -> None:
    """``create_project_context`` (MCP) and ``POST /api/memories`` (REST)
    are ONE implementation: run each over an identical payload and assert
    the resulting project_context row + created_memory audit are the same.
    """
    payload_value = {"structured": ["value", "is", "fine"]}
    async with mcp_session(tmp_path) as admin:
        # MCP surface.
        mcp_result = await admin.call(
            "create_project_context",
            {
                "context_key": "mem.mcp.k1",
                "context_value": payload_value,
                "description": "a shared description",
            },
        )
        assert "created successfully" in mcp_result[0].text.lower(), (
            mcp_result[0].text
        )

        # REST surface.
        resp = admin.client.post(
            "/api/memories",
            json={
                "token": admin.admin_token,
                "context_key": "mem.rest.k1",
                "context_value": payload_value,
                "description": "a shared description",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json().get("success") is True
        assert resp.json().get("message") == (
            "Memory 'mem.rest.k1' created successfully"
        )

        eff_mcp = _snapshot("mem.mcp.k1")
        eff_rest = _snapshot("mem.rest.k1")

        # Identical effects across BOTH surfaces (the one-path proof).
        assert eff_mcp == eff_rest, (
            f"MCP vs REST create diverged:\n  mcp ={eff_mcp}\n"
            f"  rest={eff_rest}"
        )

        # And the effects are actually a correct create (not two no-ops).
        assert eff_mcp["row_present"] is True
        assert eff_mcp["value"] == json.dumps(payload_value)
        assert eff_mcp["description"] == "a shared description"
        assert eff_mcp["created_by"] == "admin"
        assert eff_mcp["updated_by"] == "admin"
        assert eff_mcp["action_count"] == 1
        assert eff_mcp["action_actors"] == ["admin"]


async def test_create_memory_duplicate_key_conflicts_both_surfaces(
    tmp_path,
) -> None:
    """A second create on an existing key is a 409 (REST) / Conflict (MCP)
    on BOTH surfaces — the INSERT-only semantics are shared, not a
    REST-only guard."""
    async with mcp_session(tmp_path) as admin:
        first = admin.client.post(
            "/api/memories",
            json={
                "token": admin.admin_token,
                "context_key": "mem.dup",
                "context_value": {"v": 1},
            },
        )
        assert first.status_code == 200, first.text

        # REST duplicate → 409 with the exact legacy wording.
        dup_rest = admin.client.post(
            "/api/memories",
            json={
                "token": admin.admin_token,
                "context_key": "mem.dup",
                "context_value": {"v": 2},
            },
        )
        assert dup_rest.status_code == 409, dup_rest.text
        assert dup_rest.json()["error"] == "Memory with this key already exists"

        # MCP duplicate → Conflict variant (rendered as a conflict error).
        dup_mcp = await admin.call(
            "create_project_context",
            {"context_key": "mem.dup", "context_value": {"v": 3}},
        )
        assert "conflict" in dup_mcp[0].text.lower(), dup_mcp[0].text

        # The value was NOT overwritten by either failed create.
        row = _context_row("mem.dup")
        assert row is not None
        assert json.loads(row["value"]) == {"v": 1}
