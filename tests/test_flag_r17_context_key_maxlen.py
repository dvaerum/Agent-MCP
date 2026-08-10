"""FLAG-R17-2 — a maximum-length bound on project-context keys.

``string_utils.MEMORY_KEY_RE`` bounds the *charset* of a memory key
(``A-Z a-z 0-9 . _ / -``) but nothing bounded its *length*: an
authenticated caller could store an arbitrarily long ``context_key`` (an
80 KB key was accepted, limited only by the request body-size cap). This
is storage hygiene, not a security hole — but a fat key still bloats the
row, the index, and every view/health scan.

The fix caps the key length in ``_check_write_authorization`` — the ONE
seam every write path funnels through (single/bulk ``update_project_context``,
``create_project_context``, and, via ``dispatch_tool_call``, the REST
``/api/memories`` create/update routes) — and rejects an over-length key
with the SAME ``Invalid`` (4xx) shape the existing charset/config checks
use, never a 500 and never a stored row.

RED before the cap: an over-length key writes a row and "succeeds".
GREEN after: it is rejected and nothing is written.
"""

from __future__ import annotations

import sqlite3

import pytest

from agent_mcp.tools.project_context_tools import _MAX_CONTEXT_KEY_LEN
from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _row(key: str) -> dict | None:
    """Read a project_context row as a plain dict, or None if absent."""
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT * FROM project_context WHERE context_key = ?", (key,)
        )
        r = cur.fetchone()
    finally:
        conn.close()
    return dict(r) if r else None


# (1) An over-cap key is rejected cleanly (Invalid/4xx), no row written.
async def test_update_over_cap_key_rejected_no_row(tmp_path) -> None:
    over = "a" * (_MAX_CONTEXT_KEY_LEN + 1)
    async with mcp_session(tmp_path) as admin:
        r = await admin.call(
            "update_project_context",
            {"context_key": over, "context_value": "v"},
        )
        msg = r[0].text
        # Clean Invalid, not an Unauthorized-framed denial and not a 500.
        assert "Unauthorized" not in msg, msg
        assert "successfully" not in msg.lower(), msg
        assert "maximum length" in msg.lower(), msg
        assert str(_MAX_CONTEXT_KEY_LEN) in msg, msg
        assert _row(over) is None, "over-length key must not be written"


async def test_create_over_cap_key_rejected_no_row(tmp_path) -> None:
    over = "b" * (_MAX_CONTEXT_KEY_LEN + 1)
    async with mcp_session(tmp_path) as admin:
        r = await admin.call(
            "create_project_context",
            {"context_key": over, "context_value": "v"},
        )
        msg = r[0].text
        assert "Unauthorized" not in msg, msg
        assert "maximum length" in msg.lower(), msg
        assert _row(over) is None, "over-length key must not be created"


# (2) A normal key still works.
async def test_normal_key_still_writes(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = await admin.call(
            "update_project_context",
            {"context_key": "backend-dev/status", "context_value": "ok"},
        )
        assert "successfully" in r[0].text.lower(), r[0].text
        assert _row("backend-dev/status") is not None


# (3) Exactly at the cap works; cap+1 is rejected.
async def test_key_exactly_at_cap_ok(tmp_path) -> None:
    at_cap = "c" * _MAX_CONTEXT_KEY_LEN
    async with mcp_session(tmp_path) as admin:
        r = await admin.call(
            "update_project_context",
            {"context_key": at_cap, "context_value": "v"},
        )
        assert "successfully" in r[0].text.lower(), r[0].text
        assert _row(at_cap) is not None, "a key at the cap must be accepted"


async def test_key_cap_plus_one_rejected(tmp_path) -> None:
    over = "d" * (_MAX_CONTEXT_KEY_LEN + 1)
    async with mcp_session(tmp_path) as admin:
        r = await admin.call(
            "update_project_context",
            {"context_key": over, "context_value": "v"},
        )
        assert "maximum length" in r[0].text.lower(), r[0].text
        assert _row(over) is None


# Edge: the bulk write path rejects atomically before any row lands, so a
# single over-length key in a batch takes the whole batch down.
async def test_bulk_over_cap_key_rejected_atomically(tmp_path) -> None:
    over = "e" * (_MAX_CONTEXT_KEY_LEN + 1)
    async with mcp_session(tmp_path) as admin:
        r = await admin.call(
            "bulk_update_project_context",
            {
                "updates": [
                    {"context_key": "ok-key", "context_value": "v"},
                    {"context_key": over, "context_value": "v"},
                ],
            },
        )
        msg = r[0].text
        assert "maximum length" in msg.lower(), msg
        assert _row(over) is None
        assert _row("ok-key") is None, "atomic reject must not write the sibling"
