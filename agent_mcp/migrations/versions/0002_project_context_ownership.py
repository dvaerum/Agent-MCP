"""project_context creator-ownership columns + last_updated -> updated_at

Revision ID: 0002_project_context_ownership
Revises: 0001_baseline
Create Date: 2026-05-31

Phase 7b adds per-entry creator ownership to project_context:
- New `created_at` / `created_by` columns (nullable in SQL; the
  application always writes them on INSERT).
- Rename `last_updated` -> `updated_at` for consistency with the
  `created_at` naming convention.
- Backfill `created_at`/`created_by` from `updated_at`/`updated_by`
  for legacy rows so existing entries don't fail the ownership
  check (they appear "created by whoever last wrote them," which is
  the only signal available retroactively).

SQLite ALTER TABLE RENAME COLUMN works in 3.25+, but we use Alembic's
batch_alter_table for safety — it handles older SQLite via the
create-temp-table-and-copy dance transparently.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_project_context_ownership"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_columns(bind) -> set[str]:
    """Return the column-name set of project_context as it exists now."""
    inspector = sa.inspect(bind)
    return {c["name"] for c in inspector.get_columns("project_context")}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _existing_columns(bind)

    # 1. Rename last_updated -> updated_at if the legacy column is still
    #    present and the new one hasn't been added yet.
    if "last_updated" in cols and "updated_at" not in cols:
        with op.batch_alter_table("project_context") as batch_op:
            batch_op.alter_column("last_updated", new_column_name="updated_at")
        cols = _existing_columns(bind)

    # 2. Add created_at / created_by columns if missing.
    with op.batch_alter_table("project_context") as batch_op:
        if "created_at" not in cols:
            batch_op.add_column(sa.Column("created_at", sa.Text(), nullable=True))
        if "created_by" not in cols:
            batch_op.add_column(sa.Column("created_by", sa.Text(), nullable=True))

    # 3. Backfill: legacy rows have NULL created_at/created_by — seed
    #    them from updated_at/updated_by so the ownership check has
    #    something to compare against.
    op.execute(
        "UPDATE project_context "
        "SET created_at = updated_at "
        "WHERE created_at IS NULL"
    )
    op.execute(
        "UPDATE project_context "
        "SET created_by = updated_by "
        "WHERE created_by IS NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    cols = _existing_columns(bind)

    # Drop the new columns and rename updated_at back to last_updated.
    with op.batch_alter_table("project_context") as batch_op:
        if "created_at" in cols:
            batch_op.drop_column("created_at")
        if "created_by" in cols:
            batch_op.drop_column("created_by")

    cols = _existing_columns(bind)
    if "updated_at" in cols and "last_updated" not in cols:
        with op.batch_alter_table("project_context") as batch_op:
            batch_op.alter_column("updated_at", new_column_name="last_updated")
