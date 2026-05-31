"""baseline — initial schema (raw SQL owns CREATE TABLE for now)

Revision ID: 0001_baseline
Revises:
Create Date: 2026-05-31

This is the empty foot-of-the-mountain migration. It exists so
Alembic can stamp the per-project DB with `version_num = '0001_baseline'`
on first encounter; the actual `CREATE TABLE` statements are still
emitted by `agent_mcp.db.schema.init_database()` via raw SQL. As more
tables get ORM models (phases 7g–7m), they will land as 0002, 0003,
… and gradually take over from the raw-SQL bootstrap.

Carries `branch_labels=("agent_mcp",)` so that if rinadelph's
upstream ever adopts Alembic with its own root revision, the two
migration trees can coexist on the same DB without colliding.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = ("agent_mcp",)
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # No-op: schema lives in agent_mcp/db/schema.py until each table
    # gets its own ORM migration. The trivial SELECT confirms the
    # migration ran (visible in --sql output / verbose logs).
    op.execute("SELECT 1")


def downgrade() -> None:
    op.execute("SELECT 1")
