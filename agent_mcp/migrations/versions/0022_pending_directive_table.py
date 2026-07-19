"""pending_directive table (ad-hoc poke queue)

Revision ID: 0022_pending_directive_table
Revises: 0021_scheduled_directive_table
Create Date: 2026-07-19

Per plan ``event-loop-scheduled-directives.md`` §2 decision 11 + §3, an
operator/admin **poke** pushes a single directive to an agent out-of-band.
It is delivered immediately if the agent is listening (waiter-wake) or
queued as highest priority for its next check-in. Scheduled fires do NOT
use this table (they are derived from ``scheduled_directive.next_due_at``);
this store is only the one-shot poke queue.

    pending_directive(
        poke_id      TEXT PRIMARY KEY,
        agent_id     TEXT NOT NULL,   -- logical FK -> agents(agent_id)
        prompt       TEXT NOT NULL,
        priority     TEXT NOT NULL DEFAULT 'urgent',
        created_at   TEXT NOT NULL,
        created_by   TEXT,
        delivered_at TEXT
    )
    CREATE INDEX idx_pending_directive_undelivered
        ON pending_directive (agent_id, delivered_at);

The index backs the per-check-in "any undelivered pokes for me?" query
(``delivered_at IS NULL``).

## SQLite ALTER TABLE / ORM coexistence

Same pattern as 0021: ``init_database()`` runs ``create_all()`` first, so
on a FRESH DB the ORM model creates this table and this migration no-ops
(the ``_table_exists`` gate). The legacy upgrade path runs the raw CREATE.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0022_pending_directive_table"
down_revision: Union[str, None] = "0021_scheduled_directive_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "pending_directive"
_INDEX = "idx_pending_directive_undelivered"


def _table_exists(bind, name: str) -> bool:
    inspector = sa.inspect(bind)
    return name in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, _TABLE):
        return
    bind.execute(
        sa.text(
            f"CREATE TABLE {_TABLE} ("
            "poke_id TEXT PRIMARY KEY, "
            "agent_id TEXT NOT NULL, "  # logical FK -> agents(agent_id)
            "prompt TEXT NOT NULL, "
            "priority TEXT NOT NULL DEFAULT 'urgent', "
            "created_at TEXT NOT NULL, "
            "created_by TEXT, "
            "delivered_at TEXT"
            ")"
        )
    )
    bind.execute(
        sa.text(
            f"CREATE INDEX IF NOT EXISTS {_INDEX} "
            f"ON {_TABLE} (agent_id, delivered_at)"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, _TABLE):
        bind.execute(sa.text(f"DROP INDEX IF EXISTS {_INDEX}"))
        bind.execute(sa.text(f"DROP TABLE {_TABLE}"))
