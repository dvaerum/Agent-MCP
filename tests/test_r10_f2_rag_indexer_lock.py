"""R10-F2: RAG indexer must not starve concurrent writers with a
0-row-DELETE transaction held open across the (slow, network-bound)
embedding-await phase.

Bug: ``run_rag_indexing_periodically`` shares one long-lived sqlite
connection across an entire indexing cycle. The interim "commit the
deletions" step was gated on ``delete_count > 0`` — but a DELETE
statement opens sqlite's write transaction (and grabs its WAL write
lock) the instant it EXECUTES, independent of how many rows it
matched. A cycle indexing only brand-new (never-before-indexed)
sources deletes 0 rows for every source, so the commit was skipped and
the transaction stayed open through the whole embedding phase that
follows — a sequence of ``await``s on a (possibly slow, contended)
embedding endpoint. Every OTHER writer in the process (e.g. a
concurrent ``delete_project_context`` / ``create_task`` request
through ``unit_of_work()``) then blocked for the full 5s
``busy_timeout`` and failed with
``sqlite3.OperationalError: database is locked`` — live-confirmed
against vm-dev's ``verify-scaffold`` project, persisting across a full
backend service restart.

``index_task_data`` (the per-task RAG reindex fired on every task
create/update) had the identical bug: it deleted the task's stale
chunks on the shared cursor with no interim commit before the
(un-awaited, but still blocking) embedding call.

These tests reproduce the lock deterministically — no reliance on
real embedding-network timing — by racing a genuinely separate sqlite
connection/thread against the exact 0-row-DELETE-without-commit
pattern.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# 1. Unit-level: the delete pass must release the write lock immediately,
#    even when every DELETE in the batch matched zero rows.
# ---------------------------------------------------------------------------


async def test_delete_stale_chunks_commits_even_when_nothing_deleted(
    tmp_path,
) -> None:
    """A cycle where every source is brand-new (0 rows deleted for all
    of them) must NOT leave the connection sitting in an open
    transaction — that transaction is exactly what pins the WAL write
    lock across the embedding-await phase in the real caller.
    """
    from agent_mcp.db.connection import get_db_connection
    from agent_mcp.features.rag.indexing import _delete_stale_chunks_and_commit

    async with mcp_session(tmp_path):
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            deleted = _delete_stale_chunks_and_commit(
                cursor,
                conn,
                [
                    ("markdown", "brand-new/never-indexed.md", "content", "hash1"),
                    ("context", "brand-new-context-key", "content", "hash2"),
                ],
            )
            assert deleted == 0, "sanity: nothing existed yet to delete"
            assert conn.in_transaction is False, (
                "a delete pass where every DELETE affected 0 rows must "
                "still commit (releasing the WAL write lock) immediately "
                "— R10-F2 regression: it was left open until the caller's "
                "embedding-await phase finished, starving every other "
                "writer past busy_timeout"
            )
        finally:
            conn.close()


async def test_index_task_data_commits_delete_before_embedding(
    tmp_path, monkeypatch,
) -> None:
    """``index_task_data`` must release the WAL write lock right after
    its (possibly 0-row) chunk delete, BEFORE the embedding call —
    not hold it open across that (potentially slow) network hop.
    """
    from agent_mcp.db.connection import get_db_connection
    from agent_mcp.features.rag import indexing as indexing_mod

    async with mcp_session(tmp_path):
        observed_in_transaction: list[bool] = []

        class _FakeEmbClient:
            # R12-F2: index_task_data now calls the async aembed()
            # (never the sync, event-loop-freezing embed()) — this
            # fake must expose that same method.
            async def aembed(self, texts):
                # Probe the module's own shared connection state from
                # a SEPARATE connection while the (fake, instant, but
                # representative of a slow real one) embedding call is
                # "in flight" — this is where the real bug held the
                # write lock open.
                probe = get_db_connection()
                try:
                    observed_in_transaction.append(probe.in_transaction)
                    probe.execute("BEGIN IMMEDIATE")
                    probe.rollback()
                    observed_in_transaction.append(False)
                except sqlite3.OperationalError:
                    observed_in_transaction.append(True)
                finally:
                    probe.close()
                return [[0.1, 0.2, 0.3] for _ in texts]

        monkeypatch.setattr(
            indexing_mod, "embedding_client", lambda: _FakeEmbClient()
        )
        monkeypatch.setattr(indexing_mod, "is_vss_loadable", lambda: True)

        await indexing_mod.index_task_data(
            "task_probe_1",
            {
                "task_id": "task_probe_1",
                "title": "probe task",
                "description": "d",
                "status": "pending",
                "assigned_to": None,
                "created_by": "admin",
                "parent_task": None,
                "depends_on_tasks": [],
                "priority": "medium",
            },
        )

        assert observed_in_transaction, "fake embed client never ran"
        assert all(not v for v in observed_in_transaction), (
            "a separate connection could not immediately acquire the "
            "write lock while index_task_data's embedding call was in "
            "flight — the delete's transaction was still open "
            f"(observed: {observed_in_transaction})"
        )


# ---------------------------------------------------------------------------
# 2. Genuine concurrent-writer race: a second, independent connection/thread
#    must succeed promptly against the fixed indexer connection.
# ---------------------------------------------------------------------------


async def test_concurrent_project_context_write_not_starved_by_indexer(
    tmp_path,
) -> None:
    """Reproduces the live R10-F2 symptom end to end with two REAL,
    independent sqlite connections racing on the same DB file — no
    reliance on real network/embedding timing.

    Connection A runs the exact indexer delete pass (0 rows matched,
    since nothing has been indexed yet). While A's transaction state
    is whatever the fix leaves it in, connection B — a short
    busy_timeout probe standing in for a concurrent
    ``delete_project_context`` / ``create_task`` request — must be
    able to write immediately. Before the fix this would hang for the
    full busy_timeout and then raise ``database is locked``; the probe
    uses a short timeout so the red run fails fast instead of taking
    5 real seconds.
    """
    from agent_mcp.core.config import get_db_path
    from agent_mcp.db.connection import get_db_connection
    from agent_mcp.features.rag.indexing import _delete_stale_chunks_and_commit

    async with mcp_session(tmp_path):
        db_path = str(get_db_path())

        conn_a = get_db_connection()
        cursor_a = conn_a.cursor()
        _delete_stale_chunks_and_commit(
            cursor_a,
            conn_a,
            [("markdown", "another-brand-new-file.md", "content", "hashX")],
        )

        result: dict = {}

        def _concurrent_writer() -> None:
            conn_b = sqlite3.connect(db_path, timeout=0)
            try:
                conn_b.execute("PRAGMA busy_timeout=300;")
                conn_b.execute(
                    "INSERT INTO project_context "
                    "(context_key, value, created_at, created_by, "
                    "updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        "r10_f2_concurrent_probe",
                        "1",
                        "2026-01-01T00:00:00",
                        "test",
                        "2026-01-01T00:00:00",
                        "test",
                    ),
                )
                conn_b.commit()
                result["ok"] = True
            except sqlite3.OperationalError as e:
                result["error"] = str(e)
            finally:
                conn_b.close()

        t = threading.Thread(target=_concurrent_writer)
        t.start()
        t.join(timeout=5)

        conn_a.close()

        assert not t.is_alive(), "concurrent writer thread never finished"
        assert result.get("ok") is True, (
            "concurrent project_context write was blocked by the "
            f"indexer's connection: {result}"
        )
