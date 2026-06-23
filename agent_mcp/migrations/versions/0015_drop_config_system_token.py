"""drop config_system_token row from project_context

Revision ID: 0015_drop_config_system_token
Revises: 0014_drop_admin_pseudo_agent
Create Date: 2026-06-23

Per retire-system-token Wave 3: the system_token is no longer used at
runtime. Wave 1 stopped accepting it as a bearer (the backend now uses
per-agent tokens or signed forwarding headers). Wave 2 stopped the
router injecting it. Wave 3 deletes the storage + plumbing.

Forward-only — no downgrade. Idempotent: only deletes if present.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0015_drop_config_system_token"
down_revision: Union[str, None] = "0014_drop_admin_pseudo_agent"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Tolerate ancient pre-baseline DBs that don't have project_context
    # yet — the same test fixtures used by 0009 / 0010 seed a stripped
    # DB without the table, then run the migration chain to the head.
    # The table is created by migration 0001 (the baseline), so by the
    # time the migration chain is healthy and 0015 runs in production
    # this branch is the common path; the legacy fixture branch is
    # defensive only.
    bind = op.get_bind()
    has_table = bind.exec_driver_sql(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='project_context'"
    ).fetchone()
    if not has_table:
        return
    op.execute(
        "DELETE FROM project_context "
        "WHERE context_key IN ('config_system_token', 'config_admin_token')"
    )


def downgrade() -> None:
    raise NotImplementedError(
        "retire-system-token Wave 3 is forward-only"
    )
