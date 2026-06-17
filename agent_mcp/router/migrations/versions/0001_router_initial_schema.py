"""baseline — router identity schema (users, sessions, project_membership)

Revision ID: 0001_router_initial
Revises:
Create Date: 2026-06-17

Initial schema for the router-level identity DB. Three tables:

  * users               — one row per operator (human).
  * sessions            — opaque server-side session tokens.
  * project_membership  — which operator can administer which project.

The shape is verbatim from Phase 1 PR B of the operator-login plan
(prancy-napping-pie). Indices on sessions.user_id and
sessions.expires_at support the per-user session list (used by
session-revocation in Phase 3) and the periodic prune sweep.

Carries `branch_labels=("agent_mcp_router",)` so this Alembic tree
stays distinct from the per-project tree (`branch_labels =
("agent_mcp",)`). Both trees live in the same package but operate
on different SQLite files; the labels are belt-and-suspenders
against an accidental cross-import.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_router_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = ("agent_mcp_router",)
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # users — operator accounts.
    op.execute(
        """
        CREATE TABLE users (
            user_id        TEXT PRIMARY KEY,
            username       TEXT UNIQUE NOT NULL,
            email          TEXT,
            password_hash  TEXT NOT NULL,
            created_at     TEXT NOT NULL,
            last_login_at  TEXT
        )
        """
    )

    # sessions — opaque-cookie session store. last_used_at slides on
    # each successful get; the prune sweep removes rows whose
    # expires_at is in the past.
    op.execute(
        """
        CREATE TABLE sessions (
            session_id     TEXT PRIMARY KEY,
            user_id        TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            created_at     TEXT NOT NULL,
            expires_at     TEXT NOT NULL,
            last_used_at   TEXT NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX idx_sessions_user_id ON sessions(user_id)")
    op.execute("CREATE INDEX idx_sessions_expires_at ON sessions(expires_at)")

    # project_membership — composite PK; one row per (project, user)
    # tuple. project_name is denormalised (no FK) because the canonical
    # project registry lives in projects.local.json, not router.db.
    op.execute(
        """
        CREATE TABLE project_membership (
            project_name   TEXT NOT NULL,
            user_id        TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            PRIMARY KEY (project_name, user_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_project_membership_user_id "
        "ON project_membership(user_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS project_membership")
    op.execute("DROP TABLE IF EXISTS sessions")
    op.execute("DROP TABLE IF EXISTS users")
