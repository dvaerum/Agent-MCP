"""agent self-service profile columns on agents

Revision ID: 0018_agent_profile_columns
Revises: 0017_sanitize_memory_keys
Create Date: 2026-07-19

Per plan ``agent-profile-self-service.md`` §5, this migration adds four
nullable columns to the ``agents`` table so every agent can carry a
self-authored free-text ``profile`` plus the review/change bookkeeping
the governance story rides on:

  * ``profile``             TEXT NULL — self-authored prose ("what I do,
                            how I work, what to ask me about"). NULL/''
                            means never set.
  * ``profile_updated_at``  TEXT NULL — ISO-8601; bumped ONLY on content
                            change. Drives the peer-broadcast event.
  * ``profile_reviewed_at`` TEXT NULL — ISO-8601; bumped on EVERY review
                            (even a no-op confirm). Drives the staleness
                            nudge.
  * ``profile_updated_by``  TEXT NULL — agent_id of whoever last changed
                            the content (NULL = system/seed).

All four are nullable so existing rows stay valid without a backfill —
a NULL ``profile`` reads as "never set" and a NULL ``profile_reviewed_at``
reads as "overdue" (the review nudge fires on first connect anyway).

## SQLite ALTER TABLE / ORM coexistence

Same pattern as 0013 (``agent_role``): ``init_database()`` runs
``Base.metadata.create_all()`` first, so on a FRESH DB the ORM model
(``agent_mcp.db.models.agent.Agent``) already declares these columns and
this migration sees them present (the ``PRAGMA table_info`` gate) and
is a no-op. The legacy upgrade path is the one that runs the four
``ADD COLUMN`` statements. Plain nullable TEXT columns need no DEFAULT
and no CHECK, so the simple ``ADD COLUMN`` form suffices (no
``batch_alter_table`` rebuild).

## Downgrade

SQLite 3.35+ supports ``ALTER TABLE ... DROP COLUMN`` directly (same
assumption 0013 made). Drops all four columns.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0018_agent_profile_columns"
down_revision: Union[str, None] = "0017_sanitize_memory_keys"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_PROFILE_COLUMNS: tuple[str, ...] = (
    "profile",
    "profile_updated_at",
    "profile_reviewed_at",
    "profile_updated_by",
)


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
    for name in _PROFILE_COLUMNS:
        if name in existing:
            # Fresh DB path: create_all() already added the column from
            # the ORM model. Nothing to do for this one.
            continue
        # Legacy upgrade path: plain nullable TEXT — no DEFAULT, no CHECK.
        bind.execute(sa.text(f"ALTER TABLE agents ADD COLUMN {name} TEXT"))


def downgrade() -> None:
    bind = op.get_bind()

    if not _table_exists(bind, "agents"):
        return

    existing = _column_names(bind, "agents")
    # Drop in reverse so a partially-applied upgrade downgrades cleanly.
    for name in reversed(_PROFILE_COLUMNS):
        if name in existing:
            bind.execute(sa.text(f"ALTER TABLE agents DROP COLUMN {name}"))
