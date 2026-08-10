"""Arch round-2 candidate #6 — one config-read seam + truthful cap vocab.

Two invariants:

1. **Bundle-subset smoke test** (Part A). The role bundles remain
   strict subsets of ``KNOWN_CAPABILITIES``. The round-2 review
   proposed pruning 8 "unenforced" caps; verification showed all 8 are
   live, admin-assignable group-capability vocabulary surfaced by the
   dashboard groups UI + validated by the group-capabilities API, so
   they were KEPT. This test pins that the vocabulary + bundles stay
   internally consistent regardless of that decision.

2. **Unified config-read seam** (Part B).
   ``message_retention._read_retention_days`` used to re-type the
   ``SELECT value FROM project_context`` + coercion. It now routes
   through the canonical ``tools.access._get_config_bool`` /
   ``_get_config_int``. These tests assert a value read via the unified
   seam == what the old per-site coercion produced, i.e. behaviour is
   unchanged.
"""

from __future__ import annotations

import datetime as _dt
import json

import pytest

from tests.harness import mcp_session

# --------------------------------------------------------------------------
# Part A — bundle-subset smoke test (still passes post-review)
# --------------------------------------------------------------------------


def test_bundles_are_subsets_of_known_capabilities() -> None:
    from agent_mcp.core.capabilities import (
        AGENT_ROLE_BUNDLES,
        KNOWN_CAPABILITIES,
        PROJECT_ROLE_BUNDLES,
    )

    for name, bundle in PROJECT_ROLE_BUNDLES.items():
        assert bundle <= KNOWN_CAPABILITIES, (
            f"PROJECT_ROLE_BUNDLES[{name!r}] grants caps outside "
            f"KNOWN_CAPABILITIES: {bundle - KNOWN_CAPABILITIES}"
        )
    for name, bundle in AGENT_ROLE_BUNDLES.items():
        assert bundle <= KNOWN_CAPABILITIES, (
            f"AGENT_ROLE_BUNDLES[{name!r}] grants caps outside "
            f"KNOWN_CAPABILITIES: {bundle - KNOWN_CAPABILITIES}"
        )


def test_reviewed_caps_are_retained_not_pruned() -> None:
    """The 8 caps the review flagged are KEPT — they're live group-cap
    vocabulary surfaced by the dashboard + group-capabilities API."""
    from agent_mcp.core.capabilities import KNOWN_CAPABILITIES

    reviewed = {
        "agents.view",
        "agents.use",
        "memories.view",
        "messages.view",
        "messages.send",
        "coordination.wait",
        "system.view",
        "rag.rebuild",
    }
    assert reviewed <= KNOWN_CAPABILITIES


# --------------------------------------------------------------------------
# Part B — unified config-read seam == old per-site coercion
# --------------------------------------------------------------------------


def _seed_ctx(key: str, stored_value: str) -> None:
    """Write a raw project_settings row (value stored verbatim) —
    ADR-0016: the config-read seams consult project_settings."""
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO project_settings "
        "(context_key, value, description, created_at, created_by, "
        "updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (key, stored_value, "arch-r2-6 test", now, "test", now, "test"),
    )
    conn.commit()
    conn.close()


def _old_bool_coerce(raw: str | None, default: bool) -> bool:
    """Reference impl of the pre-refactor bool coercion."""
    if raw is None:
        return default
    s = raw.strip().strip('"').lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    return default


# (stored project_context value, default) pairs covering true/false/
# garbage/absent, both JSON-quoted and bare-string write formats.
_BOOL_CASES = [
    (json.dumps(True), False),
    (json.dumps(False), True),
    ("true", False),
    ("false", True),
    ("1", False),
    ("0", True),
    ("on", False),
    ("off", True),
    ("YES", False),
    ("garbage", True),
    ("garbage", False),
    ('"true"', False),
]


@pytest.mark.asyncio
async def test_bool_seam_matches_old_coercion(tmp_path) -> None:
    from agent_mcp.tools.access import _get_config_bool

    async with mcp_session(tmp_path):
        for i, (stored, default) in enumerate(_BOOL_CASES):
            key = f"config_test_bool_{i}"
            _seed_ctx(key, stored)
            expected = _old_bool_coerce(stored, default)
            # Canonical seam == the reference (old) coercion.
            assert _get_config_bool(key, default) == expected


@pytest.mark.asyncio
async def test_bool_seam_absent_key_returns_default(tmp_path) -> None:
    from agent_mcp.tools.access import _get_config_bool

    async with mcp_session(tmp_path):
        assert _get_config_bool("config_absent_bool", True) is True
        assert _get_config_bool("config_absent_bool", False) is False


@pytest.mark.asyncio
async def test_int_seam_matches_old_retention_read(tmp_path) -> None:
    from agent_mcp.features.message_retention import (
        MAX_RETENTION_DAYS,
        _read_retention_days,
    )
    from agent_mcp.tools.access import _get_config_int

    async with mcp_session(tmp_path):
        key = "config_message_retention_days"

        # positive int -> passthrough
        _seed_ctx(key, json.dumps(5))
        assert _get_config_int(key, 0) == 5
        assert _read_retention_days() == 5

        # bare-string int push
        _seed_ctx(key, "7")
        assert _get_config_int(key, 0) == 7
        assert _read_retention_days() == 7

        # zero -> retention disabled
        _seed_ctx(key, json.dumps(0))
        assert _read_retention_days() == 0

        # negative -> disabled
        _seed_ctx(key, json.dumps(-3))
        assert _get_config_int(key, 0) == -3
        assert _read_retention_days() == 0

        # above the clamp -> clamped to MAX_RETENTION_DAYS
        _seed_ctx(key, json.dumps(MAX_RETENTION_DAYS + 5000))
        assert _read_retention_days() == MAX_RETENTION_DAYS

        # garbage -> default / disabled
        _seed_ctx(key, "not-a-number")
        assert _get_config_int(key, 0) == 0
        assert _read_retention_days() == 0


@pytest.mark.asyncio
async def test_int_seam_absent_key_returns_default(tmp_path) -> None:
    from agent_mcp.tools.access import _get_config_int

    async with mcp_session(tmp_path):
        assert _get_config_int("config_absent_int", 42) == 42
