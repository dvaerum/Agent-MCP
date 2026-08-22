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

from ..utils.json_utils import _strip_control_bytes
from .migrations_runner import (
    get_router_db_path,
    run_router_migrations_upgrade,
)

# Re-exported so callers (CLI, lifespan, tests) can import everything
# they need from `agent_mcp.router.identity` without reaching into the
# migrations module.
__all__ = [
    "DEFAULT_SESSION_LIFETIME_DAYS",
    "PASSWORD_MIN_LENGTH",
    "IdentityError",
    "InvalidEmailError",
    "UsernameAlreadyExistsError",
    "WeakPasswordError",
    "add_project_membership",
    "bootstrap_first_operator",
    "create_session",
    "create_user",
    "delete_session",
    "find_linkable_user_by_email",
    "find_user_by_sso_subject",
    "get_router_db_path",
    "get_session",
    "get_user_by_id",
    "get_user_by_username",
    "hash_password",
    "init_router_db",
    "insert_project_membership",
    "is_project_member",
    "list_user_projects",
    "open_connection",
    "prune_expired_sessions",
    "remove_project_membership",
    "run_router_migrations_upgrade",
    "stamp_sso_subject_if_absent",
    "touch_last_login",
    "users_table_is_empty",
    "validate_password_strength",
    "verify_password",
]


logger = logging.getLogger(__name__)


# ── Errors ──────────────────────────────────────────────────────────


class IdentityError(Exception):
    """Base class for router identity errors."""


class UsernameAlreadyExistsError(IdentityError):
    """Raised by create_user when the username UNIQUE constraint fails."""


class InvalidEmailError(IdentityError):
    """Raised by ``create_user`` when ``email`` cannot round-trip through
    UTF-8 (R15-F2 sibling).

    ``email`` on this path is frequently IdP-claim-derived (the SSO JIT-
    create fork in ``sso.find_or_create_sso_user``) rather than REST-body
    JSON, so it never passes through ``admin_users_api._json_body``'s
    sanitizer wiring or its ``_reject_unencodable_str`` guard. A JIT
    email carrying an unpaired UTF-16 surrogate (however it got there —
    a malicious/misconfigured IdP, a claim-mapping bug) would otherwise
    crash the SQLite TEXT bind on the INSERT below with a raw
    ``UnicodeEncodeError``, exactly like the REST-layer crash this
    mirrors.
    """


class WeakPasswordError(IdentityError):
    """Raised when a password fails the strength policy.

    The message is operator-facing (rendered into the setup form), so
    it MUST NOT echo the rejected value.
    """


# ── Constants ───────────────────────────────────────────────────────


DEFAULT_SESSION_LIFETIME_DAYS = 30

# Minimum password length for any NEW operator password. 12 is a
# defensible modern floor: argon2 + login rate-limiting (round-1)
# already blunt online brute-force, so this guards mainly against
# trivially-guessable secrets on self-provisioned multi-tenant
# deployments (round-3 finding AC-2). This is the canonical policy
# home — the setup wizard imports it rather than defining its own.
# NOTE: this gates NEW password-setting only; it never re-validates
# existing stored hashes, so pre-policy operators keep working.
PASSWORD_MIN_LENGTH = 12


# Module-level PasswordHasher: stateless, thread-safe, cheap to share.
_HASHER = PasswordHasher()


# ── Connection helpers ─────────────────────────────────────────────


def open_connection() -> sqlite3.Connection:
    """THE single low-level router.db connection factory.

    Opens ``router.db`` with ``row_factory = sqlite3.Row`` and
    ``PRAGMA foreign_keys=ON``, creating the parent dir first. The
    caller owns the lifecycle (commit / close). This is the one home
    the three drifted connection helpers collapsed into
    (arch-deepening R2 #1c): the autocommit context-manager
    (:func:`_connect`) wraps it, and the store's transactional
    ``connect()`` (which ``admin_users_api._connect`` now routes
    through) hands it out raw.
    """
    db_path = get_router_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """Open a router.db connection with row-factory + FK enabled.

    Yields a context-managed connection that commits on clean exit
    and rolls back on exception. SQLite-level isolation is the
    default (transactional autobegin); we don't need explicit
    BEGIN/COMMIT around single-statement writes. Builds on the single
    :func:`open_connection` factory (arch-deepening R2 #1c) so the
    connection shape (row factory, FK pragma) has one definition.
    """
    conn = open_connection()
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


@contextmanager
def _conn_ctx(
    conn: sqlite3.Connection | None,
) -> Iterator[sqlite3.Connection]:
    """Yield a usable connection: the caller's if given, else a freshly
    self-opened autocommit one (arch-deepening R2 #1b).

    When ``conn`` is provided the caller owns the transaction — we do
    NOT commit or close it, we merely run our writes on it so a handler
    inside ``BEGIN IMMEDIATE`` enlists everything in one snapshot. When
    ``conn`` is ``None`` we borrow ``_connect`` (commit-on-exit).
    """
    if conn is not None:
        yield conn
    else:
        with _connect() as owned:
            yield owned


# ── Password hashing ───────────────────────────────────────────────


def validate_password_strength(password: str) -> None:
    """Enforce the password-strength policy; raise on violation.

    Canonical single-source policy check. Call it BEFORE ``create_user``
    at every path that sets a NEW operator password (the setup wizard;
    admin/self-serve user-create flows). Existing hashes are never
    re-validated. Raises ``WeakPasswordError`` (with an operator-facing
    message) when the policy is not met; returns ``None`` when it is.
    """
    if len(password) < PASSWORD_MIN_LENGTH:
        raise WeakPasswordError(
            f"Password must be at least {PASSWORD_MIN_LENGTH} characters."
        )


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
            if users_table_is_empty():
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
        from .router_store import store
        store.bootstrap_first_operator_as_sysadmin()
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "bootstrap_first_operator_as_sysadmin failed post-init",
        )


def users_table_is_empty(
    conn: sqlite3.Connection | None = None,
) -> bool:
    """True iff the ``users`` table has zero rows (or doesn't exist yet).

    THE single empty-table probe (arch-deepening R2 #1c) — the five
    drifted copies (this module's old ``_users_table_is_empty``, the
    setup wizard's, sso's, and two inline ``SELECT 1 FROM users`` sites)
    all route here via ``store.users_table_is_empty``. Pass ``conn`` to
    read inside a caller's open transaction (e.g. ``create_user``'s
    first-user check must see its own uncommitted INSERT snapshot);
    ``conn=None`` self-opens.

    A missing ``users`` table reads as empty — that's the pre-migration
    fresh-deploy state, which presents the same operator-facing "you
    need to set up" UX as a freshly-migrated empty table.
    """
    try:
        with _conn_ctx(conn) as c:
            return c.execute(
                "SELECT 1 FROM users LIMIT 1"
            ).fetchone() is None
    except sqlite3.OperationalError:
        return True


def bootstrap_first_operator(
    user_id: str,
    *,
    grant_sysadmin: bool,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Apply the first-operator bootstrap invariant to ``user_id``.

    THE single store-owned routine (arch-deepening R2 #1c) for the
    security-critical rule *"the first user on an otherwise-empty users
    table becomes sysadmin and gets membership in every registered
    project."* ``create_user`` calls this — inside its own INSERT
    transaction via ``conn`` — the moment it detects an empty table, so
    the invariant lives in exactly one place.

    * ``grant_sysadmin`` (True on the password/CLI path, and on the SSO
      path only when the operator opted in) flips ``is_sysadmin = 1``.
      When False the first user is created WITHOUT sysadmin (the SSO
      opt-out path — a fresh IdP deploy's first user is not silently
      crowned). Running the UPDATE inside the caller's transaction keeps
      it atomic with the INSERT: a concurrent reader on another
      connection never sees a "first operator, no sysadmin" half-state.
    * Membership in every registered project is granted unconditionally
      (the pre-Phase-1 migration story — existing single-tenant deploys
      upgrade smoothly because the first operator inherits access to
      every project they already had), routed through the single
      ``insert_project_membership`` writer on the same connection.
    """
    with _conn_ctx(conn) as c:
        if grant_sysadmin:
            c.execute(
                "UPDATE users SET is_sysadmin = 1 WHERE user_id = ?",
                (user_id,),
            )
        existing = _list_registered_projects()
        for project_name in existing:
            insert_project_membership(
                project_name, user_id=user_id, or_ignore=True, conn=c,
            )
        if existing:
            logger.info(
                "First operator (user_id=%s) granted membership in %d "
                "pre-existing project(s): %s",
                user_id,
                len(existing),
                ", ".join(existing),
            )


# ── User CRUD ──────────────────────────────────────────────────────


def create_user(
    username: str,
    password: str | None = None,
    email: str | None = None,
    *,
    password_hash: str | None = None,
    is_sysadmin: bool = False,
    sso_subject: str | None = None,
    bootstrap_sysadmin: bool = True,
) -> str:
    """Create a user; return the assigned user_id.

    Password vs. passwordless (arch-deepening R2 #1b): pass ``password``
    for a normal operator (it is argon2-hashed into ``password_hash``);
    pass ``password_hash=None`` with no ``password`` for an SSO-only row
    (``password_hash`` stays NULL — session-anchored, no password login).
    ``sso_subject`` stamps the stable reconciliation key on the row. This
    unifies what ``sso._create_passwordless_user`` used to fork verbatim.

    First-user bootstrap: if this is the FIRST user in an otherwise empty
    users table it is granted a ``project_membership`` row for every
    registered project (the pre-Phase-1 migration story). It is ALSO
    promoted to sysadmin when ``bootstrap_sysadmin`` is True (the default
    — the password path always promotes the first operator; the SSO path
    threads its opt-in flag through here so a fresh IdP deploy's first
    user is only auto-crowned when the operator opted in). ``is_sysadmin``
    forces the bit on regardless of table emptiness (the proxy-header
    ``default_is_sysadmin`` path).

    ``email`` sanitizer wiring (R15-F2 sibling): this is the ONLY
    ``create_user`` caller — CLI bootstrap, the setup wizard, AND
    ``sso.find_or_create_sso_user``'s JIT-create fork — that writes
    ``email`` to the DB, so it's stripped of the same hidden/spoofing
    Unicode ``admin_users_api._json_body`` strips on the REST path
    (``_strip_control_bytes``) and checked for UTF-8 round-trip-ability
    here, once, for every caller. The IdP-claim-derived SSO path in
    particular never goes anywhere near ``admin_users_api``'s sanitizer
    wiring, so without this an unpaired UTF-16 surrogate in an IdP's
    ``email`` claim would crash the INSERT below exactly like the
    REST-layer bug this mirrors.
    """
    if email is not None:
        email = _strip_control_bytes(email)
        try:
            email.encode("utf-8", "strict")
        except UnicodeEncodeError as e:
            raise InvalidEmailError(
                "email contains a character that cannot be represented "
                "in UTF-8 (e.g. an unpaired surrogate)"
            ) from e
    user_id = secrets.token_hex(8)  # 16 hex chars
    if password_hash is None and password is not None:
        password_hash = hash_password(password)
    created_at = _now_iso()

    # Manual BEGIN IMMEDIATE so the first-user emptiness PROBE, the
    # INSERT, and the ``bootstrap_first_operator`` sysadmin/membership
    # grant are ONE atomic unit under a single write-lock. Python's
    # default autocommit does NOT enlist a bare ``SELECT`` with the later
    # INSERT's deferred transaction, so ``was_empty`` was read outside
    # any lock: two ``create_user`` calls racing on an empty table could
    # BOTH read ``was_empty=True`` and BOTH bootstrap a sysadmin
    # (dual-sysadmin), and a wizard double-submit could mint a second
    # operator past the empty-table gate. This is serialised away on the
    # current single-process aiohttp loop (``create_user`` is fully
    # synchronous), but arms under a multi-worker deploy (anticipated in
    # ``rate_limit.py``) or a future ``asyncio.to_thread(create_user, …)``
    # refactor. ``BEGIN IMMEDIATE`` takes the write-lock up front so a
    # concurrent second creator blocks, then re-reads ``was_empty=False``
    # and is neither crowned nor bootstrapped — mirroring the check-then-
    # act sites in ``admin_users_api.py`` (edit/delete user,
    # add_group_member). ``isolation_level = None`` hands transaction
    # control fully to us (the default DML autobegin would fight the
    # explicit BEGIN). ``is_sysadmin`` still forces the bit on
    # independently of emptiness (the proxy-header ``default_is_sysadmin``
    # path); the FIRST-operator promotion is owned solely by
    # ``bootstrap_first_operator`` below, enlisted in THIS transaction.
    conn = open_connection()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        was_empty = users_table_is_empty(conn=conn)
        try:
            conn.execute(
                """
                INSERT INTO users
                    (user_id, username, email, password_hash, created_at,
                     last_login_at, is_sysadmin, sso_subject)
                VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    user_id, username, email, password_hash, created_at,
                    1 if is_sysadmin else 0, sso_subject,
                ),
            )
        except sqlite3.IntegrityError as e:
            # UNIQUE(username) is the only constraint that can fail
            # here; surface it as a typed error so CLI/HTTP callers
            # can render a clean "username taken" message rather than
            # a stack trace. The blanket rollback below unwinds the
            # open IMMEDIATE transaction before the error propagates.
            raise UsernameAlreadyExistsError(
                f"username {username!r} already exists"
            ) from e
        except UnicodeEncodeError as e:
            # R15-F2 defensive backstop: the pre-check above should
            # already have caught an unpaired surrogate in ``email``,
            # but this converts a raw ``UnicodeEncodeError`` at the bind
            # site into the same typed error regardless — a future
            # refactor that skips the pre-check must not let this
            # escape as an opaque, undocumented exception type.
            raise InvalidEmailError(
                "email contains a character that cannot be represented "
                "in UTF-8 (e.g. an unpaired surrogate)"
            ) from e

        if was_empty:
            # First operator on an empty table: apply the bootstrap
            # invariant (sysadmin unless the SSO opt-out declined it,
            # plus membership in every registered project) via the one
            # store-owned routine, enlisted in THIS transaction.
            from .router_store import store
            store.bootstrap_first_operator(
                user_id, grant_sysadmin=bootstrap_sysadmin, conn=conn,
            )
        conn.execute("COMMIT")
    except BaseException:
        # Any exit before COMMIT leaves the IMMEDIATE transaction open;
        # roll it back so the write-lock is released and no half-state
        # persists. Guard the "no transaction is active" case for the
        # rare path where ``BEGIN IMMEDIATE`` itself failed (e.g. the
        # loser of a lock race under a zero busy_timeout) — we must let
        # that original error propagate, not mask it.
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        conn.close()

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


# ── SSO user reconciliation reads/writes (via RouterStore) ─────────


def find_user_by_sso_subject(
    subject: str, conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    """Return the users row whose ``sso_subject`` == ``subject``, or None.

    Connection-injectable home (arch-deepening R2 #1c) for sso.py's
    former inline ``sqlite3.connect`` in ``_find_user_by_subject`` — the
    stable-subject reconciliation lookup.
    """
    with _conn_ctx(conn) as c:
        row = c.execute(
            "SELECT * FROM users WHERE sso_subject = ? LIMIT 1",
            (subject,),
        ).fetchone()
    return dict(row) if row is not None else None


def find_linkable_user_by_email(
    email: str, conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    """Return a LINK-eligible local row for ``email`` (case-insensitive).

    Connection-injectable home (arch-deepening R2 #1c) for sso.py's
    former inline ``sqlite3.connect`` in ``_find_linkable_user_by_email``.
    Only a password-backed local operator (``password_hash IS NOT NULL``)
    or a legacy pre-subject SSO row (``sso_subject IS NULL``) is eligible;
    password users are preferred when both shapes share an email. The
    predicate is unchanged from the inline query it replaces.
    """
    with _conn_ctx(conn) as c:
        row = c.execute(
            """
            SELECT * FROM users
            WHERE LOWER(email) = LOWER(?)
              AND (password_hash IS NOT NULL OR sso_subject IS NULL)
            ORDER BY (password_hash IS NULL) ASC
            LIMIT 1
            """,
            (email,),
        ).fetchone()
    return dict(row) if row is not None else None


def stamp_sso_subject_if_absent(
    user_id: str,
    subject: str,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Bind ``subject`` to ``user_id`` iff the row has no subject yet.

    Connection-injectable home (arch-deepening R2 #1c) for sso.py's
    former inline ``sqlite3.connect`` in ``_stamp_subject_if_absent``.
    Idempotent + race-safe: the ``sso_subject IS NULL`` guard means a
    second, different subject can never overwrite an already-bound row.
    """
    with _conn_ctx(conn) as c:
        c.execute(
            "UPDATE users SET sso_subject = ? "
            "WHERE user_id = ? AND sso_subject IS NULL",
            (subject, user_id),
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


def insert_project_membership(
    project_name: str,
    *,
    user_id: str | None = None,
    group_id: str | None = None,
    role: str | None = None,
    or_ignore: bool = False,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Canonical connection-injectable ``project_membership`` writer
    (arch-deepening R2 #1b) — the single home the four inline INSERTs
    (this module's grant + first-user loop, the admin user- and
    group-grant handlers, the SSO first-user loop) route through.

    Exactly one of ``user_id`` / ``group_id`` must be set (mirrors the
    table's CHECK); a clean ``ValueError`` surfaces the misuse. ``role``
    is omitted from the column list when None so the DB default
    (``'operator'``) applies — byte-identical to the historic
    ``(project_name, user_id)`` insert. ``or_ignore`` selects
    ``INSERT OR IGNORE`` (the idempotent grant paths) vs a plain
    ``INSERT`` whose ``sqlite3.IntegrityError`` the admin handler maps to
    a 409. Pass ``conn`` to enlist in a caller's open transaction.
    """
    if (user_id is None) == (group_id is None):
        raise ValueError(
            "insert_project_membership requires exactly one of "
            "user_id or group_id"
        )
    # ``user_id`` is always listed (NULL for a group grant) so the row
    # shape matches the admin group-grant INSERT's explicit
    # ``(project_name, user_id, group_id, role) VALUES (?, NULL, ?, ?)``.
    columns = ["project_name", "user_id"]
    values: list[Any] = [project_name, user_id]
    if group_id is not None:
        columns.append("group_id")
        values.append(group_id)
    if role is not None:
        columns.append("role")
        values.append(role)
    verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
    sql = (
        f"{verb} INTO project_membership ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in values)})"
    )
    with _conn_ctx(conn) as c:
        c.execute(sql, tuple(values))


def add_project_membership(user_id: str, project_name: str) -> None:
    """Grant `user_id` access to `project_name`. Idempotent.

    Thin back-compat wrapper over :func:`insert_project_membership` (the
    canonical writer); keeps the positional ``(user_id, project_name)``
    signature its many callers depend on.
    """
    insert_project_membership(project_name, user_id=user_id, or_ignore=True)


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
