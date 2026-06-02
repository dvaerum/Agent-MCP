"""Integration tests for the extended POST /api/update-task-dashboard.

The dashboard Tasks page Edit modal (this PR) needs to mutate every
editable field of a task — title, description, status, priority,
assigned_to — through a single endpoint. Upstream's
update-task-dashboard already accepts title/description/priority/notes
but it (a) required `status` to be present even for non-status edits
and (b) had no way to assign or unassign an agent.

This PR relaxes the `status` requirement (status is now optional but
at least one editable field must be supplied) and adds `assigned_to`.

Migrated to `tests/harness.py::mcp_session` (Candidate F from
architecture review 2026-06-02).
"""

from __future__ import annotations

import json as _json

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


def _create_task(admin, **overrides) -> str:
    body = {
        "token": admin.admin_token,
        "task_title": overrides.get("title", "edit-target"),
        "task_description": overrides.get("description", "an edit-target task"),
    }
    if "priority" in overrides:
        body["priority"] = overrides["priority"]
    r = admin.client.post("/api/tasks", json=body)
    assert r.status_code == 200, r.text
    return r.json()["task_id"]


async def test_update_task_dashboard_accepts_title_only(tmp_path) -> None:
    """Sending only {token, task_id, title} must succeed (no status required)."""
    async with mcp_session(tmp_path) as admin:
        task_id = _create_task(admin)

        r = admin.client.post(
            "/api/update-task-dashboard",
            json={
                "token": admin.admin_token,
                "task_id": task_id,
                "title": "new title",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json().get("success") is True

        listing = _json.dumps(admin.client.get("/api/tasks").json())
        assert "new title" in listing


async def test_update_task_dashboard_accepts_description_only(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        task_id = _create_task(admin)

        r = admin.client.post(
            "/api/update-task-dashboard",
            json={
                "token": admin.admin_token,
                "task_id": task_id,
                "description": "freshly edited body",
            },
        )
        assert r.status_code == 200, r.text


async def test_update_task_dashboard_accepts_priority_only(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        task_id = _create_task(admin)

        r = admin.client.post(
            "/api/update-task-dashboard",
            json={
                "token": admin.admin_token,
                "task_id": task_id,
                "priority": "high",
            },
        )
        assert r.status_code == 200, r.text


async def test_update_task_dashboard_accepts_assigned_to(tmp_path) -> None:
    """Edit modal can assign a task to an arbitrary agent_id string via
    this endpoint. The endpoint stores assigned_to verbatim; agent
    existence is enforced (or not) by upstream consumers."""
    async with mcp_session(tmp_path) as admin:
        task_id = _create_task(admin)

        r = admin.client.post(
            "/api/update-task-dashboard",
            json={
                "token": admin.admin_token,
                "task_id": task_id,
                "assigned_to": "edit-target-agent",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json().get("success") is True

        listing = _json.dumps(admin.client.get("/api/tasks").json())
        assert "edit-target-agent" in listing


async def test_update_task_dashboard_unassigns_with_empty_assigned_to(
    tmp_path,
) -> None:
    """Passing assigned_to='' (or null) must clear the assignment."""
    async with mcp_session(tmp_path) as admin:
        task_id = _create_task(admin)

        # Assign first.
        admin.client.post(
            "/api/update-task-dashboard",
            json={
                "token": admin.admin_token,
                "task_id": task_id,
                "assigned_to": "to-unassign",
            },
        )

        # Now unassign.
        r = admin.client.post(
            "/api/update-task-dashboard",
            json={
                "token": admin.admin_token,
                "task_id": task_id,
                "assigned_to": None,
            },
        )
        assert r.status_code == 200, r.text

        # And the listing no longer mentions the agent.
        listing = _json.dumps(admin.client.get("/api/tasks").json())
        assert "to-unassign" not in listing


async def test_update_task_dashboard_requires_at_least_one_field(
    tmp_path,
) -> None:
    """Sending nothing-but-the-task_id is a 400 (no-op rejected)."""
    async with mcp_session(tmp_path) as admin:
        task_id = _create_task(admin)

        r = admin.client.post(
            "/api/update-task-dashboard",
            json={"token": admin.admin_token, "task_id": task_id},
        )
        assert r.status_code == 400, r.text


async def test_update_task_dashboard_status_still_works(tmp_path) -> None:
    """Backwards compatibility: status-only updates (the old path) still
    work."""
    async with mcp_session(tmp_path) as admin:
        task_id = _create_task(admin)

        r = admin.client.post(
            "/api/update-task-dashboard",
            json={
                "token": admin.admin_token,
                "task_id": task_id,
                "status": "in_progress",
            },
        )
        assert r.status_code == 200, r.text
