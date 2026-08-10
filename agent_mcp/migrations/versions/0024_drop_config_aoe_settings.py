"""purge retired config_aoe_* rows from project_settings

Revision ID: 0024_drop_config_aoe_settings
Revises: 0023_single_root_task_index
Create Date: 2026-08-10

OBS5: the ``aoe_notify`` side-channel feature and its ``config_aoe_*``
per-project settings were removed (superseded by the ADR-0021 delivery
bridge; see the AoE-removal ADR). Nothing reads ``config_aoe_*`` any
more, so any leftover rows in the ``project_settings`` store are dead
weight — this migration deletes them.

The per-agent ``agents.aoe_session_id`` column is a DIFFERENT thing (the
delivery-bridge binding) and is deliberately left untouched.

Safety / semantics:

* Idempotent — a re-run finds no ``config_aoe_*`` rows and is a no-op.
* Table-absent safe — guards for the ``project_settings`` table not
  existing (a DB stamped before 0016 created it).
* Prefix match is INLINED (``context_key LIKE 'config_aoe_%'``) so this
  migration's behaviour is frozen at authoring time.
* downgrade() is a no-op — the deleted values are not recoverable and
  production migrations are forward-only.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0024_drop_config_aoe_settings"
down_revision: Union[str, None] = "0023_single_root_task_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(bind, name: str) -> bool:
    inspector = sa.inspect(bind)
    return name in inspector.get_table_names()


def _purge_config_aoe_settings(bind) -> int:
    """Delete every ``config_aoe_*`` row from ``project_settings``.

    Returns the number of rows deleted (0 when the table is absent or no
    matching rows exist). Split out from :func:`upgrade` so it is
    unit-testable against a plain sqlite connection without an Alembic op
    context.
    """
    if not _table_exists(bind, "project_settings"):
        return 0

    result = bind.execute(
        sa.text(
            "DELETE FROM project_settings "
            "WHERE context_key LIKE 'config_aoe_%'"
        )
    )
    # sqlite reports affected rows via rowcount for DELETE.
    return int(result.rowcount or 0)


def upgrade() -> None:
    bind = op.get_bind()
    deleted = _purge_config_aoe_settings(bind)
    if deleted:
        print(
            f"0024_drop_config_aoe_settings: purged {deleted} "
            "retired config_aoe_* setting row(s)"
        )


def downgrade() -> None:
    # Forward-only: the removed values are not recoverable. No-op.
    pass
