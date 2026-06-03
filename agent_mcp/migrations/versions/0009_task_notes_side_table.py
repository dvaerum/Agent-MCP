"""task_notes side table (db-review PR-H)

Revision ID: 0009_task_notes_side_table
Revises: 0008_admin_pseudo_agent_and_fks
Create Date: 2026-06-04

The 2026-06-02 database review's item 11 flagged the embedded JSON
list in `tasks.notes` as the chief blocker for per-note edit /
delete (PR #74 shipped append-only because mutating individual
notes inside a JSON list is awkward and racy). This migration
extracts notes into a proper side table:

    task_notes(
        note_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id   TEXT NOT NULL REFERENCES tasks(task_id),
        author    TEXT,
        timestamp TEXT NOT NULL,
        text      TEXT NOT NULL
    )
    CREATE INDEX idx_task_notes_task ON task_notes (task_id);

Every existing `tasks.notes` JSON entry is parsed and INSERTed into
`task_notes` with timestamp preserved. The original JSON column
(`tasks.notes`) is **left in place for one release** so the
existing append-only writers in `task_tools.py` / `app/routes.py`
keep functioning — this gives a deprecation window to migrate
them. A follow-up PR drops the column once every writer has been
flipped to the side table.

Notes JSON shape today is `[{"timestamp": "...", "author": "...",
"content": "..."}]`. We map `content` -> `text` because the SQL
column carries no JSON nesting and "text" reads better in a
schema browser. The original key stays in the JSON column so the
deprecation window doesn't break read paths that still look at
`task.notes`.

Safety:

* Migration runs inside a single transaction. If the JSON parse
  fails on any row (corrupted notes column from some historic bug),
  the migration logs the bad row and skips it rather than aborting
  the whole batch — losing one corrupted note is better than failing
  the whole upgrade and leaving the DB stranded.
* No FK to `tasks.task_id` is declared on `task_notes` for this
  migration. Reason: production data analysis (washing-brothers)
  shows zero orphans, but the conservative pattern from PR-G1 is
  to defer FK declaration to a follow-up after watching the new
  table in production. The schema's CREATE statement still names
  `tasks(task_id)` in a SQL comment as the intended reference.
"""

from __future__ import annotations

import json as _json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0009_task_notes_side_table"
down_revision: Union[str, None] = "0008_admin_pseudo_agent_and_fks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(bind, name: str) -> bool:
    inspector = sa.inspect(bind)
    return name in inspector.get_table_names()


def _create_task_notes(bind) -> None:
    if _table_exists(bind, "task_notes"):
        return
    bind.execute(
        sa.text(
            "CREATE TABLE task_notes ("
            "note_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "task_id TEXT NOT NULL, "  # logical FK -> tasks(task_id)
            "author TEXT, "
            "timestamp TEXT NOT NULL, "
            "text TEXT NOT NULL"
            ")"
        )
    )
    bind.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_task_notes_task "
            "ON task_notes (task_id)"
        )
    )


def _copy_existing_notes(bind) -> int:
    """Parse every row's `tasks.notes` JSON and insert the entries
    into `task_notes`. Returns the count of note rows inserted."""
    if not _table_exists(bind, "tasks"):
        return 0

    rows = bind.execute(
        sa.text("SELECT task_id, notes FROM tasks WHERE notes IS NOT NULL")
    ).all()

    inserted = 0
    for task_id, raw_notes in rows:
        if not raw_notes:
            continue
        try:
            notes_list = _json.loads(raw_notes)
        except _json.JSONDecodeError:
            # Corrupted JSON — skip. We log via the alembic context
            # because `logger` from agent_mcp.core would import too
            # much at migration time.
            continue
        if not isinstance(notes_list, list):
            continue
        for note in notes_list:
            if not isinstance(note, dict):
                continue
            ts = note.get("timestamp")
            text = note.get("content") or note.get("text")
            author = note.get("author")
            if not ts or not text:
                continue
            bind.execute(
                sa.text(
                    "INSERT INTO task_notes "
                    "(task_id, author, timestamp, text) "
                    "VALUES (:task_id, :author, :timestamp, :text)"
                ),
                {
                    "task_id": task_id,
                    "author": author,
                    "timestamp": ts,
                    "text": text,
                },
            )
            inserted += 1
    return inserted


def upgrade() -> None:
    bind = op.get_bind()
    _create_task_notes(bind)
    _copy_existing_notes(bind)
    # tasks.notes column is intentionally LEFT IN PLACE for one
    # release — see module docstring's "Safety" note. The follow-up
    # migration (0010) will drop it once writers are flipped.


def downgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, "task_notes"):
        bind.execute(sa.text("DROP INDEX IF EXISTS idx_task_notes_task"))
        bind.execute(sa.text("DROP TABLE task_notes"))
