"""Round-4 security findings — PF-1 (task-ownership oracle) + BL-R4-1
(RAG orphan on delete).

PF-1 — intra-project task-ownership differential-response oracle.
A non-owner caller lacking ``tasks.assign`` must not be able to tell
"task exists but isn't yours" (a 403 shape) from "task doesn't exist"
(a 404 shape), and must never see the owning agent's id. The
affected surfaces are ``update_task_status`` (via
``_update_single_task``), ``bulk_task_operations``, and the three
``task_note`` tools. Every one must return the SAME not-found result
for a foreign existing task as for a nonexistent one. An owner (or a
``tasks.assign`` caller) keeps normal behaviour.

BL-R4-1 — delete orphans the RAG vector chunk. ``delete_task`` and
``delete_project_context`` delete the row but the incremental indexer
(keyed on ``updated_at``, no orphan sweep) never removes the matching
``rag_chunks`` row + its ``hash_<type>_<ref>`` ``rag_meta`` watermark,
so deleted content stays queryable via ``ask_project_rag``. The fix
prunes both in the delete transaction; these tests assert against the
DB/repo directly (RAG query needs live embeddings, which the harness
mocks to zero vectors).
"""

from __future__ import annotations

import datetime

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Direct-SQL seed helpers (harness convention: INSERT a row, don't drive
# the public-API path we're not testing).
# --------------------------------------------------------------------------


def _seed_task_row(task_id: str, *, assigned_to: str, parent_task=None) -> None:
    from agent_mcp.db.connection import get_db_connection

    now = datetime.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tasks (task_id, title, description, status, "
            "priority, created_at, updated_at, created_by, assigned_to, "
            "parent_task) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, "t", "d", "pending", "medium", now, now, "admin",
             assigned_to, parent_task),
        )
        conn.commit()
    finally:
        conn.close()


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


def _text(blocks) -> str:
    return blocks[0].text if blocks else ""


# ==========================================================================
# PF-1 — task-ownership existence oracle
# ==========================================================================


async def test_update_task_status_foreign_task_indistinguishable(tmp_path):
    """A non-owner worker updating an EXISTING foreign task gets the
    SAME response as updating a nonexistent task — no owner name, no
    403-vs-404 differential."""
    async with mcp_session(tmp_path) as admin:
        _seed_task_row("foreign-task-a", assigned_to="alice")
        intruder = await admin.create_worker("bob")

        foreign = _text(await intruder.call(
            "update_task_status",
            {"task_id": "foreign-task-a", "status": "completed"},
        ))
        missing = _text(await intruder.call(
            "update_task_status",
            {"task_id": "no-such-task", "status": "completed"},
        ))

        # Byte-identical existence signal (only the id differs).
        assert "not found" in foreign.lower(), foreign
        assert foreign.replace("foreign-task-a", "X") == \
            missing.replace("no-such-task", "X"), (
            f"foreign vs missing differ: {foreign!r} vs {missing!r}"
        )
        # Never leak the owning agent's id or an authz distinction.
        assert "alice" not in foreign, foreign
        assert "unauthorized" not in foreign.lower(), foreign


async def test_update_task_status_owner_still_succeeds(tmp_path):
    """The owner keeps normal behaviour — the oracle fix must not break
    a legitimate self-update."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        _seed_task_row("alice-task", assigned_to="alice")

        result = _text(await alice.call(
            "update_task_status",
            {"task_id": "alice-task", "status": "in_progress"},
        ))
        assert "not found" not in result.lower(), result
        assert "unauthorized" not in result.lower(), result


async def test_bulk_task_operations_foreign_task_not_found(tmp_path):
    """bulk_task_operations must report a foreign existing task as
    not-found, not 'Unauthorized - can only modify own tasks'."""
    async with mcp_session(tmp_path) as admin:
        _seed_task_row("foreign-task-b", assigned_to="alice")
        intruder = await admin.create_worker("bob")

        foreign = _text(await intruder.call(
            "bulk_task_operations",
            {"operations": [
                {"type": "update_status", "task_id": "foreign-task-b",
                 "status": "completed"},
            ]},
        ))
        missing = _text(await intruder.call(
            "bulk_task_operations",
            {"operations": [
                {"type": "update_status", "task_id": "ghost-task",
                 "status": "completed"},
            ]},
        ))

        assert "not found" in foreign.lower(), foreign
        assert "unauthorized" not in foreign.lower(), foreign
        assert "can only modify" not in foreign.lower(), foreign
        assert "alice" not in foreign, foreign
        # Same not-found wording as a genuinely missing task.
        assert foreign.replace("foreign-task-b", "X") == \
            missing.replace("ghost-task", "X"), (
            f"foreign vs missing differ: {foreign!r} vs {missing!r}"
        )


async def test_add_task_comment_foreign_task_indistinguishable(tmp_path):
    """With config_allow_worker_comment_foreign_tasks disabled,
    add_task_comment on a foreign existing task must match the response
    for a nonexistent task (no owner name, no 403-vs-404 oracle). The
    toggle defaults True (a worker CAN comment on a foreign task by
    default, tests/test_sec_comment_ownership_rag_gate.py) — this test
    is specifically about what happens when a project opts back into
    the stricter policy."""
    async with mcp_session(tmp_path) as admin:
        admin.set_toggle("config_allow_worker_comment_foreign_tasks", "false")
        _seed_task_row("foreign-task-c", assigned_to="alice")
        intruder = await admin.create_worker("bob")

        foreign = _text(await intruder.call(
            "add_task_comment",
            {"task_id": "foreign-task-c", "text": "hi"},
        ))
        missing = _text(await intruder.call(
            "add_task_comment",
            {"task_id": "phantom-task", "text": "hi"},
        ))

        assert "not found" in foreign.lower(), foreign
        assert "not assigned to or created by" not in foreign.lower(), foreign
        assert "alice" not in foreign, foreign
        assert foreign.replace("foreign-task-c", "X") == \
            missing.replace("phantom-task", "X"), (
            f"foreign vs missing differ: {foreign!r} vs {missing!r}"
        )


async def test_add_task_comment_owner_still_succeeds(tmp_path):
    """The task owner may still add a comment — the fix only collapses
    the non-owner branch."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        _seed_task_row("alice-task-note", assigned_to="alice")

        result = _text(await alice.call(
            "add_task_comment",
            {"task_id": "alice-task-note", "text": "my note"},
        ))
        assert "not found" not in result.lower(), result
        assert "added" in result.lower(), result


# ==========================================================================
# BL-R4-1 — prune RAG chunk + hash watermark on delete
# ==========================================================================


async def test_delete_context_purges_rag_chunk_and_hash(tmp_path):
    """Deleting a project_context key removes its RAG chunk and clears
    the hash_context_<key> watermark, in the same delete."""
    async with mcp_session(tmp_path) as admin:
        _seed_context_row("deploy_notes", "some indexed content")
        _seed_rag_chunk("context", "deploy_notes", "some indexed content")

        assert _rag_state("context", "deploy_notes") == (1, True)

        result = _text(await admin.call(
            "delete_project_context",
            {"context_key": "deploy_notes"},
        ))
        assert "deleted" in result.lower(), result

        # Chunk gone AND hash cleared (so a re-add re-indexes).
        assert _rag_state("context", "deploy_notes") == (0, False)


async def test_delete_task_purges_rag_chunk_and_hash(tmp_path):
    """Deleting a task removes its RAG chunk + hash_task_<id>."""
    async with mcp_session(tmp_path) as admin:
        _seed_task_row("rag-task-1", assigned_to="alice")
        _seed_rag_chunk("task", "rag-task-1", "task content")

        assert _rag_state("task", "rag-task-1") == (1, True)

        result = _text(await admin.call(
            "delete_task",
            {"task_id": "rag-task-1", "force_delete": True},
        ))
        assert "deleted" in result.lower(), result

        assert _rag_state("task", "rag-task-1") == (0, False)


async def test_delete_task_cascade_purges_descendant_chunks(tmp_path):
    """Force-cascade delete must prune the RAG chunks of descendants
    too — not just the target task."""
    async with mcp_session(tmp_path) as admin:
        _seed_task_row("rag-parent", assigned_to="alice")
        _seed_task_row("rag-child", assigned_to="alice",
                       parent_task="rag-parent")
        _seed_rag_chunk("task", "rag-parent", "parent content")
        _seed_rag_chunk("task", "rag-child", "child content")

        assert _rag_state("task", "rag-parent") == (1, True)
        assert _rag_state("task", "rag-child") == (1, True)

        result = _text(await admin.call(
            "delete_task",
            {"task_id": "rag-parent", "force_delete": True},
        ))
        assert "deleted" in result.lower(), result

        assert _rag_state("task", "rag-parent") == (0, False)
        assert _rag_state("task", "rag-child") == (0, False)
