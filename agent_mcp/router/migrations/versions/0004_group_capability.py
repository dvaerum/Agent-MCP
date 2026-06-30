"""Wave 9 PR 0 schema — group_capability mapping table.

Revision ID: 0004_group_capability
Revises: 0003_sso_users
Create Date: 2026-06-30

Wave 9 PR 0 of 7 in ``prancy-napping-pie.md`` ships the capability-
based authorisation foundation. This migration adds the single new
DB table the system needs: a ``(group_id, capability)`` mapping that
sysadmins populate via the Wave 9 PR 5 dashboard UI to grant fine-
grained capabilities to a group.

Table shape::

    CREATE TABLE group_capability (
        group_id   TEXT NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
        capability TEXT NOT NULL,
        PRIMARY KEY (group_id, capability)
    );

* ``group_id`` is the same FK target ``group_membership`` uses
  (``groups(group_id)``). ``ON DELETE CASCADE`` keeps the table
  consistent when a sysadmin deletes a group — its capability grants
  go with it.
* ``capability`` is a free-form text column rather than an ENUM
  because the cap vocabulary lives in code
  (``agent_mcp/core/capabilities.py::KNOWN_CAPABILITIES``). Storing
  unknown strings is harmless — :meth:`Principal.has_capability`
  returns False for any cap not in the in-memory set, and the
  dashboard UI will only present the locked KNOWN set — so DB-level
  enum-style enforcement would just block the cap vocabulary from
  evolving across deploys without a fresh migration round-trip.
* The composite primary key (group_id, capability) doubles as the
  index for the hot read path
  (``group_capability_repository.fetch(group_id) -> frozenset[str]``)
  — every fetch is a range scan keyed on the leading column, so no
  secondary index is needed.

Seed data: NONE. The table starts empty for every deploy. The
resolution chain (``core.capabilities.resolve_capabilities``) falls
back to ``PROJECT_ROLE_BUNDLES[project_role]`` when no group caps
match, so existing deploys preserve their baseline behaviour with
zero rows; group caps are purely additive.

Idempotence: re-running ``alembic upgrade head`` is a no-op (Alembic
tracks the revision). The ``CREATE TABLE`` is bare (no IF NOT
EXISTS) because Alembic's revision tracking is the canonical
idempotence layer — a bare CREATE catches "two revisions tried to
create the same table" as a real error instead of silently swallowing
it. Tests / cold-start paths get the table by running the migration
runner end-to-end.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0004_group_capability"
down_revision: Union[str, None] = "0003_sso_users"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE group_capability (
            group_id   TEXT NOT NULL REFERENCES groups(group_id)
                       ON DELETE CASCADE,
            capability TEXT NOT NULL,
            PRIMARY KEY (group_id, capability)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS group_capability")
