"""Tests for the dashboard ``GET /agent-mcp/__overview`` endpoint and
the ``GET /agent-mcp/`` → ``/__dashboard/`` redirect, both added in
Phase 3.5a of the router-upstream plan (prancy-napping-pie).

The overview endpoint is consumed by the React overview route
(``/__dashboard/``). It must return a JSON envelope of one record per
registered project containing the fields the cards render (R2 + S2):

  ``name``            : str — project name (canonical).
  ``workspace``       : str — workspace path on disk.
  ``status``          : str — one of
                        ``active``/``idle``/``sleeping``/``stopped``/
                        ``starting``/``failed`` (S2).
  ``last_activity_ts``: float | None — UNIX timestamp, or None if the
                        backend hasn't been seen since router boot.
  ``agents``          : int — count of registered agents in the
                        project's SQLite. 0 if the DB is missing.
  ``tasks``           : int — count of all tasks. 0 if DB missing.
  ``open_messages``   : int — count of agent_messages with read=0.
                        0 if DB missing.
  ``alias``           : list[dict] — alias rows from the registry,
                        ``[]`` if the project has none.

The endpoint must also surface ``multi_tenant`` and (when applicable)
``single_tenant_name`` at the envelope top level so the dashboard can
make the picker's "← All projects" entry conditional without a
separate roundtrip.

The redirect: bare ``GET /agent-mcp/`` is now a 302 to
``/agent-mcp/__dashboard/`` in multi-tenant mode and to
``/agent-mcp/__dashboard/<single-project>/`` in single-tenant mode.
The old HTML index page is gone (absorbed into the React overview;
see ADR-0009 + Phase 3.5c for the wiring-snippets port).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


pytestmark = pytest.mark.asyncio


# ── /__overview shape ──────────────────────────────────────────────


async def test_overview_returns_empty_list_when_no_projects(
    aiohttp_client, router_app,
) -> None:
    """No registered projects → ``{"projects": [], "multi_tenant": true}``."""
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/__overview")

    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("application/json")
    assert resp.headers.get("Cache-Control") == "no-store"
    body = await resp.json()
    assert body["projects"] == []
    assert body["multi_tenant"] is True
    assert "single_tenant_name" not in body or body["single_tenant_name"] is None


async def test_overview_one_project_stopped_no_db(
    aiohttp_client, router_app, register_project,
) -> None:
    """One registered project with no backend running and no SQLite
    file on disk: status ``stopped``, counts all 0, alias ``[]``."""
    workspace = register_project("alpha")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/__overview")

    assert resp.status == 200
    body = await resp.json()
    projects = body["projects"]
    assert len(projects) == 1
    row = projects[0]
    assert row["name"] == "alpha"
    assert row["workspace"] == str(workspace)
    assert row["status"] == "stopped"
    assert row["last_activity_ts"] is None
    assert row["agents"] == 0
    assert row["tasks"] == 0
    assert row["open_messages"] == 0
    assert row["alias"] == []


async def test_overview_counts_from_sqlite(
    aiohttp_client, router_app, register_project,
) -> None:
    """When the project's ``.agent/mcp_state.db`` exists, the endpoint
    runs three COUNT queries (agents, tasks, unread agent_messages)
    and returns the totals."""
    workspace = register_project("counted")
    db_dir = Path(workspace) / ".agent"
    db_dir.mkdir(parents=True, exist_ok=True)
    db = db_dir / "mcp_state.db"
    con = sqlite3.connect(db)
    cur = con.cursor()
    cur.execute(
        "CREATE TABLE agents (agent_id TEXT PRIMARY KEY, status TEXT)"
    )
    cur.executemany(
        "INSERT INTO agents (agent_id, status) VALUES (?, ?)",
        [("a1", "active"), ("a2", "terminated"), ("a3", "active")],
    )
    cur.execute(
        "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, status TEXT)"
    )
    cur.executemany(
        "INSERT INTO tasks (task_id, status) VALUES (?, ?)",
        [("t1", "pending"), ("t2", "completed")],
    )
    cur.execute(
        "CREATE TABLE agent_messages "
        "(message_id TEXT PRIMARY KEY, read INTEGER NOT NULL)"
    )
    cur.executemany(
        "INSERT INTO agent_messages (message_id, read) VALUES (?, ?)",
        [("m1", 0), ("m2", 0), ("m3", 1)],
    )
    con.commit()
    con.close()

    client = await aiohttp_client(router_app)
    resp = await client.get("/agent-mcp/__overview")

    assert resp.status == 200
    body = await resp.json()
    row = body["projects"][0]
    assert row["agents"] == 3
    assert row["tasks"] == 2
    assert row["open_messages"] == 2


async def test_overview_status_active_when_systemd_active_and_fresh(
    aiohttp_client, router_app, register_project, router_module,
    systemctl_stub,
) -> None:
    """``status`` derives from systemd + last_activity buckets:
    backend active AND last activity in the last 5 minutes → ``active``."""
    register_project("hot")
    # Mark the backend systemd unit as active.
    systemctl_stub.active_units.add("agent-mcp@hot.service")
    import time
    router_module.last_active[("hot", "backend")] = time.time()
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/__overview")
    body = await resp.json()
    row = next(r for r in body["projects"] if r["name"] == "hot")
    assert row["status"] == "active"


async def test_overview_status_idle_after_five_minutes(
    aiohttp_client, router_app, register_project, router_module,
    systemctl_stub,
) -> None:
    """Active systemd unit but last-activity > 5min + < 4h → ``idle``."""
    register_project("warm")
    systemctl_stub.active_units.add("agent-mcp@warm.service")
    import time
    router_module.last_active[("warm", "backend")] = time.time() - (10 * 60)
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/__overview")
    body = await resp.json()
    row = next(r for r in body["projects"] if r["name"] == "warm")
    assert row["status"] == "idle"


async def test_overview_alias_rows_surface(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    """When a project has aliases attached (Phase 1b rename keeps the
    old name as a grace-period alias), the overview row's ``alias``
    field lists them with name + expires_at."""
    register_project("renamed")
    router_module._REGISTRY.add_alias("renamed", "oldname")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/__overview")
    body = await resp.json()
    row = next(r for r in body["projects"] if r["name"] == "renamed")
    assert len(row["alias"]) == 1
    entry = row["alias"][0]
    assert entry["name"] == "oldname"
    assert entry["expires_at"].endswith("Z")


# ── Multi-tenant index redirect ─────────────────────────────────────


async def test_multi_tenant_index_redirects_to_dashboard_overview(
    aiohttp_client, router_app,
) -> None:
    """``GET /agent-mcp/`` in multi-tenant mode is now a 302 to
    ``/agent-mcp/__dashboard/`` (decision #2 / ADR-0009)."""
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/", allow_redirects=False)

    assert resp.status == 302
    assert resp.headers["Location"] == "/agent-mcp/__dashboard/"


# ── Single-tenant index redirect ────────────────────────────────────


async def test_single_tenant_index_redirects_to_single_project_dashboard(
    aiohttp_client, router_module, register_project,
) -> None:
    """``GET /agent-mcp/`` in single-tenant mode goes straight to the
    configured project's dashboard (decision #2)."""
    register_project("only")
    app = router_module.make_app(single_tenant_name="only")
    client = await aiohttp_client(app)

    resp = await client.get("/agent-mcp/", allow_redirects=False)

    assert resp.status == 302
    assert resp.headers["Location"] == "/agent-mcp/__dashboard/only/"


async def test_overview_envelope_reports_single_tenant(
    aiohttp_client, router_module, register_project,
) -> None:
    """``GET /__overview`` envelope tells the dashboard which mode the
    router is in so the picker can disable + show only the one project."""
    register_project("only")
    app = router_module.make_app(single_tenant_name="only")
    client = await aiohttp_client(app)

    resp = await client.get("/agent-mcp/__overview")

    assert resp.status == 200
    body = await resp.json()
    assert body["multi_tenant"] is False
    assert body["single_tenant_name"] == "only"
    assert [r["name"] for r in body["projects"]] == ["only"]
