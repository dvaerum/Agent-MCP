"""Tests for the alias management endpoints added in Phase 3.5c of
the router-upstream plan (prancy-napping-pie).

Two endpoints back the dashboard's alias-chip expansion panel:

  ``GET  /agent-mcp/__alias-usage?alias=<name>``
      Returns ``{alias, project, expires_at, agents}`` where ``agents``
      is the list of agent_ids that have used the alias (from
      ``mcp_sessions.alias_used`` if the column exists; empty list
      if the project's SQLite is missing or hasn't recorded any
      yet). Allows the operator to see "who's still on the old
      name" before deciding to expire the alias.

  ``POST /agent-mcp/__remove-alias`` (form: ``name``, ``alias``)
      Removes the alias entry immediately (skipping the grace
      reaper). Useful when an operator confirms no agent is still
      using the old name. Returns the updated alias list for the
      project.

Both endpoints are disabled (410) in single-tenant mode for the
same reason the other write endpoints are: there's no rename
surface in N=1 mode, so there's no alias surface either.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


pytestmark = pytest.mark.asyncio


# ── __alias-usage ──────────────────────────────────────────────────


async def test_alias_usage_returns_empty_for_known_alias_no_db(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    """Project has an alias but no SQLite file (fresh project, never
    been hit) → ``{alias, project, expires_at, agents: []}``."""
    register_project("real")
    router_module._REGISTRY.add_alias("real", "oldname")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/__alias-usage?alias=oldname")

    assert resp.status == 200
    body = await resp.json()
    assert body["alias"] == "oldname"
    assert body["project"] == "real"
    assert body["expires_at"].endswith("Z")
    assert body["agents"] == []


async def test_alias_usage_404s_for_unknown_alias(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("real")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/__alias-usage?alias=nope")

    assert resp.status == 404


async def test_alias_usage_lists_agents_from_mcp_sessions(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    """When mcp_sessions has rows with alias_used set, the endpoint
    surfaces the distinct agent_ids."""
    workspace = register_project("real")
    router_module._REGISTRY.add_alias("real", "oldname")

    db_dir = Path(workspace) / ".agent"
    db_dir.mkdir(parents=True, exist_ok=True)
    db = db_dir / "mcp_state.db"
    con = sqlite3.connect(db)
    cur = con.cursor()
    cur.execute(
        "CREATE TABLE mcp_sessions ("
        "session_id TEXT PRIMARY KEY, agent_id TEXT, alias_used TEXT, "
        "last_seen_at TEXT)"
    )
    cur.executemany(
        "INSERT INTO mcp_sessions (session_id, agent_id, alias_used, last_seen_at) "
        "VALUES (?, ?, ?, ?)",
        [
            ("s1", "agent-alpha", "oldname", "2026-06-01T10:00:00Z"),
            ("s2", "agent-beta",  "oldname", "2026-06-02T10:00:00Z"),
            ("s3", "agent-alpha", "oldname", "2026-06-03T10:00:00Z"),
            ("s4", "agent-other", "differentalias", "2026-06-03T10:00:00Z"),
            ("s5", "agent-fresh", None,    "2026-06-03T10:00:00Z"),
        ],
    )
    con.commit()
    con.close()

    client = await aiohttp_client(router_app)
    resp = await client.get("/agent-mcp/__alias-usage?alias=oldname")

    assert resp.status == 200
    body = await resp.json()
    assert sorted(body["agents"]) == ["agent-alpha", "agent-beta"]


# ── __remove-alias ─────────────────────────────────────────────────


async def test_remove_alias_drops_entry_immediately(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    register_project("real")
    router_module._REGISTRY.add_alias("real", "oldname")
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/__remove-alias",
        data={"name": "real", "alias": "oldname"},
        headers={"Accept": "application/json"},
    )

    assert resp.status == 200
    body = await resp.json()
    assert body["removed"] == "oldname"
    assert body["project"] == "real"
    assert body["remaining_aliases"] == []
    # Verify on disk
    assert router_module._REGISTRY.resolve_alias("oldname") is None


async def test_remove_alias_400s_on_missing_project(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/__remove-alias",
        data={"name": "ghost", "alias": "oldname"},
        headers={"Accept": "application/json"},
    )
    assert resp.status == 404


async def test_remove_alias_disabled_in_single_tenant(
    aiohttp_client, router_module, register_project,
) -> None:
    register_project("only")
    app = router_module.make_app(single_tenant_name="only")
    client = await aiohttp_client(app)

    resp = await client.post(
        "/agent-mcp/__remove-alias",
        data={"name": "only", "alias": "x"},
    )
    assert resp.status == 410
