"""Contract tests for the settings-schema registry (ADR-0018).

The registry (``agent_mcp/core/settings_schema.py``) is the single
source of truth for every ``config_*`` setting. These tests PIN that
contract so a future edit that flips a default, mis-tiers a key, or
lets the schema drift from the live sysadmin write-gate fails the
build.

The golden-default table is the no-behaviour-change proof for PR 1:
every default here is a hardcoded copy of the value the backend
resolved BEFORE the registry existed. If a default reader now resolves
a different value, this table catches it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import json

import pytest

from agent_mcp.core.settings_schema import (
    KNOWN_SETTING_KEYS,
    SECRET_SETTING_KEYS,
    SETTINGS_SCHEMA,
    default_for,
    spec_for,
)
from agent_mcp.tools.project_settings_tools import _CONFIG_AOE_KEY_RE
from tests.harness import mcp_session


# ---------------------------------------------------------------------------
# Golden defaults — a hardcoded, independent copy of every default the
# backend resolved before the registry existed. This is the
# no-behaviour-change proof: any drift fails here.
# ---------------------------------------------------------------------------

GOLDEN_DEFAULTS: dict[str, object] = {
    "config_allow_worker_to_worker": False,
    "config_allow_worker_self_assign": True,
    "config_allow_worker_create_unassigned": True,
    "config_allow_worker_update_own_status": True,
    "config_auto_event_loop_global": True,
    "config_event_idle_stop_seconds": 604800,
    "config_message_retention_days": 0,
    "config_allow_worker_update_own_profile": True,
    "config_allow_manager_update_own_profile": True,
    "config_allow_manager_curate_profiles": True,
    "config_profile_review_interval_days": 7,
    "config_aoe_notify_enabled": False,
    "config_aoe_base_url": "http://127.0.0.1:8181",
    "config_aoe_bearer_token": None,
    "config_aoe_bearer_token_file": None,
    "config_aoe_notify_template": (
        "[agent-mcp] New message from {sender}. "
        "Call get_agent_messages to read."
    ),
    "config_aoe_timeout_ms": 2000,
}


def test_golden_defaults_unchanged() -> None:
    """Every registered default equals its pre-refactor value."""
    for key, expected in GOLDEN_DEFAULTS.items():
        assert default_for(key) == expected, (
            f"default for {key!r} drifted: {default_for(key)!r} != "
            f"{expected!r} — a settings default changed (behaviour change)"
        )
        # Guard against 1 == True / 0 == False coercion masking a drift.
        assert type(default_for(key)) is type(expected), (
            f"default TYPE for {key!r} drifted"
        )


def test_golden_table_and_schema_cover_the_same_keys() -> None:
    """The golden table and the schema describe exactly the same keys."""
    assert set(GOLDEN_DEFAULTS) == {s.key for s in SETTINGS_SCHEMA}


def test_schema_has_seventeen_ordered_specs() -> None:
    assert len(SETTINGS_SCHEMA) == 17
    # Keys are unique.
    assert len({s.key for s in SETTINGS_SCHEMA}) == 17


# ---------------------------------------------------------------------------
# Hybrid tier-enforcement invariant (ADR-0018): schema.tier MUST agree
# with the live sysadmin write-gate regex for every key. The regex stays
# the enforcer; this test guarantees the schema's tier column (which
# drives the UI) never disagrees with it.
# ---------------------------------------------------------------------------

def test_tier_agrees_with_live_aoe_write_gate() -> None:
    for spec in SETTINGS_SCHEMA:
        gated = bool(_CONFIG_AOE_KEY_RE.match(spec.key))
        assert (spec.tier == "sysadmin") == gated, (
            f"{spec.key!r}: schema.tier={spec.tier!r} disagrees with the "
            f"live _CONFIG_AOE_KEY_RE gate (matches={gated}). The regex is "
            "the enforcer — fix the schema tier to match it."
        )


# ---------------------------------------------------------------------------
# Completeness + secret derivation
# ---------------------------------------------------------------------------

def test_known_setting_keys_complete() -> None:
    """KNOWN_SETTING_KEYS == the config_* keys the backend actually
    reads (the 17 in the golden table)."""
    assert KNOWN_SETTING_KEYS == frozenset(GOLDEN_DEFAULTS)


def test_secret_keys_derived_from_schema() -> None:
    assert SECRET_SETTING_KEYS == {
        "config_aoe_bearer_token",
        "config_aoe_bearer_token_file",
    }
    # Derived, not hand-listed: every secret spec, and only those.
    assert SECRET_SETTING_KEYS == frozenset(
        s.key for s in SETTINGS_SCHEMA if s.type == "secret"
    )


def test_spec_for_unknown_key_is_none() -> None:
    assert spec_for("config_not_a_real_key") is None


# ---------------------------------------------------------------------------
# GET /api/settings-schema endpoint
# ---------------------------------------------------------------------------

def _rows(payload: dict) -> list[dict]:
    return payload["schema"]


@pytest.mark.asyncio
async def test_settings_schema_endpoint_confirmed_operator(
    tmp_path: Path,
) -> None:
    """A CONFIRMED operator-tier bearer gets 200 with all 12 schema rows
    and a caller block reflecting their tier."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get(
            "/api/settings-schema",
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        rows = _rows(body)
        assert len(rows) == 17
        # Row shape carries every schema field the frontend renders.
        first = rows[0]
        assert set(first) == {
            "key", "type", "default", "tier", "group",
            "title", "description", "widget",
        }
        assert {row["key"] for row in rows} == set(GOLDEN_DEFAULTS)
        # A confirmed operator-tier bearer is confirmed (sysadmin flag is
        # not set on the bearer path — see the sysadmin caller test below).
        assert body["caller"]["confirmed_operator"] is True
        assert isinstance(body["caller"]["sysadmin"], bool)


@pytest.mark.asyncio
async def test_settings_schema_endpoint_forwarding_non_confirmed_403(
    tmp_path: Path,
) -> None:
    """A bare-forwarding (non-confirmed) operator gets 403 — mirrors the
    /api/tokens confirmed-tier gate."""
    async with mcp_session(tmp_path) as admin:
        r = admin.get("/api/settings-schema")  # signed forwarding header
        assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_settings_schema_endpoint_requires_auth(tmp_path: Path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get("/api/settings-schema")  # no auth at all
        assert r.status_code in (401, 403), r.text


@pytest.mark.asyncio
async def test_settings_schema_caller_sysadmin_true_for_sysadmin() -> None:
    """The caller block reports sysadmin=True for a sysadmin session.

    Exercised by calling the handler with a sysadmin session auth dict
    directly: the mcp_session harness's confirmed path is a per-agent
    bearer (which carries no sysadmin flag), and only the cookie/session
    path resolves ``auth['sysadmin']`` — so this asserts the caller-block
    construction against a genuine sysadmin auth dict."""
    from agent_mcp.app.routers.settings import settings_schema_api_route

    auth = {
        "kind": "session",
        "project_role": "operator",
        "sysadmin": True,
    }
    resp = await settings_schema_api_route(
        SimpleNamespace(method="GET"), auth=auth,
    )
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["caller"]["sysadmin"] is True
    assert body["caller"]["confirmed_operator"] is True
    assert len(body["schema"]) == 17
