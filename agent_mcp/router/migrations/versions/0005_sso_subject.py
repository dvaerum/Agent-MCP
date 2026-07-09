"""SSO account-linking hardening — stable ``users.sso_subject`` key.

Revision ID: 0005_sso_subject
Revises: 0004_group_capability
Create Date: 2026-07-09

Account-linking security fix (see ADR-0015 / the SSO account-linking
findings). SSO-minted rows previously had no stable identifier: the
callback reconciled returning logins purely by ``LOWER(email)``, and
the passwordless / proxy-header path (email always NULL) suffixed the
username (``alice``, ``alice-2``, …) and INSERTed a NEW row on every
request. That leaks unbounded users, re-mints sysadmins under
``AGENT_MCP_SSO_PROXY_DEFAULT_SYSADMIN``, and orphans grants.

This migration adds a single nullable column that lets the SSO layer
key reconciliation on a STABLE SUBJECT rather than the (mutable /
absent) email:

  * OIDC   → ``oidc:<iss>:<sub>``  (the IdP's stable subject id)
  * proxy  → ``proxy:<sanitised-username>`` (the trusted upstream id)

``sso_subject`` is NULL for every password-backed local user and for
legacy SSO rows created before this migration. A partial UNIQUE index
(``WHERE sso_subject IS NOT NULL``) guarantees one subject maps to at
most one row while leaving the many-NULL password users unconstrained
— SQLite honours the partial-index predicate so the NULLs never clash.

Additive + nullable: existing rows are untouched, so this is a
zero-downtime forward migration. The downgrade drops the index +
column (SQLite ``DROP COLUMN`` is supported on the 3.35+ we target;
the router already relies on modern SQLite for the 000x rebuilds).
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0005_sso_subject"
down_revision: Union[str, None] = "0004_group_capability"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN sso_subject TEXT")
    # Partial UNIQUE index: enforce one-row-per-subject only for the
    # rows that actually carry a subject. Password users (subject NULL)
    # are exempt, so the many NULLs never collide.
    op.execute(
        """
        CREATE UNIQUE INDEX idx_users_sso_subject
        ON users(sso_subject)
        WHERE sso_subject IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_users_sso_subject")
    op.execute("ALTER TABLE users DROP COLUMN sso_subject")
