"""Memory keys use ``/`` as a namespacing convention (``ios/repo``,
``backend-dev/status``, ``cross-repo/…``). ``/`` is an ALLOWED key
character, but the REST update/delete routes used a plain
``/{context_key}`` path param — which does not match a slash — so a
slashed key returned 405 (route miss) and could not be edited or deleted
from the dashboard, even by a sysadmin operator. This pins the
``/{context_key:path}`` fix.

RED against the ``/{context_key}`` routes (405 on the slashed key);
GREEN after ``/{context_key:path}``.
"""

from __future__ import annotations


import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


_SLASH_KEYS = ["ios/improvements-doc", "a/b/c/deep-key"]


def _ctx_row(key: str) -> dict | None:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT context_key, value FROM project_context WHERE context_key = ?",
            (key,),
        )
        r = cur.fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


async def _create(admin, key: str, value: str = "orig") -> None:
    r = admin.post(
        "/api/memories",
        json={
            "context_key": key,
            "context_value": value,
            "description": "slash-route test",
        },
    )
    # Create carries the key in the BODY, so slashes already work here —
    # this is the setup, not the behaviour under test.
    assert r.status_code == 200, f"create {key!r}: {r.status_code} {r.text}"
    assert _ctx_row(key) is not None


@pytest.mark.parametrize("key", _SLASH_KEYS)
async def test_delete_slashed_memory_key(tmp_path, key) -> None:
    async with mcp_session(tmp_path) as admin:
        await _create(admin, key)
        r = admin.request(
            "DELETE",
            f"/api/memories/{key}",
            json={},
        )
        assert r.status_code == 200, (
            f"DELETE of slashed key {key!r} should be 200, got "
            f"{r.status_code}: {r.text}"
        )
        assert _ctx_row(key) is None, f"{key!r} should be gone after delete"


@pytest.mark.parametrize("key", _SLASH_KEYS)
async def test_update_slashed_memory_key(tmp_path, key) -> None:
    async with mcp_session(tmp_path) as admin:
        await _create(admin, key, "orig")
        r = admin.request(
            "PUT",
            f"/api/memories/{key}",
            json={
                "context_value": "updated-value",
                "description": "d",
            },
        )
        assert r.status_code == 200, (
            f"PUT of slashed key {key!r} should be 200, got "
            f"{r.status_code}: {r.text}"
        )
        row = _ctx_row(key)
        assert row is not None, f"{key!r} should still exist after update"
        assert "updated-value" in row["value"]
