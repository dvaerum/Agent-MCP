"""mcp_sessions.alias_used — record which alias (if any) routed each stream

Revision ID: 0005_mcp_sessions_alias_used
Revises: 0004_mcp_sessions
Create Date: 2026-06-03

Phase 1c of the router-upstream plan ships per-session alias
telemetry: when the router proxies a request that arrived on an alias
URL, it forwards `X-Agent-MCP-Alias: <alias_name>,<expires_at>` to the
backend. The backend's session-opener parses that header and persists
`alias_name` here so an operator can later answer "which alias is
still receiving traffic, and when did its last subscriber close?"
without re-joining against the router-side registry.

Column shape:
  * `alias_used TEXT` — nullable. NULL means the stream was opened on
    the canonical project URL (the common case). When non-NULL it
    holds the alias's canonical name only, not the
    `name,expires_at` blob; the expiry comes from the router-side
    registry and is not re-derived from this row.

Index:
  * `(alias_used, last_seen_at)` — covers the canonical operator
    query "for alias X, when was its last subscriber active?"
    SQLite can satisfy `WHERE alias_used = ? ORDER BY last_seen_at
    DESC LIMIT 1` from the index alone with this ordering.

`op.batch_alter_table` is used for SQLite compatibility — the runtime
target. Same pattern as 0002 (`project_context_ownership`); SQLite's
ALTER TABLE only handles ADD COLUMN natively, batch_alter_table works
around that uniformly.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0005_mcp_sessions_alias_used"
down_revision: Union[str, None] = "0004_mcp_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_columns(bind, table: str) -> set[str]:
    inspector = sa.inspect(bind)
    return {c["name"] for c in inspector.get_columns(table)}


def _existing_indexes(bind, table: str) -> set[str]:
    inspector = sa.inspect(bind)
    return {ix["name"] for ix in inspector.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "mcp_sessions" not in inspector.get_table_names():
        # 0004 will have created it before this revision runs; defend
        # the fresh-DB path anyway so partial schemas don't crash the
        # upgrader.
        return

    cols = _existing_columns(bind, "mcp_sessions")
    if "alias_used" not in cols:
        with op.batch_alter_table("mcp_sessions") as batch_op:
            batch_op.add_column(sa.Column("alias_used", sa.Text(), nullable=True))

    indexes = _existing_indexes(bind, "mcp_sessions")
    if "idx_mcp_sessions_alias_used" not in indexes:
        op.create_index(
            "idx_mcp_sessions_alias_used",
            "mcp_sessions",
            ["alias_used", "last_seen_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "mcp_sessions" not in inspector.get_table_names():
        return

    indexes = _existing_indexes(bind, "mcp_sessions")
    if "idx_mcp_sessions_alias_used" in indexes:
        op.drop_index("idx_mcp_sessions_alias_used", table_name="mcp_sessions")

    cols = _existing_columns(bind, "mcp_sessions")
    if "alias_used" in cols:
        with op.batch_alter_table("mcp_sessions") as batch_op:
            batch_op.drop_column("alias_used")
