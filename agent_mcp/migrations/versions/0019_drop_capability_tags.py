"""retire structured capability tags: drop agents.capabilities +
tasks.required_capabilities

Revision ID: 0019_drop_capability_tags
Revises: 0018_agent_profile_columns
Create Date: 2026-07-19

Per plan ``agent-profile-self-service.md`` §5 / §6 (PR5), this migration
physically drops the two columns that backed the vestigial structured
capability-tag routing:

  * ``agents.capabilities``          — JSON tag list (never populated in
                                       any project; see plan §3).
  * ``tasks.required_capabilities``  — JSON tag list the ``req ⊆ caps``
                                       unassigned-task filter matched
                                       against.

The routing filter was already a no-op ("empty required matches
everyone"), so removing the columns changes NO observable behaviour —
unassigned-task events already fanned out to every agent. This is a
behaviour-preserving cleanup so "capabilities" stops being an overloaded
word (the Wave-9 authorization capability vocabulary in
``agent_mcp.core.capabilities`` is a wholly separate concept and is NOT
touched by this migration).

## SQLite DROP COLUMN via batch_alter_table

SQLite has no in-place ``DROP COLUMN`` on the versions we target for the
legacy path, so both drops go through Alembic's
``batch_alter_table`` — the create-new → copy-rows → drop-old → rename
dance. Batch mode reflects the live table (columns, PRIMARY KEY, UNIQUE,
the ``ck_agents_agent_role_domain`` CHECK on ``agents``, every index, and
every foreign key a legacy DB carries from 0007/0008) and rebuilds it
minus the dropped column, so all constraints / indexes / FKs are carried
forward automatically. env.py already runs the batch rebuild with
``PRAGMA foreign_keys=OFF`` and a ``foreign_key_check`` safety net after
commit; neither dropped column participates in any FK, so no orphan
cleanup is needed.

## ORM coexistence / idempotency

``init_database()`` runs ``Base.metadata.create_all()`` first. As of this
PR the ORM models (``agent_mcp.db.models.agent.Agent`` /
``...task.Task``) no longer declare these columns, so a FRESH DB is
already at the post-drop shape and the ``PRAGMA table_info`` gate makes
the drop branch a no-op. The legacy upgrade path (a DB that still carries
the columns) is the one that runs the batch rebuild.

## Downgrade

Re-adds both columns as plain nullable TEXT (reversibility). The original
data is not restored — a downgrade yields empty (NULL) columns, which is
the correct "no tags" state given the columns were never populated.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0019_drop_capability_tags"
down_revision: Union[str, None] = "0018_agent_profile_columns"
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

    # agents.capabilities — drop via batch rebuild (carries forward the
    # PK/UNIQUE/CHECK/indexes/FKs by reflection). Idempotent: on a fresh
    # DB the column is already absent, so skip.
    if _table_exists(bind, "agents") and "capabilities" in _column_names(
        bind, "agents"
    ):
        with op.batch_alter_table("agents") as batch_op:
            batch_op.drop_column("capabilities")

    # tasks.required_capabilities — same treatment.
    if _table_exists(bind, "tasks") and "required_capabilities" in _column_names(
        bind, "tasks"
    ):
        with op.batch_alter_table("tasks") as batch_op:
            batch_op.drop_column("required_capabilities")

        # batch_alter_table rebuilds `tasks` via copy, and Alembic's
        # reflection recreates composite indexes WITHOUT their DESC
        # modifier — same maintenance step migrations 0007 / 0008 / 0012 /
        # 0014 perform after a rebuild. Restore the DESC ordering on the
        # wait_for_events hot-path index so BL-R20-1 (oldest-first /
        # cursor) stays anchored and the index-order invariant test holds.
        inspector = sa.inspect(bind)
        idx_names = {ix["name"] for ix in inspector.get_indexes("tasks")}
        if "idx_tasks_assigned_to_updated_at" in idx_names:
            op.drop_index(
                "idx_tasks_assigned_to_updated_at", table_name="tasks"
            )
        op.execute(
            "CREATE INDEX idx_tasks_assigned_to_updated_at "
            "ON tasks (assigned_to, updated_at DESC)"
        )


def downgrade() -> None:
    bind = op.get_bind()

    # Re-add as plain nullable TEXT. Idempotent: skip if already present.
    if _table_exists(bind, "agents") and "capabilities" not in _column_names(
        bind, "agents"
    ):
        bind.execute(sa.text("ALTER TABLE agents ADD COLUMN capabilities TEXT"))

    if _table_exists(bind, "tasks") and "required_capabilities" not in _column_names(
        bind, "tasks"
    ):
        bind.execute(
            sa.text("ALTER TABLE tasks ADD COLUMN required_capabilities TEXT")
        )
