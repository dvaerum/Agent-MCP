"""REST endpoints for task create + delete (UPSTREAM_ISSUES.md issue C).

These exist already for memories (POST/PUT/DELETE /api/memories/...).
Tasks have GET (list) and POST /api/update-task-dashboard (update),
but no POST /api/tasks (create) and no DELETE /api/tasks/<id>. This
PR adds them; tests go red→green.

Pattern: admin token in JSON body (matches existing memory + update-
task-dashboard endpoints — Q6a.1 convention).

Migrated to `tests/harness.py::mcp_session` (Candidate F from
architecture review 2026-06-02). The harness exposes the same
httpx TestClient on `admin.client`, so the assertions are byte-
identical to the legacy fixture.
"""

from __future__ import annotations

import json as _json

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


async def test_post_tasks_creates_task_with_admin_token(tmp_path) -> None:
    """POST /api/tasks with admin token + minimal body creates a task."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/api/tasks",
            json={
                "token": admin.admin_token,
                "task_title": "smoke task",
                "task_description": "created by integration test",
            },
        )
        assert r.status_code == 200, r.text
        payload = r.json()
        assert payload.get("success") is True, payload
        assert "task_id" in payload, payload

        # Sanity: it shows up in the list endpoint.
        listing = admin.client.get("/api/tasks").json()
        assert payload["task_id"] in _json.dumps(listing), (
            f"new task {payload['task_id']} not visible in /api/tasks listing"
        )


async def test_post_tasks_rejects_bad_token(tmp_path) -> None:
    """POST /api/tasks with a garbage admin token returns 403."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/api/tasks",
            json={
                "token": "deadbeef" * 4,  # 32 hex chars, doesn't match
                "task_title": "shouldn't be created",
                "task_description": "...",
            },
        )
        assert r.status_code == 401, r.text


async def test_post_tasks_rejects_missing_title(tmp_path) -> None:
    """POST /api/tasks with no title returns 400 + the legacy
    'task_title is required' error (legitimate missing-field signal,
    unchanged by F004)."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/api/tasks",
            json={"token": admin.admin_token, "task_description": "no title"},
        )
        assert r.status_code == 400, r.text
        assert r.json().get("error") == "task_title is required", r.json()


async def test_post_tasks_rejects_whitespace_only_title(tmp_path) -> None:
    """F004: a title that was sent but contains only whitespace returns
    400 with the distinct ``task_title_empty_after_strip`` error — not
    the misleading 'task_title is required' message that implies the
    field was omitted entirely (verify-all-v6 MUTATING #3)."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/api/tasks",
            json={
                "token": admin.admin_token,
                "task_title": "   ",
                "task_description": "whitespace only",
            },
        )
        assert r.status_code == 400, r.text
        body = r.json()
        assert body.get("error") == "task_title_empty_after_strip", body


async def test_post_tasks_rejects_control_chars_only_title(tmp_path) -> None:
    """F004 v6 case: literal NULL/control bytes in the JSON body get
    stripped by the input sanitizer (utils/json_utils.py step 3),
    leaving an empty string. The handler must still distinguish this
    from a missing field so the operator sees the right remediation
    hint.

    Bearer auth (not body-token) because the auth dep parses the body
    with strict ``json.loads`` which rejects literal control bytes —
    we want the auth path to admit, then exercise the
    sanitised-body path inside the handler.
    """
    async with mcp_session(tmp_path) as admin:
        # Raw bytes — control chars in the JSON literal trigger the
        # sanitizer's control-char stripping (step 3 in
        # utils/json_utils.py::sanitize_json_input) BEFORE JSON parsing.
        body = b'{"task_title":"\x00\x01","task_description":"ctrl"}'
        r = admin.client.post(
            "/api/tasks",
            content=body,
            headers={
                "content-type": "application/json",
                "Authorization": f"Bearer {admin.admin_token}",
            },
        )
        assert r.status_code == 400, r.text
        payload = r.json()
        assert payload.get("error") == "task_title_empty_after_strip", payload


async def test_delete_tasks_removes_task_with_admin_token(tmp_path) -> None:
    """DELETE /api/tasks/<id> with admin token removes the task."""
    async with mcp_session(tmp_path) as admin:
        created = admin.client.post(
            "/api/tasks",
            json={
                "token": admin.admin_token,
                "task_title": "to be deleted",
                "task_description": "ephemeral",
            },
        ).json()
        task_id = created["task_id"]

        r = admin.client.request(
            "DELETE",
            f"/api/tasks/{task_id}",
            json={"token": admin.admin_token},
        )
        assert r.status_code == 200, r.text
        assert r.json().get("success") is True, r.json()

        # Gone from listing.
        listing = admin.client.get("/api/tasks").json()
        assert task_id not in _json.dumps(listing)


async def test_delete_tasks_rejects_bad_token(tmp_path) -> None:
    """DELETE /api/tasks/<id> with bad token returns 403, task stays."""
    async with mcp_session(tmp_path) as admin:
        created = admin.client.post(
            "/api/tasks",
            json={
                "token": admin.admin_token,
                "task_title": "should survive",
                "task_description": "...",
            },
        ).json()
        task_id = created["task_id"]

        r = admin.client.request(
            "DELETE",
            f"/api/tasks/{task_id}",
            json={"token": "x" * 32},
        )
        assert r.status_code == 401, r.text


async def test_delete_tasks_404_on_unknown_id(tmp_path) -> None:
    """DELETE /api/tasks/nonexistent returns 404."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.request(
            "DELETE",
            "/api/tasks/task_does_not_exist",
            json={"token": admin.admin_token},
        )
        assert r.status_code == 404, r.text
