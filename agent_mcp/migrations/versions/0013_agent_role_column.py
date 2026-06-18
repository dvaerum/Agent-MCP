"""phase-2 wave-1a: agent_role column on agents (worker|manager)

Revision ID: 0013_agent_role_column
Revises: 0012_message_threads_and_subjects
Create Date: 2026-06-17

Per `prancy-napping-pie.md` section 2a (Phase 2 — manager agent
role, v5.0.61), this migration adds a single column to the
`agents` table:

  * agent_role  TEXT NOT NULL DEFAULT 'worker'

with a CHECK constraint restricting the value to ``'worker'`` or
``'manager'``. Wave 2 will introduce the `@requires_role`
decorator that reads this column; this migration is data-layer
only and no production code path reads `agent_role` yet — but
landing the schema now means the Wave 2 PR is purely a code
change with no DB risk.

## SQLite ALTER TABLE constraints

SQLite's `ALTER TABLE ... ADD COLUMN` accepts NOT NULL columns
only if they have a DEFAULT, and accepts an inline CHECK
constraint as part of the column definition. Both are honoured
here: the DEFAULT backfills every existing row to ``'worker'`` in
one shot, and the CHECK is enforced from the first INSERT
onward (existing rows already satisfy it because they all carry
``'worker'`` post-default-backfill).

## ORM coexistence

`init_database()` runs `Base.metadata.create_all()` before this
migration. The ORM model (`agent_mcp.db.models.agent.Agent`)
declares `agent_role` with the same CheckConstraint, so a fresh
DB picks the column up via `create_all` first. This migration
then runs, sees the column already present (idempotent ADD
COLUMN via the `PRAGMA table_info` gate), and the upgrade is a
no-op for fresh DBs — the legacy upgrade path is the one that
exercises the ALTER TABLE branch.

## Why no batch_alter_table

A CHECK constraint can be expressed inline on `ADD COLUMN`, so
we don't need the table-rebuild dance that 0007/0008/0012 use
for FK changes. The inline CHECK is faster, simpler, and keeps
the migration trivially reversible (DROP COLUMN ditches the
constraint with it).

## Downgrade

SQLite 3.35+ supports `ALTER TABLE ... DROP COLUMN` directly,
and the project's deployment targets (NixOS unstable / 25.05 /
25.11) all ship a newer sqlite — same assumption that 0010 made
for its DROP COLUMN branches.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0013_agent_role_column"
down_revision: Union[str, None] = "0012_message_threads_and_subjects"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(bind, name: str) -> bool:
    inspector = sa.inspect(bind)
    return name in inspector.get_table_names()


def _column_names(bind, table: str) -> set[str]:
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return set()
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    if not _table_exists(bind, "agents"):
        return

    existing = _column_names(bind, "agents")
    if "agent_role" in existing:
        # Fresh DB path: create_all() already added the column from
        # the ORM model. Nothing to do.
        return

    # Legacy upgrade path: SQLite accepts inline CHECK on ADD COLUMN,
    # and the NOT NULL DEFAULT backfills every existing row in one
    # statement. Use DEFAULT 'worker' so existing agents stay in the
    # least-privileged tier (the safe default per the plan).
    bind.execute(
        sa.text(
            "ALTER TABLE agents "
            "ADD COLUMN agent_role TEXT NOT NULL DEFAULT 'worker' "
            "CHECK (agent_role IN ('worker', 'manager'))"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()

    if not _table_exists(bind, "agents"):
        return

    existing = _column_names(bind, "agents")
    if "agent_role" not in existing:
        return

    bind.execute(sa.text("ALTER TABLE agents DROP COLUMN agent_role"))
