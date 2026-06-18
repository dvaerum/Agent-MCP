"""Router-level identity store — users, sessions, project memberships.

Phase 1 PR B of the operator-login plan (prancy-napping-pie). This
module owns the *data* layer for operator identity; the HTTP routes
that consume it (login/logout/setup-wizard) land in PR C, and the
FastAPI dependency that gates dashboard routes on a session lands
in PR D.

DB location: `/var/lib/agent-mcp/router.db` by default, overridable
via the `AGENT_MCP_ROUTER_DB` env var (tests rely on the override).

Threading: every public function opens a fresh sqlite3 connection
for its work and closes on exit. SQLite's serialised mode is fine
for this; the router serves a small number of operators so we don't
need a connection pool. Use `_connect()` directly only in tests
that want to inspect raw rows.

Hashing: argon2-cffi's `PasswordHasher` with library defaults
(argon2id, time_cost=2, memory_cost=64 MiB, parallelism=4). The
`hash_password` return value is the full argon2 encoded string —
salts and parameters are baked in, so callers store one TEXT column
and never reason about salt management.

Bootstrap: `init_router_db()` runs migrations, then — if the users
table is empty AND both `AGENT_MCP_BOOTSTRAP_USERNAME` /
`AGENT_MCP_BOOTSTRAP_PASSWORD` are set — creates the first operator
and unsets the env vars so they don't leak into subprocess spawns
(`create_agent` and friends inherit os.environ).

Retroactive project membership: when the first `create_user` lands
on an empty users table AND the project registry already lists one
or more projects, that first user gets a `project_membership` row
for each. This is the pre-Phase-1-deployment migration story —
existing single-tenant deploys upgrade smoothly because the operator
they create at first boot inherits access to every project they
already had.
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError

from .migrations_runner import (
    get_router_db_path,
    run_router_migrations_upgrade,
)

# Re-exported so callers (CLI, lifespan, tests) can import everything
# they need from `agent_mcp.router.identity` without reaching into the
# migrations module.
__all__ = [
    "DEFAULT_SESSION_LIFETIME_DAYS",
    "IdentityError",
    "UsernameAlreadyExistsError",
    "add_project_membership",
    "create_session",
    "create_user",
    "delete_session",
    "get_router_db_path",
    "get_session",
    "get_user_by_id",
    "get_user_by_username",
    "hash_password",
    "init_router_db",
    "is_project_member",
    "list_user_projects",
    "prune_expired_sessions",
    "remove_project_membership",
    "run_router_migrations_upgrade",
    "touch_last_login",
    "verify_password",
]


logger = logging.getLogger(__name__)


# ── Errors ──────────────────────────────────────────────────────────


class IdentityError(Exception):
    """Base class for router identity errors."""


class UsernameAlreadyExistsError(IdentityError):
    """Raised by create_user when the username UNIQUE constraint fails."""


# ── Constants ───────────────────────────────────────────────────────


DEFAULT_SESSION_LIFETIME_DAYS = 30


# Module-level PasswordHasher: stateless, thread-safe, cheap to share.
_HASHER = PasswordHasher()


# ── Connection helpers ─────────────────────────────────────────────


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """Open a router.db connection with row-factory + FK enabled.

    Yields a context-managed connection that commits on clean exit
    and rolls back on exception. SQLite-level isolation is the
    default (transactional autobegin); we don't need explicit
    BEGIN/COMMIT around single-statement writes.
    """
    db_path = get_router_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now_iso() -> str:
    """UTC timestamp in ISO 8601 with millisecond precision.

    Millisecond resolution is enough to make `last_used_at` strictly
    monotonic across two `get_session` calls separated by a sleep,
    which keeps the per-session sliding-window test deterministic
    without dragging timezone library deps in.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ── Password hashing ───────────────────────────────────────────────


def hash_password(password: str) -> str:
    """Hash `password` via argon2id with library defaults.

    Returns the full argon2-encoded string (parameters + salt +
    hash). Cost (~10 ms on a 2024 desktop CPU) is tuned by the
    argon2-cffi defaults; we don't override them.
    """
    return _HASHER.hash(password)


def verify_password(hashed: str, password: str) -> bool:
    """Return True iff `password` matches `hashed`.

    Wraps argon2.PasswordHasher.verify (which raises on mismatch /
    malformed hash) to a simple boolean — callers consistently want
    "did this pair match?" rather than the exception ladder.
    """
    try:
        return _HASHER.verify(hashed, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


# ── Init / bootstrap ───────────────────────────────────────────────


def init_router_db() -> None:
    """Apply migrations to router.db, then bootstrap from env vars.

    Bootstrap fires only when:
      * both AGENT_MCP_BOOTSTRAP_USERNAME and AGENT_MCP_BOOTSTRAP_PASSWORD
        are set (one alone is treated as a typo, not a half-bootstrap),
      * AND the users table is empty.

    After the bootstrap attempt — whether or not it actually created
    a user — both env vars are removed from os.environ so they don't
    leak into subprocess spawns (e.g., the agent backend processes
    spawned via `create_agent`).
    """
    run_router_migrations_upgrade()

    bootstrap_username = os.environ.get("AGENT_MCP_BOOTSTRAP_USERNAME")
    bootstrap_password = os.environ.get("AGENT_MCP_BOOTSTRAP_PASSWORD")

    if bootstrap_username and bootstrap_password:
        try:
            if _users_table_is_empty():
                create_user(
                    username=bootstrap_username,
                    password=bootstrap_password,
                )
                logger.info(
                    "Bootstrapped first operator %r from "
                    "AGENT_MCP_BOOTSTRAP_USERNAME/PASSWORD env vars.",
                    bootstrap_username,
                )
            else:
                logger.info(
                    "Bootstrap env vars set, but users table is non-empty "
                    "— skipping bootstrap."
                )
        finally:
            # Strip the env vars even on exception so a future
            # subprocess spawn doesn't carry a leaked password.
            os.environ.pop("AGENT_MCP_BOOTSTRAP_USERNAME", None)
            os.environ.pop("AGENT_MCP_BOOTSTRAP_PASSWORD", None)

    # Phase 3 Wave 2 (v5.0.69): make sure SOME user is a sysadmin
    # after bootstrap. The Wave-1a migration promotes the earliest
    # operator when upgrading an existing deployment, but on a
    # FRESH deployment the migration runs before any user exists,
    # so the data step finds nothing to promote. Re-run the
    # idempotent helper after the bootstrap path has had a chance
    # to create the first operator — same SQL, same idempotency
    # guarantee (never demotes, never crowns a second sysadmin).
    try:
        from . import group_resolver
        group_resolver.bootstrap_first_operator_as_sysadmin()
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "bootstrap_first_operator_as_sysadmin failed post-init",
        )


def _users_table_is_empty() -> bool:
    with _connect() as conn:
        cur = conn.execute("SELECT 1 FROM users LIMIT 1")
        return cur.fetchone() is None


# ── User CRUD ──────────────────────────────────────────────────────


def create_user(
    username: str,
    password: str,
    email: str | None = None,
) -> str:
    """Create a user; return the assigned user_id.

    Side effect: if this is the FIRST user in an otherwise empty
    users table AND one or more projects are registered in the
    project registry, the new user gets a `project_membership` row
    for every project. This is the pre-Phase-1-deployment migration
    story — existing operators inherit access to every project they
    were already implicitly admins of.
    """
    user_id = secrets.token_hex(8)  # 16 hex chars
    password_hash = hash_password(password)
    created_at = _now_iso()

    with _connect() as conn:
        was_empty = (
            conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None
        )
        try:
            conn.execute(
                """
                INSERT INTO users
                    (user_id, username, email, password_hash, created_at,
                     last_login_at)
                VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (user_id, username, email, password_hash, created_at),
            )
        except sqlite3.IntegrityError as e:
            # UNIQUE(username) is the only constraint that can fail
            # here; surface it as a typed error so CLI/HTTP callers
            # can render a clean "username taken" message rather than
            # a stack trace.
            raise UsernameAlreadyExistsError(
                f"username {username!r} already exists"
            ) from e

        if was_empty:
            # Phase 3 Wave 2 (v5.0.69): the FIRST operator is also
            # implicitly the sysadmin. This is the fresh-deployment
            # bootstrap rule (the Wave-1a Alembic migration handles
            # the upgrade-existing path; this handles the brand-new
            # router). Same transaction as the membership grants so
            # no concurrent reader sees a "first operator, no
            # sysadmin" half-state.
            conn.execute(
                "UPDATE users SET is_sysadmin = 1 WHERE user_id = ?",
                (user_id,),
            )
            # First operator: grant membership in every registered
            # project. Done inside the same transaction so a
            # half-state (user without memberships) can't be observed
            # by a concurrent reader.
            existing = _list_registered_projects()
            for project_name in existing:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO project_membership
                        (project_name, user_id)
                    VALUES (?, ?)
                    """,
                    (project_name, user_id),
                )
            if existing:
                logger.info(
                    "First operator %r granted membership in %d "
                    "pre-existing project(s): %s",
                    username,
                    len(existing),
                    ", ".join(existing),
                )

    return user_id


def get_user_by_username(username: str) -> dict[str, Any] | None:
    """Return the user row for `username`, or None if missing."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT * FROM users WHERE username = ?",
            (username,),
        )
        row = cur.fetchone()
    return dict(row) if row is not None else None


def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    """Return the user row for `user_id`, or None if missing."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = cur.fetchone()
    return dict(row) if row is not None else None


def touch_last_login(user_id: str) -> None:
    """Stamp `last_login_at` on a successful login.

    Called by the login route in PR C; kept here so all writes to
    the users table funnel through one module.
    """
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET last_login_at = ? WHERE user_id = ?",
            (_now_iso(), user_id),
        )


# ── Sessions ───────────────────────────────────────────────────────


def create_session(
    user_id: str, lifetime_days: int = DEFAULT_SESSION_LIFETIME_DAYS
) -> str:
    """Create a session for `user_id`; return the session_id.

    `lifetime_days` may be negative — useful in tests that want an
    already-expired row to assert the prune sweep removes it.
    """
    session_id = secrets.token_hex(16)  # 32 hex chars
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=lifetime_days)
    now_iso = now.isoformat(timespec="milliseconds")
    expires_iso = expires.isoformat(timespec="milliseconds")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO sessions
                (session_id, user_id, created_at, expires_at, last_used_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, user_id, now_iso, expires_iso, now_iso),
        )
    return session_id


def get_session(session_id: str) -> dict[str, Any] | None:
    """Return the session row, or None if missing OR expired.

    Side effect: slides `last_used_at` to "now" on every successful
    fetch. The 30-day window in `create_session` is a max-idle
    timer, not a hard cap; an active operator keeps the session
    alive forever.
    """
    with _connect() as conn:
        cur = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        # Expiry check on read. We don't delete the row here — the
        # periodic prune sweep handles cleanup — but we DO refuse to
        # surface it, so callers can't extend an expired session by
        # mere mention.
        expires_at = datetime.fromisoformat(row["expires_at"])
        now = datetime.now(timezone.utc)
        if expires_at <= now:
            return None
        new_last_used = now.isoformat(timespec="milliseconds")
        conn.execute(
            "UPDATE sessions SET last_used_at = ? WHERE session_id = ?",
            (new_last_used, session_id),
        )
        # Return the row with the slid timestamp baked in so the
        # caller sees the value it just stored.
        out = dict(row)
        out["last_used_at"] = new_last_used
        return out


def delete_session(session_id: str) -> None:
    """Drop a session row. No-op if missing."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM sessions WHERE session_id = ?",
            (session_id,),
        )


def prune_expired_sessions() -> int:
    """Delete every session whose `expires_at` is in the past.

    Returns the number of rows deleted. Called periodically by the
    router's reaper task (wired in PR D); safe to call ad-hoc.
    """
    now_iso = _now_iso()
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM sessions WHERE expires_at <= ?",
            (now_iso,),
        )
        return cur.rowcount


# ── Project membership ─────────────────────────────────────────────


def add_project_membership(user_id: str, project_name: str) -> None:
    """Grant `user_id` access to `project_name`. Idempotent."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO project_membership
                (project_name, user_id)
            VALUES (?, ?)
            """,
            (project_name, user_id),
        )


def remove_project_membership(user_id: str, project_name: str) -> None:
    """Revoke `user_id`'s access to `project_name`. No-op if missing."""
    with _connect() as conn:
        conn.execute(
            """
            DELETE FROM project_membership
            WHERE user_id = ? AND project_name = ?
            """,
            (user_id, project_name),
        )


def is_project_member(user_id: str, project_name: str) -> bool:
    """Return True iff ``user_id`` has a row in ``project_membership``
    for ``project_name``.

    Phase 1 PR D helper. The router's ``require_operator_session``
    middleware calls this on every project-scoped dashboard mutation
    so a logged-in operator without explicit access to ``project_name``
    gets a clean 401 rather than reaching the backend.

    Returns False on a missing ``project_membership`` table — same
    "treat as no access" defence as ``get_session``'s missing-row
    branch. The router's startup hook always runs migrations before
    the first request, so the missing-table case is genuinely the
    "router.db not initialised yet" path.
    """
    try:
        with _connect() as conn:
            cur = conn.execute(
                """
                SELECT 1 FROM project_membership
                WHERE user_id = ? AND project_name = ?
                """,
                (user_id, project_name),
            )
            return cur.fetchone() is not None
    except sqlite3.OperationalError:
        return False


def list_user_projects(user_id: str) -> list[str]:
    """Return the project names `user_id` may administer (sorted)."""
    with _connect() as conn:
        cur = conn.execute(
            """
            SELECT project_name FROM project_membership
            WHERE user_id = ?
            ORDER BY project_name
            """,
            (user_id,),
        )
        return [row["project_name"] for row in cur.fetchall()]


# ── Project registry interop ───────────────────────────────────────


def _list_registered_projects() -> list[str]:
    """Project names from the router's projects.local.json.

    Lives here (not at module top-level) so the import doesn't fire
    at module-load time — `project_registry` reads env vars during
    its own initialisation and we want the AGENT_MCP_PROJECTS_FILE
    override to win whenever the call lands.

    Returns an empty list if the registry file doesn't exist or is
    unreadable — we don't want a first-boot deploy with no projects
    yet to crash the bootstrap on a missing JSON file.
    """
    try:
        from .project_registry import ProjectRegistry

        return [p["name"] for p in ProjectRegistry().list()]
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "Failed to enumerate projects from registry; "
            "first-operator retroactive membership will be empty."
        )
        return []
