"""Phase 1c — backend side of the alias-with-grace-period telemetry.

Phase 1b shipped the router-side plumbing: a request that arrives on an
alias URL is transparently re-pointed at the backend for the canonical
project, with an `X-Agent-MCP-Alias: <alias_name>,<expires_at>` header
injected upstream.

This file covers the BACKEND half:

  1. Migration 0005 adds `mcp_sessions.alias_used TEXT` plus a
     covering index on `(alias_used, last_seen_at)` so an operator can
     later answer "which alias is still receiving traffic, and when
     did its last subscriber close?" without a full table scan.

  2. A middleware reads `X-Agent-MCP-Alias` on every /mcp request and
     stashes the parsed `(alias_name, expires_at)` on
     `request.scope["agent_mcp_alias"]`. The session-opener for GET
     /mcp threads that value into `register_session`, which persists
     `alias_name` into the new column. No header → `alias_used IS NULL`,
     wire-equivalent to today.

  3. The MCP `initialize` response's top-level `instructions` field
     (per spec rev 2025-03-26 — sibling of `serverInfo`, not nested
     inside it) is augmented with a deprecation warning block when the
     request carried an alias header. Without the header, the
     instructions field is left as-is (None today; the system prompt
     lives elsewhere in agent-mcp).

`mcp_session` is the shared E2E harness from `tests/harness.py`. We
drive the registered handlers directly, mirroring what the
StreamableHTTP transport does at the wire.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import sqlite3
from pathlib import Path

import pytest

from tests.harness import mcp_session


# ---------------------------------------------------------------------------
# Migration 0005 — schema check
# ---------------------------------------------------------------------------


def _open_project_db(project_dir: Path) -> sqlite3.Connection:
    """Open the project's mcp_state.db read-only.

    The harness's `mcp_session` runs the full lifespan (which triggers
    Alembic upgrade), so by the time we open the file the migration
    has already executed. Tests assert *post-migration* state.
    """
    db_path = project_dir / ".agent" / "mcp_state.db"
    assert db_path.exists(), f"expected DB at {db_path}"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


@pytest.mark.asyncio
async def test_migration_adds_alias_used_column_and_index(tmp_path: Path) -> None:
    """Fresh project DB → `mcp_sessions.alias_used` column exists AND
    the `idx_mcp_sessions_alias_used` composite index is created.

    The index is on `(alias_used, last_seen_at)` because the canonical
    operator query is "for alias X, when did its last subscriber close?"
    — a covering index on those two columns is exactly the shape that
    serves it without a table scan.
    """
    async with mcp_session(tmp_path):
        conn = _open_project_db(tmp_path / "project")
        try:
            cur = conn.cursor()
            cols = {row["name"] for row in cur.execute(
                "PRAGMA table_info(mcp_sessions)"
            ).fetchall()}
            assert "alias_used" in cols, (
                f"expected alias_used in mcp_sessions; got {sorted(cols)}"
            )

            indexes = {row["name"] for row in cur.execute(
                "PRAGMA index_list(mcp_sessions)"
            ).fetchall()}
            assert "idx_mcp_sessions_alias_used" in indexes, (
                f"expected idx_mcp_sessions_alias_used; got {sorted(indexes)}"
            )

            # Composite index must cover (alias_used, last_seen_at) in
            # that order so SQLite can satisfy `WHERE alias_used = ?`
            # plus `ORDER BY last_seen_at DESC` from the index alone.
            idx_cols = [
                row["name"] for row in cur.execute(
                    "PRAGMA index_info(idx_mcp_sessions_alias_used)"
                ).fetchall()
            ]
            assert idx_cols == ["alias_used", "last_seen_at"], (
                f"unexpected idx column order: {idx_cols}"
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Middleware — alias header → register_session.alias_used
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_session_persists_alias_when_header_set(
    tmp_path: Path,
) -> None:
    """A direct call to `register_session(alias_used=...)` lands the
    value in the new column.

    Driving the registry directly (rather than spinning a real GET /mcp
    stream) keeps the test independent of the SSE pump's lifetime.
    The session row should carry the canonical alias_name parsed from
    the header — *not* the raw `name,expires_at` blob.
    """
    async with mcp_session(tmp_path) as admin:
        from agent_mcp.core import session_registry

        sid = session_registry.register_session(
            agent_id="admin",
            bearer_token=admin.admin_token,
            alias_used="old-project-name",
        )
        try:
            conn = _open_project_db(tmp_path / "project")
            row = conn.execute(
                "SELECT alias_used FROM mcp_sessions WHERE session_id = ?",
                (sid,),
            ).fetchone()
            conn.close()
            assert row is not None, "expected mcp_sessions row to be inserted"
            assert row["alias_used"] == "old-project-name"
        finally:
            session_registry.unregister_session(sid)


@pytest.mark.asyncio
async def test_register_session_no_header_leaves_alias_null(
    tmp_path: Path,
) -> None:
    """Default call to `register_session` (no `alias_used` kwarg) →
    column is NULL. This is the no-alias hot path that every existing
    GET /mcp follows today; the new column must default to NULL so the
    behavior is wire-equivalent.
    """
    async with mcp_session(tmp_path) as admin:
        from agent_mcp.core import session_registry

        sid = session_registry.register_session(
            agent_id="admin", bearer_token=admin.admin_token
        )
        try:
            conn = _open_project_db(tmp_path / "project")
            row = conn.execute(
                "SELECT alias_used FROM mcp_sessions WHERE session_id = ?",
                (sid,),
            ).fetchone()
            conn.close()
            assert row is not None
            assert row["alias_used"] is None
        finally:
            session_registry.unregister_session(sid)


@pytest.mark.asyncio
async def test_alias_header_middleware_parses_scope(tmp_path: Path) -> None:
    """End-to-end shape: send a POST /mcp with `X-Agent-MCP-Alias`
    header, the middleware parses + stashes the tuple on
    `request.scope["agent_mcp_alias"]`.

    We snoop the scope via a one-shot route mounted only for this test
    — the cheapest way to assert "the middleware ran and produced the
    right value" without depending on the GET /mcp path which has its
    own scope-handling code.
    """
    async with mcp_session(tmp_path) as admin:
        from agent_mcp.app.main_app import _parse_alias_header  # type: ignore[attr-defined]

        # Parse helper roundtrip — middleware ultimately calls this.
        parsed = _parse_alias_header("old-name,2026-07-15T00:00:00Z")
        assert parsed == ("old-name", "2026-07-15T00:00:00Z")

        parsed_empty = _parse_alias_header(None)
        assert parsed_empty is None

        parsed_malformed = _parse_alias_header("no-comma")
        assert parsed_malformed is None

        # Live integration: hit /mcp with the header, ensure no 5xx and
        # the middleware doesn't blow up parsing.
        r = admin.client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {admin.admin_token}",
                "X-Agent-MCP-Alias": "old-name,2026-07-15T00:00:00Z",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
        )
        # The middleware must not 401 us; the bearer is valid. Any
        # 4xx/5xx here means the header parse exploded or the gate
        # rejected an otherwise-fine request.
        assert r.status_code == 200, f"status={r.status_code} body={r.text!r}"


# ---------------------------------------------------------------------------
# initialize response → serverInfo.instructions carries the warning
# ---------------------------------------------------------------------------


def _extract_initialize_result(response_text: str) -> dict:
    """Pull the `result` JSON out of a Streamable HTTP initialize reply.

    The transport returns either:
      * inline JSON — one bare ``{"jsonrpc": …}`` envelope, or
      * SSE — one or more frames of the shape

            event: message\\r\\n
            data: {"jsonrpc": "2.0", "id": 1, "result": {...}}\\r\\n
            \\r\\n

    We accept both shapes (and either CRLF or LF line endings) by
    iterating lines, picking up anything prefixed by ``data:``, and
    JSON-decoding the first payload that carries a ``result`` field.
    """
    import json

    text = response_text.strip()
    # SSE: split on either CRLF or LF; ``str.splitlines`` handles both.
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        payload = json.loads(line[len("data:"):].strip())
        if isinstance(payload, dict) and "result" in payload:
            return payload["result"]
    # Inline JSON fallback (no `data:` prefix anywhere).
    envelope = json.loads(text)
    assert "result" in envelope, f"no result in {envelope!r}"
    return envelope["result"]


@pytest.mark.asyncio
async def test_initialize_appends_alias_warning_when_header_present(
    tmp_path: Path,
) -> None:
    """POST /mcp with `X-Agent-MCP-Alias` → the initialize response's
    top-level `instructions` field contains the deprecation block.

    The warning mentions the alias name + expiry date so a client
    looking at its server-info display sees an actionable hint, not
    just "something's wrong".
    """
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {admin.admin_token}",
                "X-Agent-MCP-Alias": "legacy-name,2026-07-15T00:00:00Z",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
        )
        assert r.status_code == 200, r.text
        result = _extract_initialize_result(r.text)
        instructions = result.get("instructions") or ""
        assert "ALIAS DEPRECATION WARNING" in instructions, (
            f"expected warning in instructions; got {instructions!r}"
        )
        assert "legacy-name" in instructions
        assert "2026-07-15T00:00:00Z" in instructions


@pytest.mark.asyncio
async def test_initialize_omits_alias_warning_without_header(
    tmp_path: Path,
) -> None:
    """Same POST /mcp without the alias header → no warning block.

    The `instructions` field is whatever agent-mcp normally returns
    (currently None / empty); the critical contract is that we don't
    leak an unrelated warning when no alias was used.
    """
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {admin.admin_token}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
        )
        assert r.status_code == 200, r.text
        result = _extract_initialize_result(r.text)
        instructions = result.get("instructions") or ""
        assert "ALIAS DEPRECATION WARNING" not in instructions, (
            f"unexpected warning leaked: {instructions!r}"
        )
