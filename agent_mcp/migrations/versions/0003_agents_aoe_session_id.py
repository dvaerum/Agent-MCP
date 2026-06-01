"""agents.aoe_session_id — per-agent Agents-of-Empires session binding

Revision ID: 0003_agents_aoe_session_id
Revises: 0002_project_context_ownership
Create Date: 2026-06-01

The AoE notification side-channel needs to know which AoE tmux
session belongs to which agent-mcp worker. The first iteration
discovered this by matching ``title == agent_id`` against AoE's
``/api/sessions`` list — brittle: admins rename sessions, titles
collide, there's no first-class record.

This migration adds a nullable ``aoe_session_id`` column on the
``agents`` table. The dashboard's agent edit dialog exposes it; the
notifier prefers it over title-match resolution but still falls back
to the old behaviour when the column is NULL (backwards-compat for
deployments that already curated AoE titles to match agent ids).

Values are 16-char lowercase hex strings (AoE's own id format), but
SQL stores them as plain TEXT — validation happens at the API layer.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0003_agents_aoe_session_id"
down_revision: Union[str, None] = "0002_project_context_ownership"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_columns(bind) -> set[str]:
    inspector = sa.inspect(bind)
    return {c["name"] for c in inspector.get_columns("agents")}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _existing_columns(bind)
    if "aoe_session_id" not in cols:
        with op.batch_alter_table("agents") as batch_op:
            batch_op.add_column(sa.Column("aoe_session_id", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    cols = _existing_columns(bind)
    if "aoe_session_id" in cols:
        with op.batch_alter_table("agents") as batch_op:
            batch_op.drop_column("aoe_session_id")
