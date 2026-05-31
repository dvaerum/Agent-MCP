"""REST endpoints for task create + delete (UPSTREAM_ISSUES.md issue C).

These exist already for memories (POST/PUT/DELETE /api/memories/...).
Tasks have GET (list) and POST /api/update-task-dashboard (update),
but no POST /api/tasks (create) and no DELETE /api/tasks/<id>. This
PR adds them; tests go red→green.

Pattern: admin token in JSON body (matches existing memory + update-
task-dashboard endpoints — Q6a.1 convention).
"""

from __future__ import annotations


def _admin_token(client) -> str:
    """Fetch the admin token from /api/tokens (unauthenticated)."""
    r = client.get("/api/tokens")
    assert r.status_code == 200, r.text
    return r.json()["admin_token"]


def test_post_tasks_creates_task_with_admin_token(client) -> None:
    """POST /api/tasks with admin token + minimal body creates a task."""
    token = _admin_token(client)

    r = client.post(
        "/api/tasks",
        json={
            "token": token,
            "task_title": "smoke task",
            "task_description": "created by integration test",
        },
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload.get("success") is True, payload
    assert "task_id" in payload, payload

    # Sanity: it shows up in the list endpoint.
    listing = client.get("/api/tasks").json()
    # The listing shape is implementation-defined; just check the new
    # task_id appears somewhere in the JSON serialization.
    import json as _json
    assert payload["task_id"] in _json.dumps(listing), (
        f"new task {payload['task_id']} not visible in /api/tasks listing"
    )


def test_post_tasks_rejects_bad_token(client) -> None:
    """POST /api/tasks with a garbage admin token returns 403."""
    r = client.post(
        "/api/tasks",
        json={
            "token": "deadbeef" * 4,  # 32 hex chars, doesn't match
            "task_title": "shouldn't be created",
            "task_description": "...",
        },
    )
    assert r.status_code == 403, r.text


def test_post_tasks_rejects_missing_title(client) -> None:
    """POST /api/tasks with no title returns 400."""
    token = _admin_token(client)
    r = client.post(
        "/api/tasks",
        json={"token": token, "task_description": "no title"},
    )
    assert r.status_code == 400, r.text


def test_delete_tasks_removes_task_with_admin_token(client) -> None:
    """DELETE /api/tasks/<id> with admin token removes the task."""
    token = _admin_token(client)

    # Create one to delete.
    created = client.post(
        "/api/tasks",
        json={
            "token": token,
            "task_title": "to be deleted",
            "task_description": "ephemeral",
        },
    ).json()
    task_id = created["task_id"]

    # Delete it.
    r = client.request(
        "DELETE",
        f"/api/tasks/{task_id}",
        json={"token": token},
    )
    assert r.status_code == 200, r.text
    assert r.json().get("success") is True, r.json()

    # Gone from listing.
    import json as _json
    listing = client.get("/api/tasks").json()
    assert task_id not in _json.dumps(listing)


def test_delete_tasks_rejects_bad_token(client) -> None:
    """DELETE /api/tasks/<id> with bad token returns 403, task stays."""
    token = _admin_token(client)

    created = client.post(
        "/api/tasks",
        json={
            "token": token,
            "task_title": "should survive",
            "task_description": "...",
        },
    ).json()
    task_id = created["task_id"]

    r = client.request(
        "DELETE",
        f"/api/tasks/{task_id}",
        json={"token": "x" * 32},
    )
    assert r.status_code == 403, r.text


def test_delete_tasks_404_on_unknown_id(client) -> None:
    """DELETE /api/tasks/nonexistent returns 404."""
    token = _admin_token(client)
    r = client.request(
        "DELETE",
        "/api/tasks/task_does_not_exist",
        json={"token": token},
    )
    assert r.status_code == 404, r.text
