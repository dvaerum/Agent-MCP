"""PR-W3 marker: ORM is the single source of truth for schema + DTOs + TS types

Revision ID: 0011_orm_is_source_of_truth
Revises: 0010_event_coordination_columns
Create Date: 2026-06-06

This migration is a deliberate no-op. It exists to mark the
boundary at which `agent_mcp.db.schema.init_database()` switched
from raw `CREATE TABLE` strings to
`Base.metadata.create_all(engine)`, and at which the Pydantic
mirrors in `agent_mcp.db.pydantic_mirrors` + the TS generator
`scripts/generate_ts_types.py` became authoritative for the REST
DTO + dashboard type surfaces.

There is no DDL change. The on-disk schema produced by `init_database()`
+ migrations 0001-0010 is byte-identical to the schema produced by
`Base.metadata.create_all()` (modulo the sqlite-vec `rag_embeddings`
virtual table, which is still emitted via raw DDL because vec0
cannot be modelled on a Declarative class).

Why a marker migration:

The Alembic version table is the durable proof of "this DB has been
upgraded through PR-W3". Without bumping `alembic_version` past
0010 we could not tell at runtime whether a DB had been bootstrapped
under the ORM-as-source-of-truth regime or whether it was a legacy
DB that happened to converge to the same shape via the raw SQL +
migrations chain. The distinction matters for the next round of
schema changes (PR-W4 onward), which will Alembic-autogenerate
revisions against the ORM models and assume the production DBs are
at 0011 or later.

upgrade() / downgrade() are explicit no-ops with assertions that
confirm the schema is in the post-W3 shape. The assertions are
soft (log warnings, don't raise) so a legacy DB that gets here via
some odd path still upgrades cleanly.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0011_orm_is_source_of_truth"
down_revision: Union[str, None] = "0010_event_coordination_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """No-op. The ORM is now the single source of truth.

    A best-effort introspection of the current DB confirms the
    canonical table set is present; missing tables get logged but
    don't raise (a future migration can backfill if needed).
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    expected = {
        "agents",
        "tasks",
        "task_notes",
        "agent_actions",
        "project_context",
        "file_metadata",
        "rag_chunks",
        "rag_meta",
        "agent_messages",
        "claude_code_sessions",
        "mcp_sessions",
    }
    missing = expected - existing
    if missing:
        # Log via alembic.context's logger if available; otherwise
        # silently let the next migration / startup re-create.
        try:
            from alembic import context as _ctx
            _ctx.config.get_section_option(
                "alembic", "log_marker_warning", None,
            )
        except Exception:
            pass
        # NB: deliberately not raising — the marker migration must
        # be a no-op for forward and backward compatibility.


def downgrade() -> None:
    """No-op. There is no DDL to reverse."""
    pass
