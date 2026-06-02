"""mcp_sessions — durable registry of open Streamable HTTP /mcp streams

Revision ID: 0004_mcp_sessions
Revises: 0003_agents_aoe_session_id
Create Date: 2026-06-02

Streamable HTTP stateless mode (mcp-spec rev 2025-03-26) spawns a
fresh per-request server task with its own `request_ctx`. That means
MCP-protocol push notifications (`notifications/resources/updated`,
`notifications/tools/list_changed`) emitted by tool calls only reach
the *current* request's session — there's no in-process enumeration
of other open GET /mcp streams to fan out to.

This table is the cross-request **discovery layer** for fan-out: every
open GET /mcp registers a row, every disconnect removes one, every
heartbeat bumps `last_seen_at`. Emitters enumerate rows for the target
agent (or all rows for tool-list changes) and look up each session in
the companion in-memory runtime-queue map to actually push.

Why SQLite and not pure in-memory:

  * Durability across restarts. A backend restart shouldn't lose the
    registry contents — a reconnecting client picks up where it left
    off, the registry row is touched, lifecycle continues.
  * Stable storage even when the in-memory queue map is empty (e.g.
    right after a restart, before the first reconnect). Operators can
    inspect the table to see which agents had open subscriptions.
  * Cross-process readability: the dashboard could in principle surface
    "agents currently subscribed" via REST without coordinating with
    the MCP transport task.

`bearer_token_hash` stores `sha256(bearer)` rather than the raw token.
The registry only needs to confirm "is this the same bearer that
opened the stream?" during disconnect cleanup — never to re-issue or
log it, so the raw value never lands in the row.

ISO-UTC strings are used for `opened_at` / `last_seen_at` rather than
SQLite's `DATETIME` synonym so they round-trip unambiguously through
Python `datetime.fromisoformat()` (the agent_messages table uses the
same convention).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0004_mcp_sessions"
down_revision: Union[str, None] = "0003_agents_aoe_session_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(bind, name: str) -> bool:
    inspector = sa.inspect(bind)
    return name in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, "mcp_sessions"):
        return
    op.create_table(
        "mcp_sessions",
        sa.Column("session_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("opened_at", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.Text(), nullable=False),
        sa.Column("bearer_token_hash", sa.Text(), nullable=False),
    )
    op.create_index(
        "idx_mcp_sessions_agent",
        "mcp_sessions",
        ["agent_id"],
    )
    op.create_index(
        "idx_mcp_sessions_last_seen",
        "mcp_sessions",
        ["last_seen_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "mcp_sessions"):
        return
    op.drop_index("idx_mcp_sessions_last_seen", table_name="mcp_sessions")
    op.drop_index("idx_mcp_sessions_agent", table_name="mcp_sessions")
    op.drop_table("mcp_sessions")
