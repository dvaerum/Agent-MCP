"""Pentest R9-F2 — REST ``/api/memories`` PUT/DELETE must enforce the
SAME authorization gates as the MCP project_context write tools.

Confirmed live (HIGH): the dashboard cookie/forwarding REST handlers
``update_memory_api_route`` (PUT) and ``delete_memory_api_route``
(DELETE) wrote the ``project_context`` table ORM-DIRECT, skipping every
gate that lives inside the MCP tool impls
(``agent_mcp/tools/project_context_tools.py``):

  1. ``_deny_non_sysadmin_aoe_config`` — ``config_aoe_*`` keys configure
     a machine-level outbound integration and are sysadmin-only (R8-F1
     #481). A non-sysadmin operator ``PUT``-updating ``config_aoe_base_url``
     re-points the server's outbound AoE client (SSRF + bearer-exfil) —
     the R8-F1 vuln reopened via the UPDATE/DELETE surfaces even though
     the POST create surface already 403s it.
  2. ``_deny_viewer_tier_write`` — a read-only viewer-tier operator must
     not mutate project context (SEC1 / #273-#274). The direct-write
     REST path let a signed viewer forwarding header write/delete.
  3. the critical-key / ``force_delete`` guard in
     ``delete_project_context_tool_impl`` — deleting a critical system
     key requires ``force_delete=true``; the REST DELETE never enforced
     it.

The fix routes both handlers through the gated tools
(``update_project_context`` / ``delete_project_context``) exactly as the
POST create handler already dispatches ``create_project_context`` — a
single enforcement path.

RED on origin/main: every ``*_denied_*`` / ``*_requires_force_delete``
test below gets a 2xx (the direct write lands). After the fix they get
403 / 400. The GREEN guardrails (non-config operator writes, forced
critical delete) keep the change tightly scoped.
"""

from __future__ import annotations

import json

import pytest

from tests.harness import mcp_session, seed_config_context_as_sysadmin

pytestmark = pytest.mark.asyncio


# ── DB helpers ────────────────────────────────────────────────────


def _seed_memory(key: str, value: object, created_by: str = "seed-owner") -> None:
    """Insert a project_context row directly (same shape the REST create
    endpoint produces)."""
    import datetime as _dt

    from agent_mcp.db.engine import SessionLocal
    from agent_mcp.db.models import ProjectContext

    now = _dt.datetime.now().isoformat()
    sess = SessionLocal()
    try:
        sess.add(
            ProjectContext(
                context_key=key,
                value=json.dumps(value),
                created_at=now,
                created_by=created_by,
                updated_at=now,
                updated_by=created_by,
                description="seed",
            )
        )
        sess.commit()
    finally:
        sess.close()


def _row_value(key: str):
    """Return the JSON-decoded stored value for ``key`` or ``None`` if the
    row is absent."""
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT value FROM project_context WHERE context_key = ?", (key,)
        )
        r = cur.fetchone()
    finally:
        conn.close()
    if r is None:
        return None
    return json.loads(r["value"])


def _row_exists(key: str) -> bool:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM project_context WHERE context_key = ?", (key,)
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


# ── (1) config_aoe_* — non-sysadmin operator DENIED (R8-F1 reopened) ─


async def test_rest_put_config_aoe_denied_for_operator(tmp_path) -> None:
    """PUT-updating an existing ``config_aoe_base_url`` as a non-sysadmin
    operator is DENIED (403) — the direct-write handler used to let this
    re-point the outbound AoE client (SSRF). RED on main: 200."""
    async with mcp_session(tmp_path) as admin:
        seed_config_context_as_sysadmin(
            "config_aoe_base_url", "http://127.0.0.1:8181"
        )

        r = admin.request(
            "PUT",
            "/api/memories/config_aoe_base_url",
            json={"context_value": "http://attacker.evil:9999"},
        )

        assert r.status_code == 403, r.text
        # The malicious value must NOT have landed.
        assert _row_value("config_aoe_base_url") == "http://127.0.0.1:8181"


async def test_rest_delete_config_aoe_denied_for_operator(tmp_path) -> None:
    """DELETE of a ``config_aoe_*`` key as a non-sysadmin operator is
    DENIED (403). RED on main: 200 (the bearer/base_url got deleted)."""
    async with mcp_session(tmp_path) as admin:
        seed_config_context_as_sysadmin(
            "config_aoe_bearer_token", "s3cr3t-bearer"
        )

        r = admin.request(
            "DELETE",
            "/api/memories/config_aoe_bearer_token",
            json={},
        )

        assert r.status_code == 403, r.text
        assert _row_exists("config_aoe_bearer_token")


# ── (2) viewer-tier operator DENIED on the REST write surfaces ──────


async def test_rest_put_denied_for_viewer(tmp_path) -> None:
    """A signed VIEWER forwarding header must not PUT-mutate a memory
    (SEC1). The direct-write path bypassed ``_deny_viewer_tier_write``.
    RED on main: 200."""
    async with mcp_session(tmp_path) as admin:
        _seed_memory("viewer_probe", {"v": 1})

        r = admin.request(
            "PUT",
            "/api/memories/viewer_probe",
            headers=admin.forwarding_header(role="viewer"),
            json={"context_value": {"v": "hacked"}},
        )

        assert r.status_code == 403, r.text
        assert _row_value("viewer_probe") == {"v": 1}


async def test_rest_delete_denied_for_viewer(tmp_path) -> None:
    """A signed VIEWER forwarding header must not DELETE a memory."""
    async with mcp_session(tmp_path) as admin:
        _seed_memory("viewer_del_probe", {"v": 1})

        r = admin.request(
            "DELETE",
            "/api/memories/viewer_del_probe",
            headers=admin.forwarding_header(role="viewer"),
            json={},
        )

        assert r.status_code == 403, r.text
        assert _row_exists("viewer_del_probe")


# ── (3) critical-key / force_delete guard on REST DELETE ────────────


async def test_rest_delete_critical_key_requires_force_delete(tmp_path) -> None:
    """Deleting a critical system key without ``force_delete`` is rejected
    (400) — the REST DELETE handler never enforced this guard. RED on
    main: 200 (the critical key was removed)."""
    async with mcp_session(tmp_path) as admin:
        _seed_memory("server_startup", {"ts": "boot"})

        r = admin.request(
            "DELETE",
            "/api/memories/server_startup",
            json={},
        )

        assert r.status_code == 400, r.text
        assert _row_exists("server_startup")


async def test_rest_delete_critical_key_with_force_delete_allowed(
    tmp_path,
) -> None:
    """GREEN guardrail: the guard is a guard, not a wall — an operator who
    passes ``force_delete=true`` in the body can still delete a critical
    key."""
    async with mcp_session(tmp_path) as admin:
        _seed_memory("server_startup", {"ts": "boot"})

        r = admin.request(
            "DELETE",
            "/api/memories/server_startup",
            json={"force_delete": True},
        )

        assert r.status_code == 200, r.text
        assert not _row_exists("server_startup")


# ── (4) GREEN regression — operator still writes/deletes plain keys ─


async def test_rest_put_noncritical_key_still_works(tmp_path) -> None:
    """A per-project operator can STILL update an ordinary (non-config,
    non-critical) memory — the fix must not over-restrict."""
    async with mcp_session(tmp_path) as admin:
        _seed_memory("team_motto", "ship it")

        r = admin.request(
            "PUT",
            "/api/memories/team_motto",
            json={"context_value": "ship it faster"},
        )

        assert r.status_code == 200, r.text
        assert _row_value("team_motto") == "ship it faster"


async def test_rest_delete_noncritical_key_still_works(tmp_path) -> None:
    """A per-project operator can STILL delete an ordinary memory."""
    async with mcp_session(tmp_path) as admin:
        _seed_memory("scratch_note", {"tmp": True})

        r = admin.request(
            "DELETE",
            "/api/memories/scratch_note",
            json={},
        )

        assert r.status_code == 200, r.text
        assert not _row_exists("scratch_note")


# ── (5) class-sweep sibling: /api/create-sample-memories ────────────


async def test_create_sample_memories_denied_for_viewer(tmp_path) -> None:
    """The sample-memories writer was the other project_context write
    surface that skipped the tool-layer gates: a signed VIEWER forwarding
    header could seed rows on the backend directly. It now routes through
    the gated ``bulk_update_project_context`` tool → 403 for a viewer,
    with NO rows written. RED on main: 200 (the viewer's write landed)."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/api/create-sample-memories",
            headers=admin.forwarding_header(role="viewer"),
        )

        assert r.status_code == 403, r.text
        assert not _row_exists("api.config.base_url")


async def test_create_sample_memories_operator_still_seeds(tmp_path) -> None:
    """GREEN guardrail: an operator can STILL seed the sample rows."""
    async with mcp_session(tmp_path) as admin:
        r = admin.post("/api/create-sample-memories")

        assert r.status_code == 200, r.text
        assert r.json().get("success") is True
        assert _row_value("api.config.base_url") == "https://api.example.com"
