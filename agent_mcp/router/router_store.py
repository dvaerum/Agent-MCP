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
from typing import Literal, Optional

from . import group_resolver as _gr


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

    # ── ranking ─────────────────────────────────────────────────────

    @staticmethod
    def role_rank(role: str) -> int:
        """Single-sourced project-role rank (``operator`` > ``viewer``)."""
        return _gr.role_rank(role)


#: Process-wide singleton — the router has one ``router.db``.
store = RouterStore()
