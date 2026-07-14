"""move config_* rows to the project_settings store (ADR-0016)

Revision ID: 0016_move_config_to_project_settings
Revises: 0015_drop_config_system_token
Create Date: 2026-07-14

Wave 11 PR 0 (prancy-napping-pie): `project_context` was doing double
duty as both the **memory** store (agent-authored, RAG-indexed shared
knowledge) and the **settings** store (operator-only `config_*`
toggles/credentials). The conflation directly caused F009 — the
blanket "any `config_*` key is secret" redaction that broke the
Settings toggles for cookie operators. ADR-0016 splits the stores:

    project_settings(
        context_key TEXT PRIMARY KEY,
        value       TEXT NOT NULL,      -- JSON-encoded, same as context
        description TEXT NULL,
        created_at  TEXT NULL, created_by TEXT NULL,
        updated_at  TEXT NOT NULL, updated_by TEXT NOT NULL
    )

HARD CUTOVER (operator decision 2026-07-14): unlike 0009's
leave-in-place grace period, this migration copies every `config_*`
row into `project_settings` AND deletes it from `project_context` in
the SAME upgrade (one transaction — alembic's default). Leaving the
copies in place would keep the F009 redaction surface alive and let
the two stores drift.

Safety:

* The CREATE TABLE is guarded by `_table_exists` — fresh DBs already
  got the table from `Base.metadata.create_all()` (db/schema.py
  `init_database()` runs before the migration chain).
* The copy skips keys already present in `project_settings` (the
  `NOT IN` guard) so a re-run against a partially-migrated DB never
  clobbers a newer settings row; the DELETE still converges the
  cutover.
* The LIKE pattern escapes the underscore (`'config\\_%' ESCAPE '\\'`)
  so knowledge keys like `configuration_notes` are NOT swept.
* downgrade() is best-effort / DEV-ONLY: it copies rows back to
  `project_context` and drops the table. Production migrations are
  forward-only — rows written to `project_settings` AFTER the upgrade
  are moved back on a best-effort basis, but any concurrent state the
  new store accumulated is not reconciled.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0016_move_config_to_project_settings"
down_revision: Union[str, None] = "0015_drop_config_system_token"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(bind, name: str) -> bool:
    inspector = sa.inspect(bind)
    return name in inspector.get_table_names()


def _create_project_settings(bind) -> None:
    """Guarded CREATE — column set matches the ORM model
    (`db/models/project_settings.py`) exactly so the
    `test_migration_chain_matches_create_all_schema` invariant holds."""
    if _table_exists(bind, "project_settings"):
        return
    bind.execute(
        sa.text(
            "CREATE TABLE project_settings ("
            "context_key TEXT NOT NULL, "
            "value TEXT NOT NULL, "
            "description TEXT, "
            "created_at TEXT, "
            "created_by TEXT, "
            "updated_at TEXT NOT NULL, "
            "updated_by TEXT NOT NULL, "
            "PRIMARY KEY (context_key)"
            ")"
        )
    )


def upgrade() -> None:
    bind = op.get_bind()
    _create_project_settings(bind)

    # Ancient pre-baseline fixture DBs may lack project_context (same
    # defensive branch as 0015) — nothing to move.
    if not _table_exists(bind, "project_context"):
        return

    # Copy, then delete, in the SAME transaction: the hard cutover.
    bind.execute(
        sa.text(
            "INSERT INTO project_settings "
            "(context_key, value, description, created_at, created_by, "
            "updated_at, updated_by) "
            "SELECT context_key, value, description, created_at, "
            "created_by, updated_at, updated_by FROM project_context "
            "WHERE context_key LIKE 'config\\_%' ESCAPE '\\' "
            "AND context_key NOT IN "
            "(SELECT context_key FROM project_settings)"
        )
    )
    bind.execute(
        sa.text(
            "DELETE FROM project_context "
            "WHERE context_key LIKE 'config\\_%' ESCAPE '\\'"
        )
    )


def downgrade() -> None:
    """Best-effort, DEV-ONLY: copy settings rows back into
    project_context and drop the table. See module docstring."""
    bind = op.get_bind()
    if not _table_exists(bind, "project_settings"):
        return
    if _table_exists(bind, "project_context"):
        bind.execute(
            sa.text(
                "INSERT INTO project_context "
                "(context_key, value, description, created_at, created_by, "
                "updated_at, updated_by) "
                "SELECT context_key, value, description, created_at, "
                "created_by, updated_at, updated_by FROM project_settings "
                "WHERE context_key NOT IN "
                "(SELECT context_key FROM project_context)"
            )
        )
    bind.execute(sa.text("DROP TABLE project_settings"))
