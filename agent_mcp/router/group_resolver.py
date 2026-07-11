"""Group-membership resolution for the Phase 3 collaborative model.

Wave 1a of Phase 3 of the operator-login plan (prancy-napping-pie).
This module owns the *graph* layer on top of the new
``group_membership`` table: insert-time cycle detection, transitive
resolution of which groups a user belongs to, and the
``users.is_sysadmin``-with-inheritance check that downstream
permission gates will consume.

Why a separate module from ``identity.py``? identity owns the flat
CRUD over ``users`` / ``sessions`` / ``project_membership``; this
file owns recursion + invariants. Keeping the two split means the
Phase 2 code paths (which import identity but don't care about
groups) stay light, and the cycle-detection logic has one canonical
home that the dashboard / CLI / tests can all reach for.

Connection ownership (arch-deepening R2 #1a): every resolution
function takes ``conn: sqlite3.Connection | None = None``. Pass
``None`` (the default) to self-open an autocommit connection via
``identity._connect``; pass a caller's OPEN connection to enlist in
that transaction so a handler running inside ``BEGIN IMMEDIATE`` sees
one consistent snapshot without a second connection being opened.
The ``RouterStore`` seam (:mod:`agent_mcp.router.router_store`) is the
OO facade over these functions; the group-rooted variants
(``resolve_group_ancestors``, ``group_is_transitively_sysadmin``,
``group_resolved_project_roles``) replaced the hand-forked traversals
that ``admin_users_api`` used to carry because the resolver owned its
own connection.

Public surface (re-exported via ``__all__``):

  * ``CycleDetected`` — raised by ``add_group_member`` when the new
    edge would close a cycle in the membership DAG.

  * ``add_group_member(...)`` — the canonical writer for
    ``group_membership``.

  * ``resolve_user_groups(user_id, conn=None) -> set[str]`` — every
    group_id the user is in, directly or transitively.

  * ``resolve_user_is_sysadmin(user_id, conn=None) -> bool``.

  * ``resolve_user_project_role(user_id, project_name, conn=None)``.

  * ``resolve_group_ancestors(group_id, conn=None) -> set[str]`` —
    ``group_id`` plus every ancestor group (upward closure).

  * ``group_is_transitively_sysadmin(group_id, conn=None) -> bool`` —
    whether a fresh member of ``group_id`` would inherit sysadmin.

  * ``group_resolved_project_roles(group_id, conn=None) -> dict`` —
    the project roles a fresh member of ``group_id`` would inherit.

  * ``would_create_cycle(parent, child, conn=None) -> bool``.

  * ``bootstrap_first_operator_as_sysadmin()`` — idempotent helper
    that flips the earliest-by-created_at user to ``is_sysadmin = 1``.

  * ``ROLE_TIER`` / ``role_rank`` — the single canonical project-role
    ranking (``operator`` > ``viewer`` > unknown).
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Literal, Optional

from . import identity as _identity


__all__ = [
    "CycleDetected",
    "ROLE_TIER",
    "add_group_member",
    "bootstrap_first_operator_as_sysadmin",
    "ensure_group",
    "group_is_transitively_sysadmin",
    "group_resolved_project_roles",
    "remove_group_member",
    "resolve_group_ancestors",
    "resolve_user_groups",
    "resolve_user_is_sysadmin",
    "resolve_user_project_role",
    "role_rank",
    "user_group_memberships_by_name_prefix",
    "would_create_cycle",
]


logger = logging.getLogger(__name__)


# ── Errors ──────────────────────────────────────────────────────────


class CycleDetected(ValueError):
    """Raised by ``add_group_member`` when the proposed edge would
    close a cycle in the membership DAG.

    Subclasses ``ValueError`` so existing exception handlers that catch
    "bad input" (the dashboard's group-edit form in PR 3b, the CLI
    in PR 3c) get the right semantics without needing to know about a
    bespoke base class.
    """


# ── Project-role ranking (single canonical source) ─────────────────

# viewer < operator; unknown roles rank 0 (below every known role) so a
# malformed row can never out-rank a real membership. This is the ONE
# home for the tier — ``admin_users_api`` and ``router_store`` both read
# it via ``role_rank`` rather than re-declaring the table.
ROLE_TIER: dict[str, int] = {"viewer": 1, "operator": 2}


def role_rank(role: str) -> int:
    """Numeric rank for a project role; unknown roles sort below all."""
    return ROLE_TIER.get(role, 0)


# ── Helpers ────────────────────────────────────────────────────────


def _now_iso() -> str:
    """Match ``identity._now_iso``'s format so all stored timestamps
    on this DB are comparable as strings."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@contextmanager
def _conn_ctx(
    conn: Optional[sqlite3.Connection],
) -> Iterator[sqlite3.Connection]:
    """Yield a usable connection: the caller's if given, else a freshly
    self-opened autocommit one.

    When ``conn`` is provided the caller owns the transaction — we do
    NOT commit or close it, we merely run our SELECTs on it so a handler
    inside ``BEGIN IMMEDIATE`` gets a single consistent snapshot without
    a second connection. When ``conn`` is ``None`` we borrow
    ``identity._connect`` (commit-on-exit, rollback-on-error).
    """
    if conn is not None:
        yield conn
    else:
        with _identity._connect() as owned:
            yield owned


def _children_of_group(
    conn: sqlite3.Connection, group_id: str
) -> list[tuple[Optional[str], Optional[str]]]:
    """Yield (member_user_id, member_group_id) edges out of ``group_id``."""
    cur = conn.execute(
        """
        SELECT member_user_id, member_group_id
        FROM group_membership
        WHERE group_id = ?
        """,
        (group_id,),
    )
    return [(row["member_user_id"], row["member_group_id"]) for row in cur.fetchall()]


# ── Graph kernels (each takes an already-open connection) ───────────


def _ancestors_on(conn: sqlite3.Connection, seed: set[str]) -> set[str]:
    """Upward closure over ``group_membership.member_group_id``.

    Returns ``seed`` PLUS every group reachable by walking upward from
    it (a group that has any seed member as a ``member_group_id``, then
    that group's parents, transitively). Batches each level via ``IN``
    so it costs O(depth) round-trips.
    """
    result: set[str] = set(seed)
    frontier: list[str] = list(seed)
    while frontier:
        placeholders = ",".join("?" for _ in frontier)
        rows = conn.execute(
            f"""
            SELECT DISTINCT group_id FROM group_membership
            WHERE member_group_id IN ({placeholders})
            """,
            tuple(frontier),
        ).fetchall()
        next_frontier: list[str] = []
        for row in rows:
            gid = row["group_id"]
            if gid not in result:
                result.add(gid)
                next_frontier.append(gid)
        frontier = next_frontier
    return result


def _resolve_user_groups_on(conn: sqlite3.Connection, user_id: str) -> set[str]:
    cur = conn.execute(
        "SELECT group_id FROM group_membership WHERE member_user_id = ?",
        (user_id,),
    )
    direct = {row["group_id"] for row in cur.fetchall()}
    if not direct:
        return set()
    return _ancestors_on(conn, direct)


def _resolve_group_ancestors_on(
    conn: sqlite3.Connection, group_id: str
) -> set[str]:
    return _ancestors_on(conn, {group_id})


def _any_group_is_sysadmin_on(
    conn: sqlite3.Connection, groups: set[str]
) -> bool:
    if not groups:
        return False
    placeholders = ",".join("?" for _ in groups)
    cur = conn.execute(
        f"""
        SELECT 1 FROM groups
        WHERE is_sysadmin = 1 AND group_id IN ({placeholders})
        LIMIT 1
        """,
        tuple(groups),
    )
    return cur.fetchone() is not None


def _resolve_user_is_sysadmin_on(
    conn: sqlite3.Connection, user_id: str
) -> bool:
    row = conn.execute(
        "SELECT is_sysadmin FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is not None and row["is_sysadmin"]:
        return True
    return _any_group_is_sysadmin_on(conn, _resolve_user_groups_on(conn, user_id))


def _group_is_transitively_sysadmin_on(
    conn: sqlite3.Connection, group_id: str
) -> bool:
    return _any_group_is_sysadmin_on(
        conn, _resolve_group_ancestors_on(conn, group_id)
    )


def _project_roles_for_groups_on(
    conn: sqlite3.Connection, groups: set[str]
) -> dict[str, str]:
    """Highest role per project across a set of groups (group rows only)."""
    if not groups:
        return {}
    placeholders = ",".join("?" for _ in groups)
    rows = conn.execute(
        f"""
        SELECT project_name, role FROM project_membership
        WHERE group_id IN ({placeholders})
        """,
        tuple(groups),
    ).fetchall()
    best: dict[str, str] = {}
    for row in rows:
        project, role = row["project_name"], row["role"]
        if project not in best or role_rank(role) > role_rank(best[project]):
            best[project] = role
    return best


def _group_resolved_project_roles_on(
    conn: sqlite3.Connection, group_id: str
) -> dict[str, str]:
    return _project_roles_for_groups_on(
        conn, _resolve_group_ancestors_on(conn, group_id)
    )


def _resolve_user_project_role_on(
    conn: sqlite3.Connection, user_id: str, project_name: str
) -> Optional[Literal["operator", "viewer"]]:
    candidates: list[str] = []
    cur = conn.execute(
        "SELECT role FROM project_membership "
        "WHERE project_name = ? AND user_id = ?",
        (project_name, user_id),
    )
    candidates.extend(row["role"] for row in cur.fetchall())
    groups = _resolve_user_groups_on(conn, user_id)
    if groups:
        placeholders = ",".join("?" for _ in groups)
        cur = conn.execute(
            f"""
            SELECT role FROM project_membership
            WHERE project_name = ? AND group_id IN ({placeholders})
            """,
            (project_name, *groups),
        )
        candidates.extend(row["role"] for row in cur.fetchall())
    if not candidates:
        return None
    best = max(candidates, key=role_rank)
    return best  # type: ignore[return-value]


def _would_create_cycle_on(
    conn: sqlite3.Connection, parent_group_id: str, new_child_group_id: str
) -> bool:
    """Detect whether adding ``new_child_group_id`` as a member of
    ``parent_group_id`` would close a cycle.

    A cycle exists iff ``parent_group_id`` is reachable from
    ``new_child_group_id`` via the existing membership edges. Self-loop
    (``new_child_group_id == parent_group_id``) is the trivial 1-cycle.
    Iterative DFS + visited-set to stay off the recursion limit.
    """
    if parent_group_id == new_child_group_id:
        return True
    visited: set[str] = set()
    stack: list[str] = [new_child_group_id]
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        for _user, child_group in _children_of_group(conn, current):
            if child_group is None:
                continue
            if child_group == parent_group_id:
                return True
            if child_group not in visited:
                stack.append(child_group)
    return False


# ── Public: connection-injectable resolution surface ────────────────


def resolve_user_groups(
    user_id: str, conn: Optional[sqlite3.Connection] = None
) -> set[str]:
    """Return the transitive set of group_ids ``user_id`` belongs to.

    A user belongs to a group directly (``member_user_id``) or
    transitively when a direct group is itself nested in a parent group.
    Empty set when the user has no memberships (or doesn't exist).
    """
    with _conn_ctx(conn) as c:
        return _resolve_user_groups_on(c, user_id)


def resolve_group_ancestors(
    group_id: str, conn: Optional[sqlite3.Connection] = None
) -> set[str]:
    """Return ``group_id`` plus every ancestor group (upward closure).

    This is what a fresh member of ``group_id`` would resolve INTO — the
    group-rooted mirror of ``resolve_user_groups`` for a user whose only
    membership is ``group_id``.
    """
    with _conn_ctx(conn) as c:
        return _resolve_group_ancestors_on(c, group_id)


def resolve_user_is_sysadmin(
    user_id: str, conn: Optional[sqlite3.Connection] = None
) -> bool:
    """True iff the user is a sysadmin directly OR via any group in their
    transitive membership."""
    with _conn_ctx(conn) as c:
        return _resolve_user_is_sysadmin_on(c, user_id)


def group_is_transitively_sysadmin(
    group_id: str, conn: Optional[sqlite3.Connection] = None
) -> bool:
    """True iff a fresh member of ``group_id`` would inherit sysadmin —
    i.e. ``group_id`` itself OR any ancestor group is sysadmin-flagged."""
    with _conn_ctx(conn) as c:
        return _group_is_transitively_sysadmin_on(c, group_id)


def resolve_user_project_role(
    user_id: str,
    project_name: str,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[Literal["operator", "viewer"]]:
    """Return the user's effective role for ``project_name`` — the highest
    tier (``operator`` > ``viewer``) across the user's direct rows and any
    of their groups' rows, or ``None`` when no row covers them."""
    with _conn_ctx(conn) as c:
        return _resolve_user_project_role_on(c, user_id, project_name)


def group_resolved_project_roles(
    group_id: str, conn: Optional[sqlite3.Connection] = None
) -> dict[str, str]:
    """Every project role a fresh member of ``group_id`` would inherit —
    the highest tier per project across ``group_id`` and its ancestors."""
    with _conn_ctx(conn) as c:
        return _group_resolved_project_roles_on(c, group_id)


def would_create_cycle(
    parent_group_id: str,
    new_child_group_id: str,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """True iff adding ``new_child_group_id`` as a member of
    ``parent_group_id`` would close a cycle in the membership DAG."""
    with _conn_ctx(conn) as c:
        return _would_create_cycle_on(c, parent_group_id, new_child_group_id)


# ── Public: add_group_member ───────────────────────────────────────


def add_group_member(
    group_id: str,
    member_user_id: Optional[str] = None,
    member_group_id: Optional[str] = None,
    added_at: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Insert a ``group_membership`` row after validation.

    Exactly one of ``member_user_id`` / ``member_group_id`` must be
    non-None — the CHECK constraint on the table enforces this at the
    storage layer but we surface a clean ``ValueError`` here so
    callers don't have to translate sqlite3.IntegrityError.

    For group-into-group edges, runs cycle detection before inserting.
    Raises ``CycleDetected`` (a ``ValueError`` subclass) on the
    parent/child pair that would close the cycle; the table is left
    untouched.

    Connection ownership (arch-deepening R2 #1b): pass ``conn`` to
    enlist in a caller's open ``BEGIN IMMEDIATE`` transaction — the
    cycle check AND the insert then run on that one connection so a
    handler gets a single consistent snapshot without a second
    connection being opened. ``conn=None`` self-opens (commit-on-exit).
    ``sqlite3.IntegrityError`` from the idempotency UNIQUE indices is
    deliberately NOT swallowed — the admin handler maps it to a 409.
    """
    if (member_user_id is None) == (member_group_id is None):
        raise ValueError(
            "add_group_member requires exactly one of "
            "member_user_id or member_group_id"
        )

    ts = added_at or _now_iso()
    with _conn_ctx(conn) as c:
        if member_group_id is not None and _would_create_cycle_on(
            c, group_id, member_group_id
        ):
            raise CycleDetected(
                f"adding group {member_group_id!r} as a member of "
                f"{group_id!r} would close a cycle in the membership DAG"
            )
        c.execute(
            """
            INSERT INTO group_membership
                (group_id, member_user_id, member_group_id, added_at)
            VALUES (?, ?, ?, ?)
            """,
            (group_id, member_user_id, member_group_id, ts),
        )


# ── Public: bootstrap_first_operator_as_sysadmin ───────────────────


def bootstrap_first_operator_as_sysadmin(
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Promote the earliest-by-created_at operator to sysadmin.

    No-op when:
      * the users table is empty (fresh deploy — the first user the
        operator creates via the wizard/CLI gets sysadmin via the
        normal create_user path in a later PR);
      * any user already has ``is_sysadmin = 1`` (idempotent — we
        never demote an existing sysadmin and never crown a second
        one).

    Tiebreaker is ``user_id ASC`` so two users created in the same
    millisecond resolve deterministically.

    The Alembic migration (0002) runs the same SQL during upgrade so
    existing deployments come up with a sysadmin without operator
    intervention. This module-level helper exists for repair
    workflows (CLI subcommand in a future PR, or hand-invocation
    from an operator who somehow ended up sysadmin-less).
    """
    with _conn_ctx(conn) as c:
        existing = c.execute(
            "SELECT 1 FROM users WHERE is_sysadmin = 1 LIMIT 1"
        ).fetchone()
        if existing is not None:
            return
        row = c.execute(
            """
            SELECT user_id FROM users
            ORDER BY created_at ASC, user_id ASC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return
        c.execute(
            "UPDATE users SET is_sysadmin = 1 WHERE user_id = ?",
            (row["user_id"],),
        )
        logger.info(
            "Bootstrapped earliest operator (user_id=%s) as sysadmin.",
            row["user_id"],
        )


# ── Public: SSO group reads/writes (via RouterStore) ───────────────


def ensure_group(
    name: str, conn: Optional[sqlite3.Connection] = None,
) -> Optional[str]:
    """Return the ``group_id`` for ``name``, JIT-creating if missing.

    Connection-injectable home (arch-deepening R2 #1c) for sso.py's
    former inline ``sqlite3.connect`` in ``_ensure_group``. A missing
    ``groups`` table raises ``sqlite3.OperationalError`` (the sso caller
    swallows it to silently skip provisioning on a backlevel deploy).
    """
    with _conn_ctx(conn) as c:
        row = c.execute(
            "SELECT group_id FROM groups WHERE name = ?", (name,),
        ).fetchone()
        if row is not None:
            return row["group_id"]
        group_id = secrets.token_hex(8)
        c.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, "
            "created_at) VALUES (?, ?, 0, datetime('now'))",
            (group_id, name),
        )
        return group_id


def remove_group_member(
    group_id: str, user_id: str, conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """Delete a user→group edge; return True iff a row was removed.

    Connection-injectable home (arch-deepening R2 #1c) for sso.py's
    former inline ``sqlite3.connect`` in ``_remove_user_from_group``.
    """
    with _conn_ctx(conn) as c:
        cur = c.execute(
            "DELETE FROM group_membership WHERE group_id = ? "
            "AND member_user_id = ?",
            (group_id, user_id),
        )
        return cur.rowcount > 0


def user_group_memberships_by_name_prefix(
    user_id: str,
    name_prefix: str,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, str]:
    """Return ``{group_name: group_id}`` for the user's DIRECT memberships
    in groups whose ``name`` starts with ``name_prefix``.

    Connection-injectable home (arch-deepening R2 #1c) for sso.py's
    former inline ``sqlite3.connect`` in ``_user_oidc_group_memberships``
    (the ``oidc:``-namespaced IdP-managed reconcile scope). ``name_prefix``
    is treated literally: the ``LIKE`` pattern is ``name_prefix + '%'``
    with backslash as the escape char, matching the inline query.
    """
    with _conn_ctx(conn) as c:
        cur = c.execute(
            "SELECT g.group_id, g.name FROM group_membership gm "
            "JOIN groups g ON g.group_id = gm.group_id "
            "WHERE gm.member_user_id = ? AND g.name LIKE ? ESCAPE '\\'",
            (user_id, name_prefix + "%"),
        )
        return {row["name"]: row["group_id"] for row in cur.fetchall()}
