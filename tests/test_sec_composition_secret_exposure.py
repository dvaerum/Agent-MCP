"""Security: dashboard composition endpoints must not leak secrets.

FINDING (owner-authorized security review, 2026-07, same
secret-exposure class as the RAG side-channel):

  * ``GET /api/context-data`` returned the raw project_context with NO
    backend auth dep and NO secret redaction — any project member
    (including a read-only viewer) could read ``config_*_token`` /
    ``config_*_secret`` values. Fix: gate behind
    ``require_operator_session`` + redact secret-keyed values for
    callers that are not CONFIRMED operator tier
    (``is_confirmed_operator_tier``), mirroring ``/api/all-data``'s
    token gate.
  * ``POST /api/create-sample-memories`` was an UNAUTHENTICATED backend
    WRITE to project_context (viewer-write). Fix: gate behind
    ``require_operator_session`` (POST is a mutation → viewers rejected).
  * ``GET /api/all-data`` shipped ``aoe_session_id`` (the AoE
    side-channel session credential) for every agent to the viewer
    tier, while ``/api/node-details`` deliberately strips it. Fix:
    strip ``aoe_session_id`` from all-data too.
"""

from __future__ import annotations

import datetime
import secrets

import pytest

from tests.harness import mcp_session, seed_config_setting_as_sysadmin

pytestmark = pytest.mark.asyncio


_SECRET_VALUE = "SENTINEL-CTX-SECRET-7b21"


def _seed_ctx(admin, *, key: str, value: str) -> None:
    # config_aoe_* is sysadmin-only to write (pentest R8-F1) — seed those
    # keys as a sysadmin would; other keys flow through the REST seam.
    if key.lower().startswith("config_aoe_"):
        seed_config_setting_as_sysadmin(key, value)
        return
    r = admin.post(
        "/api/memories",
        json={
            "context_key": key,
            "context_value": value,
        },
    )
    assert r.status_code == 200, r.text


def _bearer(admin) -> dict[str, str]:
    """Confirmed operator-tier auth (per-agent manager bearer)."""
    return {"Authorization": f"Bearer {admin.admin_token}"}


def _seed_agent_with_aoe(agent_id: str, aoe_session_id: str) -> None:
    from agent_mcp.db.connection import get_db_connection

    now = datetime.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO agents (token, agent_id, created_at, "
            "status, working_directory, color, updated_at, agent_role, "
            "aoe_session_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                secrets.token_hex(16), agent_id, now, "active",
                "/tmp", "#abc", now, "worker", aoe_session_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ── /api/context-data ────────────────────────────────────────────────


async def test_context_data_requires_operator_session(tmp_path) -> None:
    """No auth at all → 401. The route previously had NO auth dep."""
    async with mcp_session(tmp_path) as admin:
        _seed_ctx(admin, key="config_aoe_bearer_token", value=_SECRET_VALUE)
        r = admin.client.get("/api/context-data")
        assert r.status_code == 401, r.text
        assert _SECRET_VALUE not in r.text, "secret leaked in 401 body"


async def test_context_data_redacts_secret_for_non_confirmed_operator(
    tmp_path,
) -> None:
    """A forwarding/session operator (tier unverifiable in the backend —
    could be a viewer on a GET) must NOT receive secret values."""
    async with mcp_session(tmp_path) as admin:
        _seed_ctx(admin, key="config_aoe_bearer_token", value=_SECRET_VALUE)
        _seed_ctx(admin, key="project_readme", value="public-info")

        r = admin.get("/api/context-data")  # signed forwarding header
        assert r.status_code == 200, r.text
        assert _SECRET_VALUE not in r.text, (
            "secret value leaked to non-confirmed-operator via context-data"
        )
        # Non-secret context still flows through.
        assert "project_readme" in r.text
        assert "public-info" in r.text


async def test_settings_data_confirmed_operator_sees_secret(tmp_path) -> None:
    """A confirmed operator-tier bearer still receives the secret value —
    the legitimate admin path must not regress. Wave 11 (ADR-0016): the
    AoE bearer lives in the ``project_settings`` store, so the read seam
    is ``/api/settings-data`` (config rows no longer appear in
    ``/api/context-data`` at all)."""
    async with mcp_session(tmp_path) as admin:
        _seed_ctx(admin, key="config_aoe_bearer_token", value=_SECRET_VALUE)

        r = admin.client.get("/api/settings-data", headers=_bearer(admin))
        assert r.status_code == 200, r.text
        assert _SECRET_VALUE in r.text, (
            "confirmed operator must still see the secret value"
        )

        # And the non-confirmed forwarding path stays masked.
        r = admin.get("/api/settings-data")
        assert r.status_code == 200, r.text
        assert _SECRET_VALUE not in r.text, (
            "AoE bearer leaked to a non-confirmed operator via settings-data"
        )


# ── /api/create-sample-memories ──────────────────────────────────────


async def test_create_sample_memories_requires_operator(tmp_path) -> None:
    """Unauthenticated POST → 401 and NO rows written."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post("/api/create-sample-memories")
        assert r.status_code == 401, r.text

        # Nothing was written.
        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        try:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM project_context "
                "WHERE context_key = 'api.config.base_url'"
            ).fetchone()["c"]
        finally:
            conn.close()
        assert n == 0, "unauthenticated caller wrote sample memories"


async def test_create_sample_memories_operator_succeeds(tmp_path) -> None:
    """Authenticated operator can still seed the sample rows."""
    async with mcp_session(tmp_path) as admin:
        r = admin.post("/api/create-sample-memories")
        assert r.status_code == 200, r.text
        assert r.json().get("success") is True


# ── /api/all-data ────────────────────────────────────────────────────


async def test_all_data_strips_aoe_session_id(tmp_path) -> None:
    """all-data must not ship the AoE side-channel session id to any tier
    (node-details already strips it)."""
    async with mcp_session(tmp_path) as admin:
        aoe = "deadbeefcafe0011"
        _seed_agent_with_aoe("aoe-agent", aoe)

        r = admin.get("/api/all-data")
        assert r.status_code == 200, r.text
        assert aoe not in r.text, "aoe_session_id leaked in all-data body"
        for agent in r.json().get("agents", []):
            assert "aoe_session_id" not in agent, (
                f"all-data agent still carries aoe_session_id: "
                f"{list(agent.keys())}"
            )
