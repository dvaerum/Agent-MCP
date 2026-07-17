"""Regression guard: purge must drop the visible agent count by 1.

Dennis's spec for the agent lifecycle, verbatim:

    "When an agent is purged, there should be one less agent than
     before shown on the agent page."

Pre-fix this contract was silently broken by the purge cascade
(PR-G1, post-PR #100 FK constraints). The cascade INSERTs a
`[deleted-<agent_id>]` row with `status='tombstone'` into the
`agents` table so `agent_messages.{sender_id, recipient_id}` (now FK
to `agents.agent_id`) can be UPDATE'd to point at the tombstone
without the FK firing. The original agent row is then DELETE'd.

Net effect on the visible count: the original row is gone but a new
tombstone row takes its place, so the dashboard's agents-page total
stays the same instead of dropping by 1. Live verification from
washing-brothers production (2026-06-04):

    Before purge:  6 agents (Admin, smoke-lifecycle-..., 2 prior
                   tombstones, ios-app-dev, backend-dev)
    After purge:   6 agents — smoke-lifecycle-... → replaced by
                   [deleted-smoke-lifecycle-...] tombstone row

The fix is in the REST shape the dashboard reads (`/api/all-data`
via `useDataStore.fetchAllData` and `/api/agents` via the
`getAgents` list endpoint): both must skip rows with
`status='tombstone'` the same way `/api/all-data` already skips the
synthetic `admin` pseudo-row. The tombstone rows MUST stay in the
DB (FK targets) — they just don't belong in the user-facing list.

The MCP tool surface (`view_status` and friends) gets the same
filter so a `tools/list` consumer's notion of "how many agents
exist" matches the dashboard's.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session


def _insert_tombstone(agent_id: str) -> None:
    """Mirror the purge cascade's INSERT OR IGNORE of the tombstone row."""
    import datetime as _dt

    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO agents "
            "(token, agent_id, capabilities, created_at, status, "
            " working_directory, color, updated_at) "
            "VALUES (?, ?, '[]', ?, 'tombstone', '', '#000000', ?)",
            (
                f"__tombstone_{agent_id}",
                f"[deleted-{agent_id}]",
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


# -------------------- /api/all-data ------------------------------------


@pytest.mark.asyncio
async def test_all_data_excludes_tombstone_rows(tmp_path) -> None:
    """The dashboard's primary data feed (/api/all-data) MUST omit
    tombstone rows. The dashboard's agents table reads from this
    response; tombstone leakage = the count never drops on purge.
    """
    async with mcp_session(tmp_path) as admin:
        # Seed a few non-tombstone agents + one tombstone row (simulates
        # a previously-purged agent).
        await admin.create_worker("alice")
        await admin.create_worker("bob")
        _insert_tombstone("ghost")

        resp = admin.get("/api/all-data")
        assert resp.status_code == 200, resp.text
        agents = resp.json().get("agents", [])
        ids = [a["agent_id"] for a in agents]
        statuses = [a["status"] for a in agents]

        # The two real workers should be present. Wave 3
        # (prancy-napping-pie) dropped the synthesised ``Admin``
        # display row as part of admin_token retirement, so the
        # ``"Admin" in ids`` assertion no longer holds — Wave 4 will
        # decide the post-pseudo-agent admin surface.
        assert "alice" in ids, ids
        assert "bob" in ids, ids

        # The tombstone row must NOT be in the response.
        assert "[deleted-ghost]" not in ids, (
            f"tombstone row leaked into /api/all-data agents: {ids}"
        )
        assert "tombstone" not in statuses, (
            f"any status='tombstone' row in /api/all-data is a leak: "
            f"{list(zip(ids, statuses))}"
        )


# -------------------- /api/agents --------------------------------------


@pytest.mark.asyncio
async def test_agents_list_excludes_tombstone_rows(tmp_path) -> None:
    """GET /api/agents (the back-compat list endpoint, used by various
    dashboard widgets — messages dropdown, graph data, etc.) must
    also filter out tombstone rows."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("carol")
        _insert_tombstone("phantom")

        resp = admin.client.get("/api/agents")
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        ids = [r["agent_id"] for r in rows]
        statuses = [r.get("status") for r in rows]

        assert "carol" in ids
        # Wave 4: the synthetic 'Admin' row was retired alongside the
        # admin pseudo-agent. The endpoint must no longer surface it.
        assert "Admin" not in ids, (
            f"synthetic 'Admin' row resurfaced in /api/agents post-Wave-4: "
            f"{ids}"
        )
        assert "[deleted-phantom]" not in ids, (
            f"tombstone row leaked into /api/agents: {ids}"
        )
        assert "tombstone" not in statuses, (
            f"any status='tombstone' in /api/agents is a leak: "
            f"{list(zip(ids, statuses))}"
        )


@pytest.mark.asyncio
async def test_agents_list_with_status_filter_excludes_tombstone(tmp_path) -> None:
    """GET /api/agents?status=tombstone should NOT return tombstone
    rows either — they're a DB-internal FK artefact, not a queryable
    status from the operator's perspective. Returning the empty list
    is the safe answer."""
    async with mcp_session(tmp_path) as admin:
        _insert_tombstone("alpha")
        _insert_tombstone("beta")

        resp = admin.client.get("/api/agents?status=tombstone")
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        assert rows == [], (
            f"GET /api/agents?status=tombstone should be empty (tombstone "
            f"rows are internal); got {rows}"
        )


# -------------------- end-to-end: purge drops count --------------------


@pytest.mark.asyncio
async def test_purge_drops_visible_agent_count_by_one(tmp_path) -> None:
    """The full create → terminate → purge flow that Dennis's spec
    actually pins: count BEFORE purge minus count AFTER purge == 1.

    Uses the dashboard's REST surface: POST /api/agents/register (the
    Wave 7 PR 1 register-only sibling — pre-cutover this hit POST
    /api/agents and orphan-stormed claude processes via the spawn
    impl), POST /api/terminate-agent, DELETE
    /api/agents/<id>?cascade=true. Re-fetches /api/all-data and
    asserts the agent count drops by exactly 1 — NOT zero (the
    tombstone-leak bug) and NOT more than one (would imply a cascade
    glitch removing siblings).
    """
    async with mcp_session(tmp_path) as admin:
        before_total = len(
            admin.get("/api/all-data").json()["agents"]
        )

        # 1. Register (Wave 7 PR 1: was POST /api/agents — spawn path).
        r = admin.post(
            "/api/agents/register",
            json={
                "agent_id": "spec-lifecycle-target",
                "capabilities": ["test"],
            },
        )
        assert r.status_code == 200, r.text
        after_create = len(
            admin.get("/api/all-data").json()["agents"]
        )
        assert after_create == before_total + 1, (
            f"create should add 1: before={before_total} after={after_create}"
        )

        # 2. Terminate (soft-delete — count stays)
        r = admin.post(
            "/api/terminate-agent",
            json={
                "agent_id": "spec-lifecycle-target",
            },
        )
        assert r.status_code == 200, r.text
        after_terminate = len(
            admin.get("/api/all-data").json()["agents"]
        )
        assert after_terminate == after_create, (
            f"terminate is soft-delete; count must stay: "
            f"after_create={after_create} after_terminate={after_terminate}"
        )

        # 3. Purge (hard-delete — count MUST drop by exactly 1)
        r = admin.request(
            "DELETE",
            "/api/agents/spec-lifecycle-target?cascade=true",
            json={},
        )
        assert r.status_code == 200, r.text
        after_purge = len(
            admin.get("/api/all-data").json()["agents"]
        )
        assert after_purge == after_terminate - 1, (
            f"purge must drop visible agent count by exactly 1 "
            f"(Dennis's spec). after_terminate={after_terminate} "
            f"after_purge={after_purge} delta={after_terminate - after_purge}. "
            f"If delta is 0, tombstone rows are leaking into /api/all-data; "
            f"see test_all_data_excludes_tombstone_rows."
        )
        # And specifically: the purged agent must be gone, AND no
        # tombstone row for it should be in the response.
        ids = [
            a["agent_id"]
            for a in admin.get("/api/all-data").json()["agents"]
        ]
        assert "spec-lifecycle-target" not in ids
        assert "[deleted-spec-lifecycle-target]" not in ids, (
            f"tombstone row visible post-purge: {ids}"
        )
