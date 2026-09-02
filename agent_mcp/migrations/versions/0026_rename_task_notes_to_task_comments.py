"""rename task_notes -> task_comments (pure rename, PR 1/3)

Revision ID: 0026_rename_task_notes_to_task_comments
Revises: 0025_terminal_task_guard_trigger
Create Date: 2026-08-27

Renames the `task_notes` side table (introduced in migration 0009,
db-review PR-H) to `task_comments`. This is PR 1 of a 3-PR sequence
that renames the "note" concept to "comment" across the whole
task-notes feature — a hard cutover, no backward-compat alias. This
migration handles ONLY the table itself; the application-layer
rename (ORM model `TaskNote` -> `TaskComment`, tool names
`add_task_note` -> `add_task_comment` etc., the `task_notes_db` /
`task_notes_tools` modules) lands in the same PR as this migration,
and ships together as one hard cutover.

`op.rename_table` on SQLite is a plain `ALTER TABLE ... RENAME TO`
(3.25.0+ — the version this project already requires for the
`0002_project_context_ownership` column rename). SQLite's
`ALTER TABLE RENAME` automatically rewrites references to the old
table name inside trigger bodies and view definitions recorded in
`sqlite_master`, so the two terminal-state guard triggers installed
by migration 0025 (`trg_task_notes_terminal_guard_insert` /
`trg_task_notes_terminal_guard_update`) keep firing correctly against
the renamed table without being touched here — this is verified
empirically by this project's test suite (a terminal-status task
still blocks a new comment-insert post-rename), not just assumed from
the SQLite documentation.

The single-column index `idx_task_notes_task` is NOT auto-renamed by
`ALTER TABLE RENAME` (only the table name reference inside its
definition is updated) — SQLite has no `ALTER INDEX ... RENAME`, so
this migration drops and recreates it under the new name
(`idx_task_comments_task`) to match the renamed ORM model
(`agent_mcp.db.models.task_comment.TaskComment.__table_args__`).

`note_id` — the primary-key column — is deliberately NOT renamed to
`comment_id` here. Its name is a compatibility-neutral internal
detail (nothing in the JSON-RPC surface exposes a raw column name;
the tool schemas and dicts already return the value under the key
`note_id`, which stays `note_id` across this PR sequence), not part
of the user-facing "note -> comment" identity this rename is about.
Renaming a PK column carries real risk (every INSERT/SELECT/ORM
mapped_column site would need to move in lockstep with zero drift)
for a purely cosmetic win, so it is left as-is.

## The ORM-precedes-migrations race (found empirically, not assumed)

`agent_mcp/app/server_lifecycle.py` calls `init_database()`
(`Base.metadata.create_all()`, ORM-current schema) BEFORE
`run_migrations_upgrade()` on every boot. `create_all()` only
creates tables MISSING from the DB; it never touches a table that's
merely absent from the CURRENT model. On an existing deployment
whose DB still has the real `task_notes` table (not yet renamed),
booting the new (post-rename) code means: `init_database()` sees
`task_comments` (the new model's table) is missing and creates an
EMPTY stub for it, *then* migrations run and reach this revision —
whose naive `ALTER TABLE task_notes RENAME TO task_comments` would
collide with that stub (`OperationalError: there is already another
table or index with this name: task_comments`). The same race also
reproduces on a from-scratch install that bootstraps the schema via
`init_database()` and then replays every migration from base (this
project's own migration test harness does exactly that): migration
0009 unconditionally (re)creates an empty `task_notes` scaffold
because the ORM already created `task_comments` under the new name,
leaving both tables present by the time this revision runs.

Both races are handled the same way: if `task_comments` already
exists AND `task_notes` also exists, the `task_comments` side is
*always* the empty ORM-precreated stub (nothing can have written
real rows into it — migrations run to completion before the app ever
serves a tool call), so it is safe to drop and let the real
`task_notes` table (carrying its data and, per the SQLite behavior
above, its correctly-rewritten triggers) take the new name via the
normal rename path. `downgrade()` guards the symmetric case for the
same reason, defensively.

Verified empirically (not just reasoned through): this project's
migration-0025 regression test bootstraps exactly this way
(`init_database()` then a full base-to-head replay) and reproduced
the collision against the very first version of this migration
before the guard above was added.

Downgrade renames the table (and index) back to their pre-rename
names.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0026_rename_task_notes_to_task_comments"
down_revision: Union[str, None] = "0025_terminal_task_guard_trigger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(bind, name: str) -> bool:
    inspector = sa.inspect(bind)
    return name in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    old_exists = _table_exists(bind, "task_notes")
    new_exists = _table_exists(bind, "task_comments")

    if old_exists and new_exists:
        # ORM-precedes-migrations race (see module docstring): the
        # `task_comments` side here is always the empty stub
        # `init_database()` created under the new model ahead of this
        # migration — safe to drop so the real `task_notes` table can
        # take its rightful name below.
        bind.execute(sa.text("DROP TABLE task_comments"))
        new_exists = False

    if old_exists and not new_exists:
        op.rename_table("task_notes", "task_comments")
        bind.execute(sa.text("DROP INDEX IF EXISTS idx_task_notes_task"))
        bind.execute(
            sa.text(
                "CREATE INDEX IF NOT EXISTS idx_task_comments_task "
                "ON task_comments (task_id)"
            )
        )
    # else: neither table exists yet (schema not bootstrapped), or
    # `task_comments` already exists with no `task_notes` present
    # (already renamed, or a from-scratch install whose migration
    # replay never re-created the legacy scaffold) — nothing to do.


def downgrade() -> None:
    bind = op.get_bind()
    old_exists = _table_exists(bind, "task_comments")
    new_exists = _table_exists(bind, "task_notes")

    if old_exists and new_exists:
        # Symmetric guard to upgrade()'s race handling, defensively —
        # see module docstring.
        bind.execute(sa.text("DROP TABLE task_notes"))
        new_exists = False

    if old_exists and not new_exists:
        op.rename_table("task_comments", "task_notes")
        bind.execute(sa.text("DROP INDEX IF EXISTS idx_task_comments_task"))
        bind.execute(
            sa.text(
                "CREATE INDEX IF NOT EXISTS idx_task_notes_task "
                "ON task_notes (task_id)"
            )
        )
