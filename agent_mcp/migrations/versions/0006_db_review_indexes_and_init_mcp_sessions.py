"""db review indexes (items 1 + 7) + mcp_sessions parity (item 15)

Revision ID: 0006_db_review_indexes
Revises: 0005_mcp_sessions_alias_used
Create Date: 2026-06-03

The 2026-06-02 database review's two critical items both reduce to
index work:

  1. `wait_for_events` polls `tasks WHERE assigned_to = ? ORDER BY
     updated_at DESC` with no covering index — a full scan every
     poll. The composite `(assigned_to, updated_at DESC)` collapses
     that to an index seek.
  7. Hot single-column filters that today force scans:
        - `tasks.status` (every "pending / in_progress" view)
        - `tasks.priority` (sort / filter on dashboard)
        - `agent_messages.delivered` (cleanup of delivered messages)
        - `claude_code_sessions.status` (admin view filter)

This migration adds those five indexes. It is intentionally bundled
with `init_database()` updates in the same PR so a brand-new DB
that hasn't run Alembic yet still has the indexes (the raw-SQL
init_database CREATE INDEX IF NOT EXISTS calls).

`mcp_sessions` parity (item 15) is handled in init_database directly
— this migration touches only indexes. Alembic remains the source of
truth for the schema; init_database just stops being a footgun on
fresh DBs.

All operations use `IF NOT EXISTS`/`CREATE INDEX IF NOT EXISTS` so
re-running the migration on a partially-applied DB is safe.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0006_db_review_indexes"
down_revision: Union[str, None] = "0005_mcp_sessions_alias_used"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_indexes(bind, table: str) -> set[str]:
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return set()
    return {ix["name"] for ix in inspector.get_indexes(table)}


# Indexes this migration owns. Tuple is (name, table, ddl). We use raw
# CREATE INDEX DDL rather than op.create_index for two reasons:
#
#   * `op.create_index` does not support the per-column DESC modifier
#     that the (assigned_to, updated_at DESC) composite needs to match
#     the wait_for_events ORDER BY direction.
#   * Sticking to raw DDL keeps the migration verbatim-matchable
#     against the `CREATE INDEX IF NOT EXISTS` lines in
#     `agent_mcp/db/schema.py::init_database`, which is the
#     fresh-DB parity surface.
_INDEX_DDL: list[tuple[str, str, str]] = [
    (
        "idx_tasks_assigned_to_updated_at",
        "tasks",
        "CREATE INDEX IF NOT EXISTS idx_tasks_assigned_to_updated_at "
        "ON tasks (assigned_to, updated_at DESC)",
    ),
    (
        "idx_tasks_status",
        "tasks",
        "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status)",
    ),
    (
        "idx_tasks_priority",
        "tasks",
        "CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks (priority)",
    ),
    (
        "idx_agent_messages_delivered",
        "agent_messages",
        "CREATE INDEX IF NOT EXISTS idx_agent_messages_delivered "
        "ON agent_messages (delivered)",
    ),
    (
        "idx_claude_sessions_status",
        "claude_code_sessions",
        "CREATE INDEX IF NOT EXISTS idx_claude_sessions_status "
        "ON claude_code_sessions (status)",
    ),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for name, table, ddl in _INDEX_DDL:
        if table not in tables:
            # Tables come from `init_database()` at lifespan startup;
            # if any are missing here, something else is wrong and the
            # index is moot. Skip silently rather than crash.
            continue
        op.execute(ddl)


def downgrade() -> None:
    bind = op.get_bind()
    for name, table, _ddl in reversed(_INDEX_DDL):
        existing = _existing_indexes(bind, table)
        if name in existing:
            op.drop_index(name, table_name=table)
