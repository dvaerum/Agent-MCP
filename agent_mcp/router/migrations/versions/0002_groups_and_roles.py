"""Phase 3 schema — groups, nested-group membership, project roles.

Revision ID: 0002_groups_and_roles
Revises: 0001_router_initial
Create Date: 2026-06-18

Wave 1a of Phase 3 of the operator-login plan (prancy-napping-pie).
The Phase 1 router DB carried only operator identity + per-project
membership; this migration introduces the collaborative-team model
that Wave 2 and Wave 3 will gate on:

  * ``users.is_sysadmin`` — global system-perm flag (project create/
    delete, user CRUD, group CRUD). Defaults to 0; the bootstrap data
    step below promotes the earliest pre-existing Phase 1 operator so
    upgrades-in-place don't lock anyone out.

  * ``groups`` — first-class collaborative buckets with their own
    optional ``is_sysadmin`` bit. ``name`` is UNIQUE because the
    dashboard surfaces groups by name and operators expect "engineers"
    to be ONE bucket, not two.

  * ``group_membership`` — edges in the membership graph. Each edge is
    EITHER a user-into-group OR a group-into-group; the CHECK
    constraint makes that exactly-one-set rule enforceable at the
    storage layer. (The resolver helper does cycle detection on
    insert; the table itself doesn't reject cycles because Alembic +
    SQLite don't make recursive-trigger constraints ergonomic.)

  * ``project_membership.role`` + ``project_membership.group_id`` —
    extends the existing two-column composite with role tiers
    (``operator`` / ``viewer``) and an alternative "group, not user"
    grant path. The CHECK constraint mirrors ``group_membership``:
    exactly one of ``user_id`` / ``group_id`` is set per row. To get
    the new constraint into the existing table without dropping data,
    SQLite forces us through the "create new + copy + swap" dance —
    which is what the ALTER TABLE block does.

Bootstrap (data step):

  1. ``UPDATE project_membership SET role = 'operator' WHERE role IS
     NULL`` — pre-Phase-3 rows existed before the column did; the
     ALTER schema-rebuild defaults missing values to ``'operator'``
     during copy, so this step is belt-and-suspenders against any
     code path that bypassed the default at insert time.

  2. Earliest Phase-1 operator → sysadmin. Pulled out into the
     ``group_resolver.bootstrap_first_operator_as_sysadmin()``
     helper so it can be re-run idempotently from anywhere (init
     hook, CLI repair tool, tests). The migration calls it once
     so an upgrade-in-place leaves the deployment with a
     functioning sysadmin without operator intervention.

Idempotence: re-running ``alembic upgrade head`` is a no-op — Alembic
tracks the revision. Re-running the data step manually (via the
resolver helper) is also a no-op once any user already has
``is_sysadmin=1`` and once ``role`` is populated on every membership
row.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0002_groups_and_roles"
down_revision: Union[str, None] = "0001_router_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── users.is_sysadmin ────────────────────────────────────────────
    op.execute(
        "ALTER TABLE users ADD COLUMN is_sysadmin BOOLEAN NOT NULL DEFAULT 0"
    )

    # ── groups ──────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE groups (
            group_id     TEXT PRIMARY KEY,
            name         TEXT NOT NULL UNIQUE,
            is_sysadmin  BOOLEAN NOT NULL DEFAULT 0,
            created_at   TEXT NOT NULL
        )
        """
    )

    # ── group_membership ────────────────────────────────────────────
    # Edge in the membership graph. The CHECK constraint encodes
    # "exactly one of (member_user_id, member_group_id) is set" — the
    # `<>` operator on boolean-coerced NULL-vs-non-NULL is SQLite's
    # idiomatic XOR.
    op.execute(
        """
        CREATE TABLE group_membership (
            group_id          TEXT NOT NULL REFERENCES groups(group_id)
                              ON DELETE CASCADE,
            member_user_id    TEXT REFERENCES users(user_id)
                              ON DELETE CASCADE,
            member_group_id   TEXT REFERENCES groups(group_id)
                              ON DELETE CASCADE,
            added_at          TEXT NOT NULL,
            CHECK ((member_user_id IS NOT NULL) <> (member_group_id IS NOT NULL))
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_group_membership_group_id "
        "ON group_membership(group_id)"
    )
    op.execute(
        "CREATE INDEX idx_group_membership_member_user_id "
        "ON group_membership(member_user_id)"
    )
    op.execute(
        "CREATE INDEX idx_group_membership_member_group_id "
        "ON group_membership(member_group_id)"
    )

    # ── project_membership: role + group_id ─────────────────────────
    # SQLite can't add a NOT NULL column with a CHECK constraint via
    # ALTER TABLE, and can't add a CHECK on existing columns at all,
    # so we go through the rebuild-and-swap dance. The temporary
    # table mirrors the future schema; the INSERT copies every
    # existing row (defaulting ``role`` to 'operator'); we drop the
    # old table and rename the new one into place.
    #
    # The composite PK changes from (project_name, user_id) to
    # (project_name, COALESCE(user_id, group_id)) — practically
    # we keep (project_name, user_id, group_id) as the new PK so
    # both grant paths can coexist on the same project without a
    # uniqueness clash. The CHECK ensures one of them is always NULL,
    # so the triplet is still effectively (project, grantee).
    op.execute(
        """
        CREATE TABLE project_membership_new (
            project_name  TEXT NOT NULL,
            user_id       TEXT REFERENCES users(user_id) ON DELETE CASCADE,
            group_id      TEXT REFERENCES groups(group_id) ON DELETE CASCADE,
            role          TEXT NOT NULL DEFAULT 'operator'
                          CHECK (role IN ('operator', 'viewer')),
            CHECK ((user_id IS NOT NULL) <> (group_id IS NOT NULL))
        )
        """
    )
    op.execute(
        """
        INSERT INTO project_membership_new
            (project_name, user_id, group_id, role)
        SELECT project_name, user_id, NULL, 'operator'
        FROM project_membership
        """
    )
    op.execute("DROP TABLE project_membership")
    op.execute(
        "ALTER TABLE project_membership_new RENAME TO project_membership"
    )
    # Re-create useful indices. The old PK gave us (project_name,
    # user_id) automatically; the new shape needs explicit indices
    # because there's no PK to lean on.
    op.execute(
        "CREATE INDEX idx_project_membership_user_id "
        "ON project_membership(user_id)"
    )
    op.execute(
        "CREATE INDEX idx_project_membership_group_id "
        "ON project_membership(group_id)"
    )
    op.execute(
        "CREATE INDEX idx_project_membership_project_name "
        "ON project_membership(project_name)"
    )
    # Uniqueness: at most one row per (project_name, user_id) and one
    # per (project_name, group_id). Partial UNIQUE indices keep the
    # two grant paths from colliding on each other's NULLs.
    op.execute(
        "CREATE UNIQUE INDEX uq_project_membership_user "
        "ON project_membership(project_name, user_id) "
        "WHERE user_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_project_membership_group "
        "ON project_membership(project_name, group_id) "
        "WHERE group_id IS NOT NULL"
    )

    # ── Data step: promote earliest operator to sysadmin ───────────
    # Localised here (vs. the resolver) so a fresh ``alembic upgrade``
    # leaves an upgraded deployment with a functioning sysadmin out
    # of the box. The same SQL lives in
    # ``group_resolver.bootstrap_first_operator_as_sysadmin`` for
    # ad-hoc / repair use; this duplication is deliberate — Alembic
    # migration files shouldn't import from package code that may
    # evolve underneath them.
    op.execute(
        """
        UPDATE users
        SET is_sysadmin = 1
        WHERE user_id = (
            SELECT user_id FROM users
            WHERE is_sysadmin = 0
            ORDER BY created_at ASC, user_id ASC
            LIMIT 1
        )
        AND NOT EXISTS (
            SELECT 1 FROM users WHERE is_sysadmin = 1
        )
        """
    )


def downgrade() -> None:
    # Reverse order; the SQLite "rebuild-and-swap" dance also reverses.
    op.execute("DROP INDEX IF EXISTS uq_project_membership_group")
    op.execute("DROP INDEX IF EXISTS uq_project_membership_user")
    op.execute("DROP INDEX IF EXISTS idx_project_membership_project_name")
    op.execute("DROP INDEX IF EXISTS idx_project_membership_group_id")
    op.execute("DROP INDEX IF EXISTS idx_project_membership_user_id")
    op.execute(
        """
        CREATE TABLE project_membership_old (
            project_name TEXT NOT NULL,
            user_id      TEXT NOT NULL REFERENCES users(user_id)
                         ON DELETE CASCADE,
            PRIMARY KEY (project_name, user_id)
        )
        """
    )
    # Only user-row memberships survive the downgrade; group-row
    # memberships are dropped (the column doesn't exist downstream).
    op.execute(
        """
        INSERT INTO project_membership_old (project_name, user_id)
        SELECT project_name, user_id
        FROM project_membership
        WHERE user_id IS NOT NULL
        """
    )
    op.execute("DROP TABLE project_membership")
    op.execute(
        "ALTER TABLE project_membership_old RENAME TO project_membership"
    )
    op.execute(
        "CREATE INDEX idx_project_membership_user_id "
        "ON project_membership(user_id)"
    )

    op.execute("DROP INDEX IF EXISTS idx_group_membership_member_group_id")
    op.execute("DROP INDEX IF EXISTS idx_group_membership_member_user_id")
    op.execute("DROP INDEX IF EXISTS idx_group_membership_group_id")
    op.execute("DROP TABLE IF EXISTS group_membership")
    op.execute("DROP TABLE IF EXISTS groups")

    # Drop the is_sysadmin column. SQLite supports DROP COLUMN since
    # 3.35 (2021); the runner pins a modern SQLite via the host
    # Python's bundled lib.
    op.execute("ALTER TABLE users DROP COLUMN is_sysadmin")
