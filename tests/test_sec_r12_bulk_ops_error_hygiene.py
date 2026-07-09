"""SD-R12-1 — ``bulk_task_operations`` per-op error hygiene.

The per-operation ``except`` branch in ``bulk_task_operations``
appends its failure line to ``results``, which is returned as
``Ok(message=...)``. The renderer genericises only the ``Failed``
variant (SD-R8-1 choke-point); an ``Ok`` body is emitted VERBATIM
over the MCP wire. So interpolating ``str(e)`` into that line leaks
raw SQLite table/column names, OSError filesystem paths, and other
internal state to any worker holding ``tasks.update``.

This is the same class as SD-R9-1 (the RAG ``Ok``-body bypass);
that round's sweep fixed the RAG instance but missed this one.

Fix: keep the exception detail server-side (``logger`` with
``exc_info``) and put a STATIC, non-revealing line into ``results``.
The test drives the tool into the per-op exception branch by making
the repository write raise a ``sqlite3`` error carrying a distinctive
secret token, then asserts the client-facing wire text does NOT
contain that token.
"""

from __future__ import annotations

import datetime as _dt
import secrets
import sqlite3

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


# A distinctive, secret-shaped exception payload: a fake SQLite
# "no such column" message naming an internal column. If any part of
# this reaches the wire, the leak is present.
_SECRET_EXC_TEXT = "no such column: tasks.internal_secret_column_xyz"


def _seed_assigned_task(title: str, assigned_to: str) -> str:
    """Insert an assigned task row + mirror it into the in-memory
    cache. Mirrors ``test_wave6_pr4_task_tools_e2e._seed_assigned_task``.
    """
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    task_id = f"task_{secrets.token_hex(6)}"
    now = _dt.datetime.now().isoformat()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tasks (task_id, title, description, status, priority, "
        "assigned_to, created_by, created_at, updated_at, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            title,
            "seed description",
            "pending",
            "medium",
            assigned_to,
            "admin",
            now,
            now,
            "[]",
        ),
    )
    conn.commit()
    conn.close()

    g.tasks[task_id] = {
        "task_id": task_id,
        "title": title,
        "status": "pending",
        "priority": "medium",
        "assigned_to": assigned_to,
        "created_by": "admin",
        "created_at": now,
        "updated_at": now,
        "notes": [],
    }
    return task_id


async def test_bulk_ops_per_op_exception_does_not_leak_raw_exception(
    tmp_path, monkeypatch,
) -> None:
    """Driving a bulk op into its per-op exception branch must NOT
    surface the raw exception text on the wire.

    RED (origin/main): the branch appends
    ``f"... Error processing - {str(e)}"`` into ``results``, which is
    returned as ``Ok(message=...)`` and rendered verbatim — so the
    SQLite column name leaks. GREEN: a static line is emitted and the
    detail is confined to the server log.
    """
    from agent_mcp.repositories.task_repository import TaskRepository

    async with mcp_session(tmp_path) as admin:
        task_id = _seed_assigned_task("bulk secret leak", "admin")

        # Force the per-op write to raise a secret-shaped SQLite error.
        # The update_status branch calls ``task_repo.update_fields`` on
        # the ``TaskRepository`` singleton; patch the class method so any
        # instance the loop resolves raises (the singleton may be swapped
        # by the harness, so patching the class is the robust target).
        def _boom(self, *_args, **_kwargs):
            raise sqlite3.OperationalError(_SECRET_EXC_TEXT)

        monkeypatch.setattr(TaskRepository, "update_fields", _boom)

        result = await admin.call(
            "bulk_task_operations",
            {
                "operations": [
                    {
                        "type": "update_status",
                        "task_id": task_id,
                        "status": "in_progress",
                    },
                ],
            },
        )

        text = result[0].text

        # The raw exception text must not reach the client in any form.
        assert _SECRET_EXC_TEXT not in text, text
        assert "internal_secret_column_xyz" not in text, text
        assert "no such column" not in text, text

        # A static, non-revealing failure line must be present so the
        # caller still learns the op failed.
        assert "internal error" in text.lower(), text
