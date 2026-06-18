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

Public surface (re-exported via ``__all__``):

  * ``CycleDetected`` — raised by ``add_group_member`` when the new
    edge would close a cycle in the membership DAG.

  * ``add_group_member(group_id, member_user_id=None,
    member_group_id=None, added_at=None)`` — the canonical writer
    for ``group_membership``. Validates exactly-one-of, runs cycle
    detection from the proposed edge outward (DFS until we either
    revisit ``group_id`` or exhaust the reachable set), then inserts.

  * ``resolve_user_groups(user_id) -> set[str]`` — every group_id the
    user is in, directly or transitively via nested groups.

  * ``resolve_user_is_sysadmin(user_id) -> bool`` — true if
    ``users.is_sysadmin`` OR any group in the resolved set has
    ``is_sysadmin = 1``.

  * ``resolve_user_project_role(user_id, project_name) ->
    Optional[Literal['operator', 'viewer']]`` — walks
    ``project_membership`` rows for the user OR any of their groups,
    returning the highest-tier match (operator > viewer) or ``None``
    when no row covers the user.

  * ``bootstrap_first_operator_as_sysadmin()`` — idempotent helper
    that flips the earliest-by-created_at user to
    ``is_sysadmin = 1`` when no sysadmin exists yet. Re-runnable
    from anywhere (init hook, repair CLI, tests); the migration
    calls it once during upgrade.

All public callers go through ``identity._connect()`` for the DB
handle, sharing FK enforcement + commit/rollback contract.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable, Literal, Optional

from . import identity as _identity


__all__ = [
    "CycleDetected",
    "add_group_member",
    "bootstrap_first_operator_as_sysadmin",
    "resolve_user_groups",
    "resolve_user_is_sysadmin",
    "resolve_user_project_role",
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


# ── Helpers ────────────────────────────────────────────────────────


def _now_iso() -> str:
    """Match ``identity._now_iso``'s format so all stored timestamps
    on this DB are comparable as strings."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _children_of_group(conn, group_id: str) -> list[tuple[Optional[str], Optional[str]]]:
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


def _would_create_cycle(
    conn, parent_group_id: str, new_child_group_id: str
) -> bool:
    """Detect whether adding ``new_child_group_id`` as a member of
    ``parent_group_id`` would close a cycle.

    A cycle exists iff ``parent_group_id`` is reachable from
    ``new_child_group_id`` via the existing membership edges (because
    once we add ``new_child_group_id ∈ parent_group_id``, traversing
    from ``new_child_group_id`` would now loop back to itself through
    its newly-acquired parent).

    Self-loop (``new_child_group_id == parent_group_id``) is the
    trivial 1-cycle and is handled by the same reachability check.
    """
    if parent_group_id == new_child_group_id:
        return True

    # DFS from the proposed child, looking for an existing path back
    # to the proposed parent. Iterative + visited-set so we don't blow
    # the Python recursion limit on adversarial inputs.
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


# ── Public: add_group_member ───────────────────────────────────────


def add_group_member(
    group_id: str,
    member_user_id: Optional[str] = None,
    member_group_id: Optional[str] = None,
    added_at: Optional[str] = None,
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
    """
    if (member_user_id is None) == (member_group_id is None):
        raise ValueError(
            "add_group_member requires exactly one of "
            "member_user_id or member_group_id"
        )

    if member_group_id is not None:
        with _identity._connect() as conn:
            if _would_create_cycle(conn, group_id, member_group_id):
                raise CycleDetected(
                    f"adding group {member_group_id!r} as a member of "
                    f"{group_id!r} would close a cycle in the membership DAG"
                )

    ts = added_at or _now_iso()
    with _identity._connect() as conn:
        conn.execute(
            """
            INSERT INTO group_membership
                (group_id, member_user_id, member_group_id, added_at)
            VALUES (?, ?, ?, ?)
            """,
            (group_id, member_user_id, member_group_id, ts),
        )


# ── Public: resolve_user_groups ─────────────────────────────────────


def resolve_user_groups(user_id: str) -> set[str]:
    """Return the transitive set of group_ids ``user_id`` belongs to.

    A user belongs to a group directly if there's a
    ``group_membership`` row with ``member_user_id = user_id``. A user
    belongs to a parent group transitively if any direct group is
    itself a member of the parent (recursive).

    Implementation: collect direct groups, then BFS upward over
    ``group_membership.member_group_id`` to find every ancestor group.
    Empty set when the user has no memberships (or doesn't exist).
    """
    result: set[str] = set()
    with _identity._connect() as conn:
        cur = conn.execute(
            """
            SELECT group_id FROM group_membership
            WHERE member_user_id = ?
            """,
            (user_id,),
        )
        frontier: list[str] = [row["group_id"] for row in cur.fetchall()]
        result.update(frontier)
        while frontier:
            next_frontier: list[str] = []
            # Find every group that has any of the current frontier
            # as a member (member_group_id in (...)). Batch via IN to
            # keep this O(depth) round-trips instead of O(n).
            placeholders = ",".join("?" for _ in frontier)
            cur = conn.execute(
                f"""
                SELECT DISTINCT group_id FROM group_membership
                WHERE member_group_id IN ({placeholders})
                """,
                tuple(frontier),
            )
            for row in cur.fetchall():
                gid = row["group_id"]
                if gid not in result:
                    result.add(gid)
                    next_frontier.append(gid)
            frontier = next_frontier
    return result


# ── Public: resolve_user_is_sysadmin ───────────────────────────────


def resolve_user_is_sysadmin(user_id: str) -> bool:
    """Return True iff the user is a sysadmin directly OR via any
    group in their transitive membership."""
    user = _identity.get_user_by_id(user_id)
    if user is not None and user.get("is_sysadmin"):
        return True
    groups = resolve_user_groups(user_id)
    if not groups:
        return False
    placeholders = ",".join("?" for _ in groups)
    with _identity._connect() as conn:
        cur = conn.execute(
            f"""
            SELECT 1 FROM groups
            WHERE is_sysadmin = 1 AND group_id IN ({placeholders})
            LIMIT 1
            """,
            tuple(groups),
        )
        return cur.fetchone() is not None


# ── Public: resolve_user_project_role ──────────────────────────────


_ROLE_TIER: dict[str, int] = {"viewer": 1, "operator": 2}


def resolve_user_project_role(
    user_id: str, project_name: str
) -> Optional[Literal["operator", "viewer"]]:
    """Return the user's effective role for ``project_name``.

    Walks every ``project_membership`` row matching ``user_id``
    directly OR any group in ``resolve_user_groups(user_id)``. When
    multiple rows match, returns the highest tier (``operator``
    outranks ``viewer``). Returns ``None`` when no row covers the
    user — caller treats that as "no access".
    """
    candidates: list[str] = []
    groups = resolve_user_groups(user_id)
    with _identity._connect() as conn:
        cur = conn.execute(
            "SELECT role FROM project_membership "
            "WHERE project_name = ? AND user_id = ?",
            (project_name, user_id),
        )
        candidates.extend(row["role"] for row in cur.fetchall())
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
    # max by tier; unknown tiers (shouldn't happen given the CHECK)
    # sort below known ones so they don't accidentally win.
    best = max(candidates, key=lambda r: _ROLE_TIER.get(r, 0))
    return best  # type: ignore[return-value]


# ── Public: bootstrap_first_operator_as_sysadmin ───────────────────


def bootstrap_first_operator_as_sysadmin() -> None:
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
    with _identity._connect() as conn:
        existing = conn.execute(
            "SELECT 1 FROM users WHERE is_sysadmin = 1 LIMIT 1"
        ).fetchone()
        if existing is not None:
            return
        row = conn.execute(
            """
            SELECT user_id FROM users
            ORDER BY created_at ASC, user_id ASC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return
        conn.execute(
            "UPDATE users SET is_sysadmin = 1 WHERE user_id = ?",
            (row["user_id"],),
        )
        logger.info(
            "Bootstrapped earliest operator (user_id=%s) as sysadmin.",
            row["user_id"],
        )


# Silence unused-import warnings from static analyzers — we re-export
# nothing from ``identity`` but importing it primes the migrations
# runner so ``_identity._connect`` always sees an up-to-date schema.
_ = Iterable
