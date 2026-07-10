"""BL-R22-1: REST-vs-MCP PARTIAL-UPDATE PARITY on the ``description``
field of a ``project_context`` / memory row.

The REST update handler (``PUT /api/memories/{key}``) applies
partial-update semantics — it only overwrites ``description`` when the
caller EXPLICITLY supplied one
(``agent_mcp/app/routers/memories.py``: ``if description is not None:
row.description = description``). The two MCP write surfaces did NOT:

* ``update_project_context`` (single) overwrote ``description``
  UNCONDITIONALLY with the arg's value, which is ``None`` when the
  caller omits ``description`` — NULLing out the existing description
  on a value-only update.
* ``bulk_update_project_context`` (bulk) is worse: it defaults a
  missing ``description`` to the junk placeholder
  ``"Bulk update operation N"``, so a value-only bulk item overwrote
  the real description with that placeholder.

A value-only update via EITHER MCP path must PRESERVE the existing
row's description (matching REST). An explicit description update via
either path still changes it. A CREATE still stores the
provided/default description.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _read_description(context_key: str):
    """Read a row's ``description`` straight from the DB (source of
    truth), not a session-cached view."""
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import ProjectContext

    with get_session() as session:
        row = (
            session.query(ProjectContext)
            .filter(ProjectContext.context_key == context_key)
            .one_or_none()
        )
        assert row is not None, f"row {context_key!r} missing"
        return row.description


# ── Single path: value-only update PRESERVES description ─────────────


async def test_mcp_single_value_only_update_preserves_description(
    tmp_path: Path,
) -> None:
    """RED before fix: create a row WITH a description, then
    ``update_project_context`` with a NEW value but NO ``description``.
    The existing description must be preserved (on main it is NULLed)."""
    async with mcp_session(tmp_path) as admin:
        await admin.assert_tool_succeeds(
            "update_project_context",
            {
                "context_key": "r22_single",
                "context_value": {"v": 1},
                "description": "original description",
            },
        )
        assert _read_description("r22_single") == "original description"

        # value-only update — no description supplied
        await admin.assert_tool_succeeds(
            "update_project_context",
            {"context_key": "r22_single", "context_value": {"v": 2}},
        )
        assert _read_description("r22_single") == "original description", (
            "value-only MCP single update must PRESERVE the existing "
            "description (REST parity); it was clobbered"
        )


async def test_mcp_single_explicit_description_update_changes_it(
    tmp_path: Path,
) -> None:
    """Regression: an EXPLICIT description on the single path still
    updates the row's description."""
    async with mcp_session(tmp_path) as admin:
        await admin.assert_tool_succeeds(
            "update_project_context",
            {
                "context_key": "r22_single_explicit",
                "context_value": {"v": 1},
                "description": "original",
            },
        )
        await admin.assert_tool_succeeds(
            "update_project_context",
            {
                "context_key": "r22_single_explicit",
                "context_value": {"v": 2},
                "description": "changed",
            },
        )
        assert _read_description("r22_single_explicit") == "changed"


async def test_mcp_single_create_stores_description(
    tmp_path: Path,
) -> None:
    """Regression: a CREATE via the single path still stores the
    provided description (create semantics unchanged)."""
    async with mcp_session(tmp_path) as admin:
        await admin.assert_tool_succeeds(
            "update_project_context",
            {
                "context_key": "r22_single_create",
                "context_value": {"v": 1},
                "description": "fresh description",
            },
        )
        assert _read_description("r22_single_create") == "fresh description"


# ── Bulk path: value-only item PRESERVES description ─────────────────


async def test_mcp_bulk_value_only_update_preserves_description(
    tmp_path: Path,
) -> None:
    """RED before fix: create a row WITH a description, then
    ``bulk_update_project_context`` with a value-only item. The
    existing description must be preserved (on main it is overwritten
    with the ``"Bulk update operation N"`` placeholder)."""
    async with mcp_session(tmp_path) as admin:
        await admin.assert_tool_succeeds(
            "update_project_context",
            {
                "context_key": "r22_bulk",
                "context_value": {"v": 1},
                "description": "original bulk description",
            },
        )
        assert _read_description("r22_bulk") == "original bulk description"

        # value-only bulk item — no description key
        await admin.assert_tool_succeeds(
            "bulk_update_project_context",
            {
                "updates": [
                    {"context_key": "r22_bulk", "context_value": {"v": 2}}
                ]
            },
        )
        got = _read_description("r22_bulk")
        assert got == "original bulk description", (
            "value-only bulk update must PRESERVE the existing "
            f"description (REST parity); got {got!r}"
        )


async def test_mcp_bulk_explicit_description_update_changes_it(
    tmp_path: Path,
) -> None:
    """Regression: an EXPLICIT per-item description on the bulk path
    still updates the row's description."""
    async with mcp_session(tmp_path) as admin:
        await admin.assert_tool_succeeds(
            "update_project_context",
            {
                "context_key": "r22_bulk_explicit",
                "context_value": {"v": 1},
                "description": "original",
            },
        )
        await admin.assert_tool_succeeds(
            "bulk_update_project_context",
            {
                "updates": [
                    {
                        "context_key": "r22_bulk_explicit",
                        "context_value": {"v": 2},
                        "description": "changed via bulk",
                    }
                ]
            },
        )
        assert _read_description("r22_bulk_explicit") == "changed via bulk"


async def test_mcp_bulk_create_stores_default_description(
    tmp_path: Path,
) -> None:
    """Regression: a CREATE via the bulk path still stores the
    default placeholder description when none is provided (create
    semantics unchanged)."""
    async with mcp_session(tmp_path) as admin:
        await admin.assert_tool_succeeds(
            "bulk_update_project_context",
            {
                "updates": [
                    {"context_key": "r22_bulk_create", "context_value": {"v": 1}}
                ]
            },
        )
        assert _read_description("r22_bulk_create") == "Bulk update operation 1"


# ── REST reference: value-only update PRESERVES description ──────────


async def test_rest_value_only_update_preserves_description(
    tmp_path: Path,
) -> None:
    """The REST surface already does the right thing — a value-only
    PUT preserves the existing description. Guards the reference
    behaviour so the parity target can't silently regress."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/api/memories",
            json={
                "token": admin.admin_token,
                "context_key": "r22_rest",
                "context_value": {"v": 1},
                "description": "original rest description",
            },
        )
        assert r.status_code == 200, r.text
        assert _read_description("r22_rest") == "original rest description"

        r = admin.client.request(
            "PUT",
            "/api/memories/r22_rest",
            json={"token": admin.admin_token, "context_value": {"v": 2}},
        )
        assert r.status_code == 200, r.text
        assert _read_description("r22_rest") == "original rest description"
