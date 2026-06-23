"""Regression: /api/all-data must not surface duplicate admin entries.

Background
----------
Before PR #100 ("db-review PR-G1"), admin identity was enforced via
`g.admin_token` alone — there was no row in the `agents` table for
admin. The dashboard's `/api/all-data` endpoint inserted a hardcoded
``{'agent_id': 'Admin', ...}`` entry at index 0 so the Agents view had
something to render for the admin user.

PR #100 added the synthetic ``agent_id='admin'`` row to the `agents`
table so the new foreign-key constraints (agent_messages.{sender_id,
recipient_id} → agents.agent_id, mcp_sessions.agent_id →
agents.agent_id) had a target. After #100, /api/all-data started
returning BOTH:

    [
        {'agent_id': 'Admin', ...},   # hardcoded UI entry (synthesised)
        {'agent_id': 'admin', ...},   # PR #100 synthetic row
        ...real workers...
    ]

The dashboard then showed two Admin entries side-by-side.

Wave 3 (prancy-napping-pie) closed the loop differently: the
hardcoded ``Admin`` synthesis was the surface that leaked
``g.admin_token`` via ``auth_token``. Removing it (and keeping the
lowercase-``admin`` filter) means /api/all-data surfaces ZERO admin
entries — the underlying pseudo-agent row stays in the DB for the
FKs, but neither it nor a synthesised stand-in reaches the
dashboard. Wave 4 will delete the pseudo-agent entirely.

So the regression contract that survives this file is the
zero/one-but-never-two rule: /api/all-data must not surface multiple
agent entries whose ``agent_id`` matches ``admin`` case-insensitively.
The post-Wave-3 count is zero; the original "must be exactly one"
assertion no longer holds.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


async def test_all_data_returns_at_most_one_admin_entry(tmp_path) -> None:
    """GET /api/all-data must return AT MOST one agent entry whose
    agent_id is admin (case-insensitive). Pre-Wave-3 the count was
    exactly one (the synthesised ``Admin`` row); Wave 3 dropped the
    synthesis as part of admin_token retirement, so the count is now
    zero. The invariant that this test pins is the "never two" rule —
    the pre-PR-100 bug was a duplicate, and that must never come back
    regardless of whether the synthesis exists or not."""
    async with mcp_session(tmp_path) as admin:
        resp = admin.get("/api/all-data")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        agents = body.get("agents", [])
        admin_like = [
            a for a in agents
            if str(a.get("agent_id", "")).lower() == "admin"
        ]
        assert len(admin_like) <= 1, (
            f"Expected at most one admin entry in /api/all-data, got "
            f"{len(admin_like)}: {[a.get('agent_id') for a in admin_like]}"
        )


async def test_all_data_with_workers_still_at_most_one_admin(tmp_path) -> None:
    """Adding real workers must not regress the de-dup — at most one
    admin entry regardless of how many workers exist (and zero
    post-Wave-3, since the synthesis is gone)."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")

        resp = admin.get("/api/all-data")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        agents = body.get("agents", [])
        admin_like = [
            a for a in agents
            if str(a.get("agent_id", "")).lower() == "admin"
        ]
        assert len(admin_like) <= 1, (
            f"With real workers present, must still have at most one "
            f"admin entry; got {[a.get('agent_id') for a in admin_like]}"
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
    can talk to" gets a phantom admin entry.

    retire-system-token Wave 1: the harness re-seeds a real 'admin'
    per-agent row in the agents table (for the post-Wave-1 principal),
    which lifespan-replay would then load into active_agents. So this
    test runs lifespan BARE (without the harness's seeding) to
    exercise just the lifespan's own behaviour."""
    from agent_mcp.core import globals as g
    from agent_mcp.app.main_app import create_app
    from starlette.testclient import TestClient

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    app = create_app(project_dir=str(project_dir))
    with TestClient(app):
        # No harness seeding — just the lifespan's own active_agents
        # population path.
        active_ids = {
            data.get("agent_id") for data in g.active_agents.values()
        }
        assert "admin" not in active_ids, (
            f"g.active_agents must not contain the synthetic 'admin' "
            f"pseudo-agent row; found {active_ids}"
        )
