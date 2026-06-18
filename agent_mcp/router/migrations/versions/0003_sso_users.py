"""Phase 3 Wave 3 schema — relax users.password_hash for SSO-only users.

Revision ID: 0003_sso_users
Revises: 0002_groups_and_roles
Create Date: 2026-06-18

Wave 3 of Phase 3 of the operator-login plan (prancy-napping-pie) adds
two SSO front-ends (OIDC + proxy-header trust). Both JIT-create local
``users`` rows for first-time SSO logins. SSO users have no
password — the IdP (or upstream proxy) owns the credential — so the
``users.password_hash`` column has to admit NULL.

The original Phase 1 schema declared the column ``NOT NULL`` because
every user was created via the username/password form. This migration
performs the SQLite ``CREATE NEW + COPY + SWAP`` dance (the same one
used in 0002) to relax the constraint without dropping data.

Idempotence: re-running ``alembic upgrade head`` is a no-op (Alembic
tracks the revision). The data copy preserves every existing row's
password_hash verbatim — SSO-only users land with NULL going forward,
existing username/password users keep their argon2 hash.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0003_sso_users"
down_revision: Union[str, None] = "0002_groups_and_roles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite can't relax NOT NULL via ALTER TABLE. Rebuild + swap.
    #
    # We preserve everything else (PK, UNIQUE on username, defaults
    # for ``is_sysadmin``, the optional ``email`` column added in
    # 0001). Only ``password_hash`` changes: NOT NULL → nullable.
    op.execute(
        """
        CREATE TABLE users_new (
            user_id       TEXT PRIMARY KEY,
            username      TEXT UNIQUE NOT NULL,
            email         TEXT,
            password_hash TEXT,                       -- nullable now
            created_at    TEXT NOT NULL,
            last_login_at TEXT,
            is_sysadmin   BOOLEAN NOT NULL DEFAULT 0
        )
        """
    )
    op.execute(
        """
        INSERT INTO users_new
            (user_id, username, email, password_hash, created_at,
             last_login_at, is_sysadmin)
        SELECT user_id, username, email, password_hash, created_at,
               last_login_at, is_sysadmin
        FROM users
        """
    )
    # Drop the old table; rename the new one into place. The FK
    # references from sessions / group_membership / project_membership
    # point at ``users(user_id)`` by NAME, not by ROWID, so the swap
    # is invisible to downstream tables provided we preserve user_id
    # values verbatim (which the SELECT above does).
    op.execute("DROP TABLE users")
    op.execute("ALTER TABLE users_new RENAME TO users")


def downgrade() -> None:
    # Re-introduce the NOT NULL constraint. Any SSO-only user without
    # a password_hash gets a placeholder argon2-shaped value so the
    # constraint can be satisfied — but the value is intentionally
    # unguessable so it can't be used to log in via the password form
    # if the downgrade is ever reverted.
    op.execute(
        """
        UPDATE users
        SET password_hash = '$argon2id$v=19$m=65536,t=2,p=4$'
                            || hex(randomblob(16))
                            || '$' || hex(randomblob(32))
        WHERE password_hash IS NULL
        """
    )
    op.execute(
        """
        CREATE TABLE users_old (
            user_id       TEXT PRIMARY KEY,
            username      TEXT UNIQUE NOT NULL,
            email         TEXT,
            password_hash TEXT NOT NULL,
            created_at    TEXT NOT NULL,
            last_login_at TEXT,
            is_sysadmin   BOOLEAN NOT NULL DEFAULT 0
        )
        """
    )
    op.execute(
        """
        INSERT INTO users_old
            (user_id, username, email, password_hash, created_at,
             last_login_at, is_sysadmin)
        SELECT user_id, username, email, password_hash, created_at,
               last_login_at, is_sysadmin
        FROM users
        """
    )
    op.execute("DROP TABLE users")
    op.execute("ALTER TABLE users_old RENAME TO users")
