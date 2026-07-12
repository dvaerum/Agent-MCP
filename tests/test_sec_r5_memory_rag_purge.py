"""Round-5 security findings — BL-R5-1 (REST memory-delete RAG orphan) +
BL-R5-2 (raw SQLAlchemy error reflected in memory 500 bodies).

BL-R5-1 — the REST ``DELETE /api/memories/<key>`` handler
(``delete_memory_api_route``) deletes the ``project_context`` row via a
direct ``session.delete(row)`` but — unlike the round-4 fix on the MCP
``delete_project_context`` tool — never pruned the matching
``source_type='context'`` RAG chunk + its ``hash_context_<key>``
``rag_meta`` watermark. The incremental indexer keys on ``updated_at``
and never sweeps orphans, so the deleted memory stayed queryable via
``ask_project_rag`` forever. The dashboard's primary delete path is
REST, so this was the live gap. These tests assert against the DB/repo
directly (a real RAG query needs live embeddings, which the harness
mocks to zero vectors).

BL-R5-2 — the three memory handlers (create/update/delete) returned
``f"...: {str(e)}"`` where ``e`` is a ``SQLAlchemyError``; ``str(e)``
embeds the SQL text and bound parameters (schema disclosure). The fix
keeps the server-side ``logger.error(..., exc_info=True)`` and returns
a generic client message with no exception text.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy.exc import SQLAlchemyError

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Direct-SQL seed / inspect helpers (harness convention: INSERT a row,
# don't drive a public-API path we're not testing).
# --------------------------------------------------------------------------


def _seed_context_row(key: str, value: str) -> None:
    from agent_mcp.db.connection import get_db_connection

    now = datetime.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO project_context (context_key, value, description, "
            "created_at, created_by, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key, value, "d", now, "admin", now, "admin"),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_rag_chunk(source_type: str, source_ref: str, text: str) -> None:
    """Insert a rag_chunks row + its hash_<type>_<ref> rag_meta watermark.

    Skips rag_embeddings on purpose — the vec0 table may be absent on a
    host without sqlite-vec, and the purge path guards that separately.
    """
    from agent_mcp.db.connection import get_db_connection

    now = datetime.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO rag_chunks (source_type, source_ref, chunk_text, "
            "indexed_at, metadata) VALUES (?, ?, ?, ?, ?)",
            (source_type, source_ref, text, now, None),
        )
        cur.execute(
            "INSERT OR REPLACE INTO rag_meta (meta_key, meta_value) "
            "VALUES (?, ?)",
            (f"hash_{source_type}_{source_ref}", "deadbeef"),
        )
        conn.commit()
    finally:
        conn.close()


def _rag_state(source_type: str, source_ref: str):
    """Return (chunk_count, hash_meta_present) for a source ref."""
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM rag_chunks "
            "WHERE source_type = ? AND source_ref = ?",
            (source_type, source_ref),
        )
        chunk_count = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM rag_meta WHERE meta_key = ?",
            (f"hash_{source_type}_{source_ref}",),
        )
        hash_present = cur.fetchone()[0] > 0
        return chunk_count, hash_present
    finally:
        conn.close()


# ==========================================================================
# BL-R5-1 — REST delete prunes RAG chunk + hash watermark
# ==========================================================================


async def test_rest_delete_memory_purges_rag_chunk_and_hash(tmp_path):
    """DELETE /api/memories/<key> removes the memory's RAG chunk and
    clears the hash_context_<key> watermark, in the same delete.

    RED against origin/main: the REST handler deleted only the
    project_context row, so the chunk + hash survived.
    """
    async with mcp_session(tmp_path) as admin:
        _seed_context_row("deploy_notes", "some indexed content")
        _seed_rag_chunk("context", "deploy_notes", "some indexed content")

        assert _rag_state("context", "deploy_notes") == (1, True)

        r = admin.client.request(
            "DELETE",
            "/api/memories/deploy_notes",
            json={"token": admin.admin_token},
        )
        assert r.status_code == 200, r.text

        # Row gone AND chunk gone AND hash cleared (so a re-add re-indexes).
        assert _rag_state("context", "deploy_notes") == (0, False)


async def test_rest_delete_memory_leaves_other_chunks(tmp_path):
    """The purge is scoped to the deleted key — a sibling memory's chunk
    and hash survive."""
    async with mcp_session(tmp_path) as admin:
        _seed_context_row("target_key", "content a")
        _seed_context_row("keep_key", "content b")
        _seed_rag_chunk("context", "target_key", "content a")
        _seed_rag_chunk("context", "keep_key", "content b")

        r = admin.client.request(
            "DELETE",
            "/api/memories/target_key",
            json={"token": admin.admin_token},
        )
        assert r.status_code == 200, r.text

        assert _rag_state("context", "target_key") == (0, False)
        assert _rag_state("context", "keep_key") == (1, True)


# ==========================================================================
# BL-R5-2 — memory handlers must not reflect raw SQLAlchemy errors
# ==========================================================================


# A SQLAlchemyError whose str() looks like the confirmed-live leak:
# raw SQL text + bound parameters.
_LEAKY_MESSAGE = (
    "(sqlite3.OperationalError) database is locked "
    "[SQL: DELETE FROM project_context WHERE context_key = ?] "
    "[parameters: ('secret_key',)]"
)


def _assert_generic_no_leak(body: dict, generic: str) -> None:
    err = body.get("error", "")
    assert err == generic, body
    # No SQL text, no bound params, no raw exception str.
    assert "SQL:" not in err
    assert "parameters:" not in err
    assert "project_context" not in err
    assert "secret_key" not in err
    assert "database is locked" not in err


async def test_delete_memory_db_error_returns_generic_message(
    tmp_path, monkeypatch
):
    """A SQLAlchemyError in the delete path yields a 500 whose body is
    a generic message — never the SQL/params/str(e).

    R9-F2: the DELETE handler is now a thin adapter over the
    ``delete_project_context`` tool (routed there so the tool-layer
    authorization gates apply on the REST surface too). Patch the repo's
    delete entry point so it raises the same leaky-looking error a real
    DB fault would; the tool returns ``Failed`` and the adapter maps that
    to a STATIC generic 500 body (no SQL/params/table-name leak).

    RED against origin/main: body was
    ``"Failed to delete memory: (sqlite3...) [SQL: ...] [parameters: ...]"``.
    """
    async with mcp_session(tmp_path) as admin:
        import agent_mcp.tools.project_context_tools as pctx_mod

        def _boom_delete_many(*args, **kwargs):
            raise SQLAlchemyError(_LEAKY_MESSAGE)

        monkeypatch.setattr(
            pctx_mod.project_context_repo, "delete_many", _boom_delete_many
        )

        r = admin.client.request(
            "DELETE",
            "/api/memories/some_key",
            json={"token": admin.admin_token},
        )
        assert r.status_code == 500, r.text
        _assert_generic_no_leak(r.json(), "Failed to delete memory")


async def test_create_memory_db_error_returns_generic_message(
    tmp_path, monkeypatch
):
    async with mcp_session(tmp_path) as admin:
        # E3: create_memory is now a thin adapter over the
        # ``create_project_context`` tool. arch-r4 #6 moved that tool's
        # write off a raw ``SessionLocal()`` ORM session onto
        # ``unit_of_work()`` + ``project_context_repo`` (parameterized
        # SQL on the uow's cursor) — patch the repo's INSERT entry point
        # so it raises the same leaky-looking error a real DB fault
        # would. The tool returns ``Failed`` on the exception; the
        # adapter maps that to a STATIC generic 500 body (no
        # SQL/params/table-name leak).
        import agent_mcp.tools.project_context_tools as pctx_mod

        def _boom_create_new(*args, **kwargs):
            raise SQLAlchemyError(_LEAKY_MESSAGE)

        monkeypatch.setattr(
            pctx_mod.project_context_repo, "create_new", _boom_create_new
        )

        r = admin.client.post(
            "/api/memories",
            json={"token": admin.admin_token, "context_key": "k",
                  "context_value": {"a": 1}},
        )
        assert r.status_code == 500, r.text
        _assert_generic_no_leak(r.json(), "Failed to create memory")


async def test_update_memory_db_error_returns_generic_message(
    tmp_path, monkeypatch
):
    """R9-F2: the PUT handler is now a thin adapter over the
    ``update_project_context`` tool. Patch the repo's upsert entry point
    so it raises; the tool returns ``Failed`` and the adapter maps that to
    a STATIC generic 500 body (no SQL/params/table-name leak)."""
    async with mcp_session(tmp_path) as admin:
        import agent_mcp.tools.project_context_tools as pctx_mod

        def _boom_upsert(*args, **kwargs):
            raise SQLAlchemyError(_LEAKY_MESSAGE)

        monkeypatch.setattr(
            pctx_mod.project_context_repo, "upsert", _boom_upsert
        )

        r = admin.client.request(
            "PUT",
            "/api/memories/k",
            json={"token": admin.admin_token, "context_value": {"a": 1}},
        )
        assert r.status_code == 500, r.text
        _assert_generic_no_leak(r.json(), "Failed to update memory")
