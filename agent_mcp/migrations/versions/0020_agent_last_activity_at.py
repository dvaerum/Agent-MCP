"""agent last_activity_at column for event-loop idle-stop

Revision ID: 0020_agent_last_activity_at
Revises: 0019_drop_capability_tags
Create Date: 2026-07-19

Per plan ``event-loop-longlived-connections.md`` §5, the event-loop
idle-stop feature winds an agent down after a configurable window with no
REAL events. That needs a per-agent "when did this agent last receive a
real event" marker measured across reconnects — distinct from
``last_event_seen_at`` (the fetch cursor / high-water event TIMESTAMP,
which conflates "newest event I've seen" with "when I last got activity").

This migration adds one nullable column:

  * ``last_activity_at`` TEXT NULL — ISO-8601 wall-clock time the agent
    last received a real event (or first started listening, seeded on the
    first ``wait_for_events`` call). NULL = never seeded yet; the tool
    seeds it to "now" on first use, so the idle clock starts when the
    agent begins listening rather than counting a brand-new agent as
    instantly idle.

Nullable so existing rows stay valid without a backfill.

## SQLite ALTER TABLE / ORM coexistence

Same pattern as 0018 (``profile*``): ``init_database()`` runs
``Base.metadata.create_all()`` first, so on a FRESH DB the ORM model
(``agent_mcp.db.models.agent.Agent``) already declares this column and the
``PRAGMA table_info`` gate makes this migration a no-op. The legacy
upgrade path runs the single ``ADD COLUMN``. A plain nullable TEXT column
needs no DEFAULT and no CHECK, so the simple form suffices.

## Downgrade

SQLite 3.35+ supports ``ALTER TABLE ... DROP COLUMN`` directly.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0020_agent_last_activity_at"
down_revision: Union[str, None] = "0019_drop_capability_tags"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLUMN = "last_activity_at"


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
    if _COLUMN in _column_names(bind, "agents"):
        # Fresh DB path: create_all() already added the column.
        return
    bind.execute(sa.text(f"ALTER TABLE agents ADD COLUMN {_COLUMN} TEXT"))


def downgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "agents"):
        return
    if _COLUMN in _column_names(bind, "agents"):
        bind.execute(sa.text(f"ALTER TABLE agents DROP COLUMN {_COLUMN}"))
