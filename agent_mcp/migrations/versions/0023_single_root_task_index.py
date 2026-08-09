"""single-root-task partial UNIQUE index (structural backstop)

Revision ID: 0023_single_root_task_index
Revises: 0022_pending_directive_table
Create Date: 2026-08-09

R15-BL-1: every project DB (one ``mcp_state.db`` per project) must have
AT MOST ONE root task — a task with ``parent_task IS NULL``. The
validator documents this as a "hard structural constraint" and the rest
of the code relies on it, but nothing enforced it below the (previously
inconsistent) application-code checks. This migration installs the
backstop no app path can bypass::

    CREATE UNIQUE INDEX idx_tasks_single_root
        ON tasks ((parent_task IS NULL)) WHERE parent_task IS NULL

Why the constant expression: SQLite treats each NULL value in a UNIQUE
index as DISTINCT, so a plain ``UNIQUE(parent_task) WHERE
parent_task IS NULL`` would let unlimited roots coexist. Indexing the
expression ``(parent_task IS NULL)`` — which is the constant ``1`` for
every root row — makes all roots collide on a single key, while the
``WHERE`` predicate keeps child rows out of the index entirely.

Uniqueness scope: one root per DB. Projects map one-to-one onto a
SQLite ``mcp_state.db``, so a table-wide unique index over the root
rows is exactly "one root per project" — no project-id column exists
(or is needed) to scope on.

## Pre-existing-violation strategy (chosen: (a) repair, in-migration)

This very bug already allowed multi-root DBs to exist (the pentest
created three roots), so a naive ``CREATE UNIQUE INDEX`` would hard-fail
on real data. We REPAIR before creating the index rather than shipping a
migration that crashes on production DBs:

  * Keep the OLDEST root (``created_at`` ASC, ``task_id`` ASC as a
    deterministic tie-break) — the legitimate "first" root the invariant
    is meant to protect.
  * Re-parent every OTHER root under it (set ``parent_task`` to the
    survivor and append to the survivor's ``child_tasks`` JSON mirror,
    matching ``task_tools._link_child_to_parent``).

Re-parenting is a well-defined, loss-free repair: no task is deleted,
and the result satisfies the app's own rule that every non-first task
has a parent. Idempotent — a re-run on an already-repaired (single-root)
DB is a no-op and the ``CREATE UNIQUE INDEX IF NOT EXISTS`` skips.

## create_all / migration ordering

``init_database()`` runs ``Base.metadata.create_all()`` before the
Alembic chain. ``create_all`` only emits an index when it CREATES the
owning table, so on a FRESH DB the ORM model
(``agent_mcp.db.models.task.Task``) already declares this index and this
migration no-ops (the index-exists gate). On a LEGACY DB the ``tasks``
table already exists, so ``create_all`` does NOT touch the index — the
repair-then-create here is the path that lands it, and it never runs
against un-repaired data via ``create_all``.

## Downgrade

Drops the index. The re-parenting repair is intentionally NOT reverted
(forward-only; the pre-repair multi-root state is invalid by design).
"""

from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0023_single_root_task_index"
down_revision: Union[str, None] = "0022_pending_directive_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_INDEX = "idx_tasks_single_root"


def _table_exists(bind, name: str) -> bool:
    inspector = sa.inspect(bind)
    return name in inspector.get_table_names()


def _collapse_extra_roots(bind) -> list[tuple[str, str]]:
    """Re-parent every root except the oldest under that oldest root.

    Returns the list of ``(reparented_task_id, survivor_root_id)`` pairs
    performed (empty when there is at most one root — the no-op case).

    Split out from :func:`upgrade` so it is unit-testable against a plain
    sqlite connection without an Alembic op context (mirrors 0017's
    ``_sanitize_context_keys``). Works with BOTH an Alembic ``bind``
    (SQLAlchemy connection) and a raw ``sqlite3.Connection`` — a raw
    connection exposes ``.cursor``; a SQLAlchemy one does not — so we
    branch the parameter style once on that.
    """
    is_raw = hasattr(bind, "cursor")

    def run(sql: str, params: tuple = ()):  # noqa: ANN001
        if is_raw:
            return bind.execute(sql, params)
        named = {f"p{i}": v for i, v in enumerate(params)}
        # SQLAlchemy uses ``:name`` placeholders; positional ``?`` maps to
        # ``:p0``, ``:p1``, … in order.
        i = 0
        while "?" in sql:
            sql = sql.replace("?", f":p{i}", 1)
            i += 1
        return bind.execute(sa.text(sql), named)

    # Table-existence probe that works on both bind types.
    exists = run(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='tasks'"
    ).fetchone()
    if exists is None:
        return []

    rows = run(
        "SELECT task_id, child_tasks FROM tasks "
        "WHERE parent_task IS NULL "
        "ORDER BY created_at ASC, task_id ASC"
    ).fetchall()

    if len(rows) <= 1:
        return []

    survivor_id = rows[0][0]
    survivor_children = json.loads(rows[0][1] or "[]")

    reparented: list[tuple[str, str]] = []
    for task_id, _child in rows[1:]:
        run(
            "UPDATE tasks SET parent_task = ? WHERE task_id = ?",
            (survivor_id, task_id),
        )
        if task_id not in survivor_children:
            survivor_children.append(task_id)
        reparented.append((task_id, survivor_id))

    run(
        "UPDATE tasks SET child_tasks = ? WHERE task_id = ?",
        (json.dumps(survivor_children), survivor_id),
    )
    return reparented


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "tasks"):
        return

    # Repair FIRST so the unique index can be created on valid data.
    reparented = _collapse_extra_roots(bind)
    for reparented_id, survivor_id in reparented:
        print(
            f"0023_single_root_task_index: re-parented extra root "
            f"{reparented_id!r} under {survivor_id!r}"
        )

    bind.execute(
        sa.text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX} "
            "ON tasks ((parent_task IS NULL)) WHERE parent_task IS NULL"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(f"DROP INDEX IF EXISTS {_INDEX}"))
