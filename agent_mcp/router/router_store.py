"""RouterStore — the router-side repository seam (arch-deepening R2 #1a).

The router persists users / groups / membership / sessions in
``router.db`` but historically had NO repository object: three
incompatible connection helpers (``identity._connect`` autocommit,
``admin_users_api._connect`` raw ``BEGIN IMMEDIATE``, and inline
``sqlite3.connect`` calls in ``sso``) and, because ``group_resolver``
owned its own connection, ``admin_users_api`` hand-FORKED the group
graph traversal so a handler inside ``BEGIN IMMEDIATE`` could run it on
its own connection without breaking snapshot isolation.

``RouterStore`` is that missing seam. Every group-resolution method
takes ``conn: sqlite3.Connection | None = None``:

  * ``conn=None`` (default) — self-open an autocommit connection.
  * ``conn=<open connection>`` — enlist in the caller's transaction;
    the store runs its queries on THAT connection and never opens a
    second one, so a handler holding a ``BEGIN IMMEDIATE`` transaction
    gets one consistent snapshot.

The implementation lives in :mod:`agent_mcp.router.group_resolver`
(the deepest existing piece — it already owned the graph kernels); the
store is the connection-injectable facade the handlers call. This is
the lower-risk of the two shapes the brief allowed (delegate vs. move)
because the resolver's module functions stay monkeypatch- and
reload-compatible for the existing test suite.

Follow-up slices (#1b/#1c) grow this seam to own membership inserts,
``create_user(password_hash=None)``, the first-user bootstrap, and the
empty-probe / inline-connect collapses.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Literal, Optional

from . import group_resolver as _gr
from . import identity as _identity


class RouterStore:
    """Connection-injectable facade over router group/membership reads.

    Delegates to :mod:`agent_mcp.router.group_resolver` so there is ONE
    graph traversal. The group-rooted methods
    (``resolve_group_ancestors``, ``group_is_transitively_sysadmin``,
    ``group_resolved_project_roles``) are the canonical replacements for
    the traversals ``admin_users_api`` used to fork.
    """

    # ── user-rooted resolution ──────────────────────────────────────

    def resolve_user_groups(
        self, user_id: str, *, conn: Optional[sqlite3.Connection] = None
    ) -> set[str]:
        return _gr.resolve_user_groups(user_id, conn=conn)

    def resolve_user_is_sysadmin(
        self, user_id: str, *, conn: Optional[sqlite3.Connection] = None
    ) -> bool:
        return _gr.resolve_user_is_sysadmin(user_id, conn=conn)

    def resolve_user_project_role(
        self,
        user_id: str,
        project_name: str,
        *,
        conn: Optional[sqlite3.Connection] = None,
    ) -> Optional[Literal["operator", "viewer"]]:
        return _gr.resolve_user_project_role(user_id, project_name, conn=conn)

    # ── group-rooted resolution (was forked in admin_users_api) ─────

    def resolve_group_ancestors(
        self, group_id: str, *, conn: Optional[sqlite3.Connection] = None
    ) -> set[str]:
        return _gr.resolve_group_ancestors(group_id, conn=conn)

    def group_is_transitively_sysadmin(
        self, group_id: str, *, conn: Optional[sqlite3.Connection] = None
    ) -> bool:
        return _gr.group_is_transitively_sysadmin(group_id, conn=conn)

    def group_resolved_project_roles(
        self, group_id: str, *, conn: Optional[sqlite3.Connection] = None
    ) -> dict[str, str]:
        return _gr.group_resolved_project_roles(group_id, conn=conn)

    # ── membership-DAG invariant ────────────────────────────────────

    def would_create_cycle(
        self,
        parent_group_id: str,
        new_child_group_id: str,
        *,
        conn: Optional[sqlite3.Connection] = None,
    ) -> bool:
        return _gr.would_create_cycle(
            parent_group_id, new_child_group_id, conn=conn
        )

    # ── membership writes (was forked ×4 each) ──────────────────────

    def add_group_member(
        self,
        group_id: str,
        *,
        member_user_id: Optional[str] = None,
        member_group_id: Optional[str] = None,
        added_at: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        """Insert a ``group_membership`` edge — the single writer the
        four inline INSERTs (``group_resolver``, ``admin_users_api``,
        ``sso._add_user_to_group_idempotent``) route through.

        Exactly one of ``member_user_id`` / ``member_group_id``; group
        edges run cycle detection first (``CycleDetected`` on a closing
        edge). ``conn`` enlists in the caller's ``BEGIN IMMEDIATE`` so
        the cycle check and the insert share one snapshot;
        ``sqlite3.IntegrityError`` from the idempotency UNIQUE indices
        propagates so the admin handler can map it to a 409.
        """
        _gr.add_group_member(
            group_id,
            member_user_id=member_user_id,
            member_group_id=member_group_id,
            added_at=added_at,
            conn=conn,
        )

    def add_project_membership(
        self,
        project_name: str,
        *,
        user_id: Optional[str] = None,
        group_id: Optional[str] = None,
        role: Optional[str] = None,
        or_ignore: bool = False,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        """Insert a ``project_membership`` row — the single writer the
        four inline INSERTs (``identity`` grant + first-user loop, the
        admin user- and group-grant handlers, the SSO first-user loop)
        route through.

        Exactly one of ``user_id`` / ``group_id``; ``role=None`` lets the
        DB default (``operator``) apply; ``or_ignore`` selects the
        idempotent ``INSERT OR IGNORE`` vs a plain ``INSERT`` whose
        ``IntegrityError`` the admin handler maps to a 409. ``conn``
        enlists in the caller's open transaction.
        """
        _identity.insert_project_membership(
            project_name,
            user_id=user_id,
            group_id=group_id,
            role=role,
            or_ignore=or_ignore,
            conn=conn,
        )

    # ── connection factory (3 helpers → 1) ─────────────────────────

    def connect(self) -> sqlite3.Connection:
        """Hand out a RAW router.db connection (row_factory + FK on);
        the caller owns commit + close.

        The transactional-mode factory (arch-deepening R2 #1c): the
        admin handlers that drive an explicit ``BEGIN IMMEDIATE`` call
        this (``admin_users_api._connect`` now delegates here) so their
        multi-statement transaction runs on one connection they manage.
        Built on the single :func:`identity.open_connection` factory the
        three drifted connection helpers collapsed into.
        """
        return _identity.open_connection()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a self-opened autocommit-on-exit connection (the
        counterpart to :meth:`connect`, wrapping ``identity._connect``).

        Commits on clean exit, rolls back on exception, always closes —
        the "self-open" mode of the one connection factory.
        """
        with _identity._connect() as c:
            yield c

    # ── empty-table probe (5 helpers → 1) ──────────────────────────

    def users_table_is_empty(
        self, *, conn: Optional[sqlite3.Connection] = None
    ) -> bool:
        """True iff the ``users`` table has no rows (or doesn't exist).

        The single empty-table probe the five drifted copies route
        through. Pass ``conn`` to read inside a caller's transaction.
        """
        return _identity.users_table_is_empty(conn=conn)

    # ── first-operator bootstrap (3 sites → 1) ─────────────────────

    def bootstrap_first_operator(
        self,
        user_id: str,
        *,
        grant_sysadmin: bool,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        """Apply the first-operator invariant to ``user_id``: sysadmin
        (when ``grant_sysadmin``) + membership in every registered
        project. THE single owner of *"first user on an empty table →
        sysadmin + all-project membership"* — ``create_user`` calls this
        inside its INSERT transaction via ``conn``.
        """
        _identity.bootstrap_first_operator(
            user_id, grant_sysadmin=grant_sysadmin, conn=conn,
        )

    def bootstrap_first_operator_as_sysadmin(
        self, *, conn: Optional[sqlite3.Connection] = None
    ) -> None:
        """Idempotent repair: promote the earliest operator to sysadmin
        when the table has users but none is sysadmin (the
        ``init_router_db`` re-run). No-op on an empty table or when a
        sysadmin already exists."""
        _gr.bootstrap_first_operator_as_sysadmin(conn=conn)

    # ── SSO user reconciliation reads/writes (sso inline connects) ──

    def find_user_by_sso_subject(
        self, subject: str, *, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[dict[str, Any]]:
        """Stable-subject reconciliation lookup (was
        ``sso._find_user_by_subject``'s inline connect)."""
        return _identity.find_user_by_sso_subject(subject, conn=conn)

    def find_linkable_user_by_email(
        self, email: str, *, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[dict[str, Any]]:
        """Verified-email link target lookup (was
        ``sso._find_linkable_user_by_email``'s inline connect)."""
        return _identity.find_linkable_user_by_email(email, conn=conn)

    def stamp_sso_subject_if_absent(
        self,
        user_id: str,
        subject: str,
        *,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        """Bind ``subject`` to ``user_id`` iff unset (was
        ``sso._stamp_subject_if_absent``'s inline connect)."""
        _identity.stamp_sso_subject_if_absent(user_id, subject, conn=conn)

    def upgrade_sso_subject(
        self,
        user_id: str,
        old_subject: str,
        new_subject: str,
        *,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        """Re-stamp a legacy-format subject to the current tagged
        format on a fallback-lookup hit (R19-F1 self-heal; was
        ``sso._upgrade_subject``'s inline connect)."""
        _identity.upgrade_sso_subject(user_id, old_subject, new_subject, conn=conn)

    # ── SSO group reads/writes (sso inline connects) ───────────────

    def ensure_group(
        self, name: str, *, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[str]:
        """Return ``group_id`` for ``name``, JIT-creating if missing (was
        ``sso._ensure_group``'s inline connect)."""
        return _gr.ensure_group(name, conn=conn)

    def remove_group_member(
        self,
        group_id: str,
        user_id: str,
        *,
        conn: Optional[sqlite3.Connection] = None,
    ) -> bool:
        """Delete a user→group edge; True iff a row went (was
        ``sso._remove_user_from_group``'s inline connect)."""
        return _gr.remove_group_member(group_id, user_id, conn=conn)

    def user_group_memberships_by_name_prefix(
        self,
        user_id: str,
        name_prefix: str,
        *,
        conn: Optional[sqlite3.Connection] = None,
    ) -> dict[str, str]:
        """``{group_name: group_id}`` for the user's DIRECT memberships in
        groups whose name starts with ``name_prefix`` (was
        ``sso._user_oidc_group_memberships``'s inline connect)."""
        return _gr.user_group_memberships_by_name_prefix(
            user_id, name_prefix, conn=conn,
        )

    # ── ranking ─────────────────────────────────────────────────────

    @staticmethod
    def role_rank(role: str) -> int:
        """Single-sourced project-role rank (``operator`` > ``viewer``)."""
        return _gr.role_rank(role)


#: Process-wide singleton — the router has one ``router.db``.
store = RouterStore()
