"""scheduled_directive table (event-loop scheduled directives)

Revision ID: 0021_scheduled_directive_table
Revises: 0020_agent_last_activity_at
Create Date: 2026-07-19

Per plan ``event-loop-scheduled-directives.md`` §3, a **directive** is a
recurring imperative an agent self-registers (or a manager/operator
registers for it) that fires *when the agent next checks in* at-or-after
its interval. Firing is wait-loop-native — this row is pure state
(``next_due_at``) and the ``wait_for_events`` slice loop is the sole
driver; there is no background sweeper.

This migration creates the ``scheduled_directive`` store:

    scheduled_directive(
        directive_id     TEXT PRIMARY KEY,
        agent_id         TEXT NOT NULL,   -- logical FK -> agents(agent_id)
        prompt           TEXT NOT NULL,
        interval_seconds INTEGER NOT NULL,
        next_due_at      TEXT NOT NULL,
        enabled          INTEGER NOT NULL DEFAULT 1,
        status           TEXT NOT NULL DEFAULT 'active',
        until_at         TEXT,
        max_runs         INTEGER,
        run_count        INTEGER NOT NULL DEFAULT 0,
        created_at       TEXT NOT NULL,
        created_by       TEXT,
        updated_at       TEXT,
        updated_by       TEXT
    )
    CREATE INDEX idx_scheduled_directive_due
        ON scheduled_directive (agent_id, enabled, next_due_at);

The index backs the per-slice ``SELECT min(next_due_at)`` the wait loop
runs to compute the soonest wake condition.

## SQLite ALTER TABLE / ORM coexistence

Same pattern as 0009 (``task_notes``): ``init_database()`` runs
``Base.metadata.create_all()`` first, so on a FRESH DB the ORM model
(``agent_mcp.db.models.scheduled_directive.ScheduledDirective``) already
creates this table and the ``_table_exists`` gate makes this migration a
no-op. The legacy upgrade path runs the ``CREATE TABLE`` + ``CREATE
INDEX`` below.

## Downgrade

Drop the index then the table (SQLite handles both directly).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0021_scheduled_directive_table"
down_revision: Union[str, None] = "0020_agent_last_activity_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "scheduled_directive"
_INDEX = "idx_scheduled_directive_due"


def _table_exists(bind, name: str) -> bool:
    inspector = sa.inspect(bind)
    return name in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, _TABLE):
        # Fresh DB path: create_all() already made the table + index.
        return
    bind.execute(
        sa.text(
            f"CREATE TABLE {_TABLE} ("
            "directive_id TEXT PRIMARY KEY, "
            "agent_id TEXT NOT NULL, "  # logical FK -> agents(agent_id)
            "prompt TEXT NOT NULL, "
            "interval_seconds INTEGER NOT NULL, "
            "next_due_at TEXT NOT NULL, "
            "enabled INTEGER NOT NULL DEFAULT 1, "
            "status TEXT NOT NULL DEFAULT 'active', "
            "until_at TEXT, "
            "max_runs INTEGER, "
            "run_count INTEGER NOT NULL DEFAULT 0, "
            "created_at TEXT NOT NULL, "
            "created_by TEXT, "
            "updated_at TEXT, "
            "updated_by TEXT"
            ")"
        )
    )
    bind.execute(
        sa.text(
            f"CREATE INDEX IF NOT EXISTS {_INDEX} "
            f"ON {_TABLE} (agent_id, enabled, next_due_at)"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, _TABLE):
        bind.execute(sa.text(f"DROP INDEX IF EXISTS {_INDEX}"))
        bind.execute(sa.text(f"DROP TABLE {_TABLE}"))
