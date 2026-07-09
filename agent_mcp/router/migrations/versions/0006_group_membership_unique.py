"""Idempotent group_membership — partial UNIQUE indices per grant path.

Revision ID: 0006_group_membership_unique
Revises: 0005_sso_subject
Create Date: 2026-07-09

Security round-2 pentest-loop finding [LOW-MED]: ``group_membership`` had
only the exactly-one-of CHECK (``0002_groups_and_roles``) and no
uniqueness, and the REST ``add_group_member`` handler did no pre-check —
so a double-submit inserted two rows, double-counting the membership and
duplicating dashboard listings.

This mirrors the ``project_membership`` uniqueness already established in
``0002_groups_and_roles`` (``uq_project_membership_user`` /
``uq_project_membership_group``): partial UNIQUE indices, one per grant
path, so the two member kinds don't collide on each other's NULLs (SQLite
honours the ``WHERE ... IS NOT NULL`` predicate so the many NULLs are
exempt).

De-dupe first: any deployment that already ran the pre-fix handler may
carry duplicate rows, which would make ``CREATE UNIQUE INDEX`` fail.
Keep the earliest edge of each duplicate set (``MIN(rowid)``) — the
``added_at`` timestamp of the first insert is the meaningful one — then
create the indices. SQLite groups NULLs together in ``GROUP BY`` so the
per-path duplicate sets collapse correctly.

Additive + forward-only in effect; the downgrade drops the two indices.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0006_group_membership_unique"
down_revision: Union[str, None] = "0005_sso_subject"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # De-dupe pre-existing rows so the UNIQUE indices below can build.
    op.execute(
        """
        DELETE FROM group_membership
        WHERE rowid NOT IN (
            SELECT MIN(rowid) FROM group_membership
            GROUP BY group_id, member_user_id, member_group_id
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_group_membership_user
        ON group_membership(group_id, member_user_id)
        WHERE member_user_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_group_membership_group
        ON group_membership(group_id, member_group_id)
        WHERE member_group_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_group_membership_group")
    op.execute("DROP INDEX IF EXISTS uq_group_membership_user")
