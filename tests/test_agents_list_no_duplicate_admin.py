"""Regression: /api/all-data must not surface BOTH the hardcoded 'Admin'
display row AND the synthetic 'admin' row from the agents table.

Background
----------
Before PR #100 ("db-review PR-G1"), admin identity was enforced via
`g.admin_token` alone — there was no row in the `agents` table for
admin. The dashboard's `/api/all-data` endpoint inserts a hardcoded
``{'agent_id': 'Admin', ...}`` entry at index 0 so the Agents view has
something to render for the admin user.

PR #100 added the synthetic ``agent_id='admin'`` row to the `agents`
table so the new foreign-key constraints (agent_messages.{sender_id,
recipient_id} → agents.agent_id, mcp_sessions.agent_id →
agents.agent_id) had a target. After #100, /api/all-data started
returning BOTH:

    [
        {'agent_id': 'Admin', ...},   # hardcoded UI entry
        {'agent_id': 'admin', ...},   # PR #100 synthetic row
        ...real workers...
    ]

The dashboard shows two Admin entries side-by-side.

Fix: skip the synthetic 'admin' row when building the agents_data
list in `/api/all-data` (and at startup load — see
`test_state_load_skips_admin_pseudo_agent`). The hardcoded 'Admin'
entry stays because the entire frontend keys off
``agent_id === 'Admin'`` for special-case handling (no terminate
button, no edit, special token mapping). The synthetic row remains in
the database — it's still needed for the FKs — it just isn't
surfaced to the UI.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


async def test_all_data_returns_one_admin_entry(tmp_path) -> None:
    """GET /api/all-data must return exactly one agent entry whose
    agent_id is admin (case-insensitive). Before the fix, both 'Admin'
    (hardcoded UI entry) and 'admin' (PR #100 synthetic row) appear."""
    async with mcp_session(tmp_path) as admin:
        resp = admin.client.get("/api/all-data")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        agents = body.get("agents", [])
        admin_like = [
            a for a in agents
            if str(a.get("agent_id", "")).lower() == "admin"
        ]
        assert len(admin_like) == 1, (
            f"Expected exactly one admin entry in /api/all-data, got "
            f"{len(admin_like)}: {[a.get('agent_id') for a in admin_like]}"
        )


async def test_all_data_admin_entry_uses_capital_A_label(tmp_path) -> None:
    """The single admin entry must keep agent_id='Admin' (capital A)
    so the frontend's many `agent_id === 'Admin'` special-case
    branches continue to work."""
    async with mcp_session(tmp_path) as admin:
        resp = admin.client.get("/api/all-data")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        agents = body.get("agents", [])
        admin_like = [
            a for a in agents
            if str(a.get("agent_id", "")).lower() == "admin"
        ]
        assert len(admin_like) == 1
        assert admin_like[0]["agent_id"] == "Admin", (
            f"Admin entry must use the 'Admin' (capital A) label; got "
            f"{admin_like[0]['agent_id']!r}"
        )


async def test_all_data_with_workers_still_returns_one_admin(tmp_path) -> None:
    """Adding real workers must not regress the de-dup — exactly one
    admin entry regardless of how many workers exist."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")

        resp = admin.client.get("/api/all-data")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        agents = body.get("agents", [])
        admin_like = [
            a for a in agents
            if str(a.get("agent_id", "")).lower() == "admin"
        ]
        assert len(admin_like) == 1, (
            f"With real workers present, still expected one admin entry; "
            f"got {[a.get('agent_id') for a in admin_like]}"
        )
        # Workers themselves should still be present.
        worker_ids = {a.get("agent_id") for a in agents}
        assert "alice" in worker_ids
        assert "bob" in worker_ids


async def test_state_load_skips_admin_pseudo_agent(tmp_path) -> None:
    """At lifespan startup `application_startup` loads non-terminated
    agents into `g.active_agents`. The synthetic 'admin' row must NOT
    appear there — otherwise `view_status` (which iterates
    g.active_agents) surfaces it alongside everything else, and any
    other code path that treats g.active_agents as "agents the dashboard
    can talk to" gets a phantom admin entry."""
    from agent_mcp.core import globals as g

    async with mcp_session(tmp_path):
        # admin pseudo-agent row must exist in the DB (migration 0008
        # / startup backstop), but must NOT be loaded into the
        # in-memory active map.
        active_ids = {
            data.get("agent_id") for data in g.active_agents.values()
        }
        assert "admin" not in active_ids, (
            f"g.active_agents must not contain the synthetic 'admin' "
            f"pseudo-agent row; found {active_ids}"
        )
