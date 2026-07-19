"""event-coord PR-1: schema columns for event-driven agent coordination

Revision ID: 0010_event_coordination_columns
Revises: 0009_task_notes_side_table
Create Date: 2026-06-05

Per the event-driven coordination plan (PR-1, v5.0.9), three new
columns:

  * agents.auto_event_loop          BOOLEAN NOT NULL DEFAULT 1
  * agents.last_event_seen_at       TEXT NULL  (ISO timestamp string)
  * tasks.required_capabilities     TEXT NULL  (JSON list of
                                                lowercase strings)

`auto_event_loop` is the per-agent toggle that gates the wake-loop
bootstrap shipped in PR-2. Default TRUE means every existing agent
automatically opts into the wake-loop once PR-2 ships; operators can
flip it off per-agent via the dashboard Agent Edit dialog, or globally
via the new `project_context["config_auto_event_loop_global"]` row.

`last_event_seen_at` is the per-agent cursor used by the
`fetch_events_since` tool (also PR-2) to catch up after a
disconnect. NULL means "no events seen yet" — `fetch_events_since`
treats it as "from the beginning".

`tasks.required_capabilities` was the JSON-encoded list of capability
labels a worker had to satisfy (subset match) to receive an
`unassigned_task_appeared` event for that task (PR-2 wiring). This
structured capability-tag routing was RETIRED in migration 0019
(behaviour-preserving — the subset filter was already a no-op); the
column is physically dropped there. This migration is kept unchanged so
the historical add→drop chain still applies cleanly on legacy DBs.

## SQLite ALTER TABLE constraints

SQLite's `ALTER TABLE ... ADD COLUMN` accepts NOT NULL columns only if
they have a DEFAULT. `auto_event_loop` has DEFAULT 1, which doubles
as the backfill value for every existing row — no separate UPDATE
needed (SQLite materialises the default into every existing row when
the column is added).

The two TEXT-nullable columns add without any default trickery.

## Idempotency

Each ADD COLUMN is gated on a `PRAGMA table_info` check, so running
the migration twice (or on a DB that picked up the columns via a
hand-applied patch) is a no-op rather than a `duplicate column
name` failure.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0010_event_coordination_columns"
down_revision: Union[str, None] = "0009_task_notes_side_table"
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

    if _table_exists(bind, "agents"):
        existing = _column_names(bind, "agents")
        if "auto_event_loop" not in existing:
            # SQLite ALTER TABLE ADD COLUMN with NOT NULL DEFAULT is
            # legal and backfills every existing row with the default
            # in one shot — exactly the (iv) test assertion.
            bind.execute(
                sa.text(
                    "ALTER TABLE agents "
                    "ADD COLUMN auto_event_loop BOOLEAN NOT NULL DEFAULT 1"
                )
            )
        if "last_event_seen_at" not in existing:
            bind.execute(
                sa.text(
                    "ALTER TABLE agents ADD COLUMN last_event_seen_at TEXT"
                )
            )

    if _table_exists(bind, "tasks"):
        existing = _column_names(bind, "tasks")
        if "required_capabilities" not in existing:
            bind.execute(
                sa.text(
                    "ALTER TABLE tasks "
                    "ADD COLUMN required_capabilities TEXT"
                )
            )


def downgrade() -> None:
    """SQLite's `ALTER TABLE ... DROP COLUMN` was added in 3.35; the
    target deployments (NixOS unstable / 25.05 / 25.11) all ship a
    newer sqlite, so we use it directly. Idempotent on column-already-
    absent."""
    bind = op.get_bind()

    if _table_exists(bind, "agents"):
        existing = _column_names(bind, "agents")
        if "last_event_seen_at" in existing:
            bind.execute(
                sa.text("ALTER TABLE agents DROP COLUMN last_event_seen_at")
            )
        if "auto_event_loop" in existing:
            bind.execute(
                sa.text("ALTER TABLE agents DROP COLUMN auto_event_loop")
            )

    if _table_exists(bind, "tasks"):
        existing = _column_names(bind, "tasks")
        if "required_capabilities" in existing:
            bind.execute(
                sa.text(
                    "ALTER TABLE tasks DROP COLUMN required_capabilities"
                )
            )
