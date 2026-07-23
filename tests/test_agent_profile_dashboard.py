"""Operator view + edit of an agent's self-authored profile in the web UI.

The ``profile`` (agent self-description, migration 0018) is authored by
the agent via the ``update_agent_profile`` MCP tool. This surfaces it to
the operator dashboard: the agents read path carries ``profile`` and the
``POST /api/agents/<id>/edit`` route accepts a ``profile`` field so an
operator can curate it — going through ``review_profile`` so the
``profile_updated_at`` / ``profile_updated_by`` / ``profile_reviewed_at``
bookkeeping stays correct.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


async def test_operator_can_edit_agent_profile(tmp_path) -> None:
    """POST /api/agents/<id>/edit with a ``profile`` persists it AND bumps
    the content bookkeeping (updated_at + updated_by)."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")

        r = admin.post(
            "/api/agents/alice/edit",
            json={"profile": "Backend dev. Ask me about the router."},
        )
        assert r.status_code == 200, r.text

        from agent_mcp.repositories.agent_repository import get_agent_by_id

        row = get_agent_by_id("alice")
        assert row["profile"] == "Backend dev. Ask me about the router.", row
        # A real content change bumps updated_at + updated_by (operator).
        assert row["profile_updated_at"], "profile_updated_at not bumped"
        assert row["profile_updated_by"], "profile_updated_by not set"
        # Every edit is a review → reviewed_at set too.
        assert row["profile_reviewed_at"], "profile_reviewed_at not bumped"


async def test_operator_can_clear_agent_profile(tmp_path) -> None:
    """An empty-string profile clears the self-description."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        admin.post("/api/agents/alice/edit", json={"profile": "something"})

        r = admin.post("/api/agents/alice/edit", json={"profile": ""})
        assert r.status_code == 200, r.text

        from agent_mcp.repositories.agent_repository import get_agent_by_id

        row = get_agent_by_id("alice")
        assert not row["profile"], f"profile should be cleared; got {row['profile']!r}"


async def test_edit_agent_rejects_structured_profile(tmp_path) -> None:
    """A dict/list profile is a 400 (type-confusion guard)."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = admin.post(
            "/api/agents/alice/edit",
            json={"profile": {"nope": 1}},
        )
        assert r.status_code == 400, r.text


async def test_agents_read_path_exposes_profile(tmp_path) -> None:
    """The dashboard hydration (all-data) carries the profile so the UI can
    display it."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        admin.post("/api/agents/alice/edit", json={"profile": "I test things"})

        r = admin.get("/api/all-data")
        assert r.status_code == 200, r.text
        agents = r.json().get("agents", [])
        alice = next((a for a in agents if a.get("agent_id") == "alice"), None)
        assert alice is not None, "alice missing from all-data agents"
        assert alice.get("profile") == "I test things", (
            f"all-data agent must carry profile; got {alice}"
        )
