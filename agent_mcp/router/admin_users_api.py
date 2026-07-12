"""Router admin REST surface — users, groups, project memberships.

Phase 3 Wave 1b (prancy-napping-pie). Adds operator-facing CRUD
over the router-level identity tables introduced in Phase 1
(``users``, ``project_membership``) and extended in Phase 3 Wave 1a
(``groups``, ``group_membership``; new columns on the existing
tables).

URL prefix: every handler lives under ``/agent-mcp/api/router/...``
so the operator-session middleware gates them automatically (same
mechanism as the existing ``admin_api`` module). The handlers are
register-side-effect-only — wiring happens in ``register_admin_users_routes``,
called from ``router.app.make_app`` next to the existing admin route
registration.

ENFORCEMENT NOTE (Wave 1b boundary): every endpoint here requires
only that the caller carry a valid operator session cookie. There
is no per-role gating yet. Wave 2 PR 3c will:
  * require ``users.is_sysadmin`` for user/group CRUD,
  * require ``users.is_sysadmin`` OR project ``operator`` role for
    project-membership management.
The dashboard scaffolding ships first so we can review the UX
independently of the resolver work.

SCHEMA GUARD (Wave 1a coupling): the handlers reach into the
Phase 3 schema (``users.is_sysadmin``, the ``groups`` /
``group_membership`` tables, ``project_membership.role`` /
``.group_id``). To keep this PR shippable before Wave 1a lands
in main, ``_ensure_wave1a_schema()`` runs a small idempotent
``CREATE TABLE IF NOT EXISTS`` / ``ALTER TABLE`` migration on
first use. When Wave 1a's Alembic migration arrives the helper
becomes a no-op (the columns/tables already exist; ``ALTER TABLE
IF NOT EXISTS`` style is emulated via a column-name lookup) so
the two PRs compose without drift.

Membership-id surrogate: ``project_membership`` has no
auto-increment PK — it's keyed on (project_name, user_id) or
(project_name, group_id). The REST DELETE/PATCH paths use
``u:<user_id>`` or ``g:<group_id>`` as the membership identifier
so callers can address either kind of row without an extra "is
this a user or group row?" query param. The prefix is parsed in
``_split_membership_id``.

Wave 9 PR 5 (prancy-napping-pie) adds two more group-scoped
endpoints — ``GET`` / ``PUT`` ``/api/router/groups/<id>/capabilities``
— that read and atomically replace the cap grants for a group.
Both are sysadmin-gated (the cap
``system.groups.capabilities.manage`` is exclusively in the
sysadmin set per the Wave 9 bundle table). Unknown cap strings on
``PUT`` fail closed with a 400 ``unknown_capability`` error.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from aiohttp import web

from . import identity
from .router_store import store
from .single_tenant import bypasses_operator_gate


logger = logging.getLogger(__name__)


# ── Error codes (extended from app._ERROR_*) ───────────────────────


_ERROR_VALIDATION = "validation_error"
_ERROR_NOT_FOUND = "not_found"
_ERROR_CONFLICT = "conflict"
_ERROR_INTERNAL = "internal_error"


_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_GROUP_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
# Password STRENGTH policy is NOT defined here — it lives canonically in
# ``identity.validate_password_strength`` (min length ``identity.
# PASSWORD_MIN_LENGTH``). This handler calls that single source so an
# admin-created user gets the same floor as a self-provisioned one (no
# policy drift). Only the required-field check stays local below.


# ── Schema guard (Wave 1a interop) ─────────────────────────────────


# Cache by DB-path string so tests that swap router.db across tmp
# directories don't see a stale "already ensured" flag from another
# test's connection. The module-level identity helper resolves the
# env var on every call (no caching of its own), so we key off the
# same resolved string.
_SCHEMA_ENSURED_PATHS: set[str] = set()


def _ensure_wave1a_schema() -> None:
    """Apply the Phase 3 Wave 1a schema additions if not already present.

    Wave 1a (the parallel PR) ships these via Alembic. Wave 1b
    (this PR) needs to read/write the same tables; when Wave 1a
    lands first, the columns/tables already exist and the helper
    is a no-op. When Wave 1b lands first, the helper creates the
    minimum schema so the handlers + tests can function. Either
    way the two PRs compose without drift; Wave 1a's canonical
    migration is the long-term source.

    Idempotency: every ``ALTER TABLE ... ADD COLUMN`` is guarded
    by a ``PRAGMA table_info`` lookup because SQLite doesn't
    support ``ADD COLUMN IF NOT EXISTS``. ``CREATE TABLE IF NOT
    EXISTS`` covers the new tables.

    Cached per-DB-path: the all-or-nothing check runs once per
    distinct router.db path. Tests that swap router.db files via
    ``AGENT_MCP_ROUTER_DB`` see independent caches because the
    resolved path is different.
    """
    db_path = identity.get_router_db_path()
    path_key = str(db_path)
    if path_key in _SCHEMA_ENSURED_PATHS:
        return
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        # users.is_sysadmin
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if cols and "is_sysadmin" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN is_sysadmin "
                "INTEGER NOT NULL DEFAULT 0"
            )
        # groups
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS groups (
                group_id     TEXT PRIMARY KEY,
                name         TEXT UNIQUE NOT NULL,
                is_sysadmin  INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT NOT NULL
            )
            """
        )
        # group_membership
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS group_membership (
                group_id        TEXT NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
                member_user_id  TEXT REFERENCES users(user_id) ON DELETE CASCADE,
                member_group_id TEXT REFERENCES groups(group_id) ON DELETE CASCADE,
                added_at        TEXT NOT NULL,
                CHECK ((member_user_id IS NOT NULL) <> (member_group_id IS NOT NULL))
            )
            """
        )
        # Partial UNIQUE indices per grant path (mirrors router migration
        # 0006 / the project_membership uniqueness). Keeps add_group_member
        # idempotent: a double-submit hits the constraint → 409 instead of
        # a duplicate row. Same names as the migration so the two paths
        # (Wave1a-first vs Wave1b-first) converge without drift.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_group_membership_user "
            "ON group_membership(group_id, member_user_id) "
            "WHERE member_user_id IS NOT NULL"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_group_membership_group "
            "ON group_membership(group_id, member_group_id) "
            "WHERE member_group_id IS NOT NULL"
        )
        # project_membership.role / .group_id
        pm_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(project_membership)")
        }
        if pm_cols and "role" not in pm_cols:
            conn.execute(
                "ALTER TABLE project_membership ADD COLUMN role "
                "TEXT NOT NULL DEFAULT 'operator' "
                "CHECK (role IN ('operator', 'viewer'))"
            )
        if pm_cols and "group_id" not in pm_cols:
            # Note: SQLite ALTER TABLE ADD COLUMN can't add a FK
            # constraint after the fact, but the column itself is
            # what the REST layer reads/writes; FK enforcement is
            # handled at app level. Wave 1a's canonical migration
            # has the proper FK.
            conn.execute(
                "ALTER TABLE project_membership ADD COLUMN group_id TEXT"
            )
        # Phase 3 needs user_id to be NULLABLE so a group-only row
        # (user_id IS NULL AND group_id IS NOT NULL) is legal. The
        # Phase 1 schema declared user_id NOT NULL; SQLite can't drop
        # NOT NULL via ALTER, so rebuild the table once if needed.
        # Wave 1a's migration does the same rebuild canonically.
        if pm_cols:
            user_id_row = next(
                (r for r in conn.execute(
                    "PRAGMA table_info(project_membership)"
                ) if r[1] == "user_id"),
                None,
            )
            # PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk
            if user_id_row is not None and user_id_row[3] == 1:
                conn.executescript(
                    """
                    CREATE TABLE project_membership__new (
                        project_name   TEXT NOT NULL,
                        user_id        TEXT REFERENCES users(user_id) ON DELETE CASCADE,
                        group_id       TEXT REFERENCES groups(group_id) ON DELETE CASCADE,
                        role           TEXT NOT NULL DEFAULT 'operator'
                                       CHECK (role IN ('operator', 'viewer')),
                        CHECK ((user_id IS NOT NULL) <> (group_id IS NOT NULL))
                    );
                    INSERT INTO project_membership__new
                        (project_name, user_id, group_id, role)
                    SELECT project_name, user_id, group_id,
                           COALESCE(role, 'operator')
                    FROM project_membership;
                    DROP TABLE project_membership;
                    ALTER TABLE project_membership__new
                        RENAME TO project_membership;
                    CREATE INDEX IF NOT EXISTS
                        idx_project_membership_user_id
                        ON project_membership(user_id);
                    """
                )
        conn.commit()
    finally:
        conn.close()
    _SCHEMA_ENSURED_PATHS.add(path_key)


# ── Envelope helpers (mirror app._success_envelope shape) ──────────


def _success(payload: dict, *, status: int = 200) -> web.Response:
    body: dict = {"success": True}
    body.update(payload)
    return web.json_response(
        body, status=status, headers={"Cache-Control": "no-store"},
    )


def _error(
    *, error: str, message: str, status: int, extra: dict | None = None,
) -> web.Response:
    body: dict = {"success": False, "error": error, "message": message}
    if extra:
        body.update(extra)
    return web.json_response(
        body, status=status, headers={"Cache-Control": "no-store"},
    )


async def _json_body(req: web.Request) -> dict:
    raw = await req.read()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        # Broaden to ValueError (was JSONDecodeError): an invalid-UTF8
        # body makes ``json.loads(bytes)`` raise ``UnicodeDecodeError``
        # — a ``ValueError`` subclass but NOT a ``JSONDecodeError`` — so
        # the narrower guard let it propagate to an uncaught 500
        # (PF-R21-1). ``ValueError`` covers BOTH JSONDecodeError and
        # UnicodeDecodeError; ``RecursionError`` (a ``RuntimeError``, not
        # a ValueError subclass) stays explicit for the deeply-nested
        # body case (PF-R20-1). All are a malformed body → clean 400.
        msg = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
        raise web.HTTPBadRequest(
            text=json.dumps({
                "success": False, "error": _ERROR_VALIDATION,
                "message": f"request body is not valid JSON: {msg}",
            }),
            content_type="application/json",
        )
    if not isinstance(parsed, dict):
        raise web.HTTPBadRequest(
            text=json.dumps({
                "success": False, "error": _ERROR_VALIDATION,
                "message": "request body must be a JSON object",
            }),
            content_type="application/json",
        )
    return parsed


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ── Sysadmin-grant guard (self-escalation defence) ─────────────────


def _caller_is_sysadmin(req: web.Request) -> bool:
    """True iff the CALLER of this request is a sysadmin.

    Writing the ``is_sysadmin`` bit — on a user OR a group, granting on
    create/edit or clearing on edit — is reserved for sysadmins. The
    ``system.users.manage`` / ``system.groups.manage`` capabilities that
    gate these routes are NOT sufficient: a sysadmin-flagged group
    confers sysadmin to its members (``group_resolver`` walks the
    transitive closure), so letting a delegated operator flip the bit
    would let them self-escalate to full sysadmin. Granting sysadmin is
    strictly a sysadmin-only operation.

    Single-tenant mode (ADR-0008) pins the deploy to one operator-owned
    host with no multi-operator audience; the auth middleware bypasses
    the per-request Principal there, so treat that operator as sysadmin
    (mirrors ``perm_gates.require_capability``'s single-tenant bypass).
    """
    if bypasses_operator_gate():
        return True
    principal = req.get("principal")
    if principal is not None and getattr(principal, "sysadmin", False):
        return True
    # Fall back to the flag the middleware stashes alongside the
    # Principal (canonical source is the Principal; this mirrors it).
    return bool(req.get("is_sysadmin"))


def _forbid_sysadmin_write(req: web.Request) -> web.Response:
    """403 envelope for a non-sysadmin attempting to write ``is_sysadmin``."""
    user = req.get("user") or {}
    username = user.get("username", "<unknown>")
    return _error(
        error="forbidden",
        message=(
            f"operator {username!r} may not set 'is_sysadmin'; granting "
            "or clearing sysadmin is reserved for sysadmins"
        ),
        status=403,
    )


def _is_last_sysadmin(conn: sqlite3.Connection, user_id: str) -> bool:
    """True iff ``user_id`` is the only remaining ``users.is_sysadmin=1``.

    Guards the last-sysadmin lockout: demoting or deleting the final
    sysadmin would leave nobody able to grant sysadmin again. Scoped to
    the direct ``users.is_sysadmin`` flag (the canonical grant path);
    sysadmin conferred transitively via a group is a separate,
    self-healing bit (the group can be re-flagged by any remaining
    direct sysadmin).
    """
    others = conn.execute(
        "SELECT 1 FROM users WHERE is_sysadmin = 1 AND user_id != ? LIMIT 1",
        (user_id,),
    ).fetchone()
    return others is None


def _last_sysadmin_error(verb: str) -> web.Response:
    """409 envelope for an attempt to demote/delete the last sysadmin."""
    return _error(
        error=_ERROR_CONFLICT,
        message=(
            f"cannot {verb} the last remaining sysadmin; promote another "
            "user or group to sysadmin first"
        ),
        status=409,
    )


def _forbid_sysadmin_membership(req: web.Request) -> web.Response:
    """403 envelope for a non-sysadmin adding a member to a group that is
    transitively sysadmin-flagged.

    A member of a (transitively) sysadmin-flagged group inherits sysadmin
    via ``group_resolver``'s transitive closure. So adding a member into
    such a group is a sysadmin-grant in disguise: a delegated operator
    could add themselves — or a group they control — and self-escalate.
    Reserved for sysadmins, exactly like writing the ``is_sysadmin`` bit.
    """
    user = req.get("user") or {}
    username = user.get("username", "<unknown>")
    return _error(
        error="forbidden",
        message=(
            f"operator {username!r} may not add members to a "
            "sysadmin-flagged group; a member inherits sysadmin via the "
            "group's transitive closure, so this is reserved for sysadmins"
        ),
        status=403,
    )


def _group_resolved_capabilities(
    conn: sqlite3.Connection, group_id: str,
) -> frozenset[str]:
    """Every capability a new member of ``group_id`` would inherit.

    The union of ``group_capability`` grants across ``group_id`` and its
    ancestor groups — mirrors the group-cap overlay in
    ``core.capabilities.resolve_capabilities`` (which unions caps over
    ``resolve_user_groups``' upward closure). The ancestor closure comes
    from ``RouterStore.resolve_group_ancestors`` on the caller's
    ``BEGIN IMMEDIATE`` connection (one snapshot, no second connection);
    the cap lookup stays here because capabilities are this module's
    (``group_capability``), not the resolver's, domain. Defensive against
    a pre-migration DB without the ``group_capability`` table (mirrors
    ``resolve_capabilities``' swallow-and-degrade posture).
    """
    ancestors = store.resolve_group_ancestors(group_id, conn=conn)
    placeholders = ",".join("?" for _ in ancestors)
    try:
        rows = conn.execute(
            f"SELECT DISTINCT capability FROM group_capability "
            f"WHERE group_id IN ({placeholders})",
            tuple(ancestors),
        ).fetchall()
    except sqlite3.OperationalError:  # table absent on a pre-0004 DB
        return frozenset()
    return frozenset(row[0] for row in rows)


def _caps_caller_lacks(
    req: web.Request, caps: Iterable[str],
) -> list[str]:
    """The subset of ``caps`` a NON-sysadmin caller does not hold.

    The single privilege-amplification guard shared by the cap-grant
    path (AZ-1: ``replace_group_capabilities_handler``) and the
    group-join path (AZ-2: ``add_group_member_handler``). Both routes
    admit callers by a ``system.*.manage`` cap alone, but that cap must
    only let a delegate ADMINISTER authority they already hold — never
    MINT authority beyond it and confer it on themselves (a group they
    control, or their own group's cap set).

    A sysadmin may grant / confer anything, so returns ``[]`` for them.
    For a non-sysadmin, a cap is a violation unless the caller already
    carries it (:meth:`Principal.has_capability`). Fail closed when no
    Principal is on the request — treat every cap as un-held.
    """
    if _caller_is_sysadmin(req):
        return []
    principal = req.get("principal")
    if principal is None:
        return sorted(caps)
    return sorted(c for c in caps if not principal.has_capability(c))


def _forbid_cap_amplification(
    req: web.Request, offending: list[str],
) -> web.Response:
    """403 envelope for a non-sysadmin attempting to grant / confer caps
    they do not themselves hold (privilege amplification).

    Mirrors ``_forbid_sysadmin_write`` / ``_forbid_sysadmin_membership``:
    ``error="forbidden"``, 403, operator named for the audit trail.
    """
    user = req.get("user") or {}
    username = user.get("username", "<unknown>")
    return _error(
        error="forbidden",
        message=(
            f"operator {username!r} may not grant capabilities they do "
            f"not themselves hold: "
            f"{', '.join(repr(c) for c in offending)}"
        ),
        status=403,
    )


# ── Project-membership self-escalation guard (SEC round 5) ─────────


def _membership_grant_denied(
    req: web.Request, project_name: str, conferred_role: str,
) -> web.Response | None:
    """403 when a non-sysadmin caller would confer project access above
    their own on ``project_name`` (SEC round 5, finding AZ-R5-1).

    ``add_project_membership_handler`` and
    ``change_project_membership_role_handler`` are gated only by
    ``system.projects.manage`` — a DELEGABLE table-management cap. But the
    per-project data middleware (``auth_middleware``) gates
    ``/api/<project>/…`` on ``project_membership``, NOT on that cap. So a
    non-sysadmin delegate self-writing a membership row (as a user, or via
    a group they belong to, or by PATCHing their own viewer row up) turns
    table-management authority into cross-tenant DATA authority — the
    unguarded sibling of the round-4 AZ-1/AZ-2 amplification fix.

    Guard (regardless of member kind — user OR group):
      * a sysadmin may confer anything (returns ``None``);
      * a non-sysadmin with NO membership on the project may confer
        nothing — they can't hand out access they don't have;
      * a non-sysadmin may not confer a role ABOVE their own effective
        role on the project (a viewer-caller may not grant/set operator).

    Fail closed: no Principal / no caller identity ⇒ treat as no
    membership and deny.
    """
    if _caller_is_sysadmin(req):
        return None
    principal = req.get("principal")
    caller_id = getattr(principal, "user_id", None) if principal else None
    caller_role = (
        store.resolve_user_project_role(caller_id, project_name)
        if caller_id
        else None
    )
    if caller_role is None or store.role_rank(
        conferred_role
    ) > store.role_rank(caller_role):
        user = req.get("user") or {}
        username = user.get("username", "<unknown>")
        held = caller_role or "none"
        return _error(
            error="forbidden",
            message=(
                f"operator {username!r} may not confer role "
                f"{conferred_role!r} on project {project_name!r}: a "
                "non-sysadmin may only grant membership at or below their "
                f"own role on that project (currently {held})"
            ),
            status=403,
        )
    return None


def _connect() -> sqlite3.Connection:
    """Hand out a raw router.db connection (row factory + FK enabled)
    the handler manages across multiple statements (e.g. an
    insert-then-select, or an explicit ``BEGIN IMMEDIATE``).

    Delegates to the single store-owned connection factory
    (``store.connect``) — the local duplicate open+PRAGMA body was
    retired in arch-deepening R2 #1c so the three drifted connection
    helpers share one definition. Callers MUST close the connection —
    use ``try / finally`` or wrap with ``with``.
    """
    return store.connect()


# ── Pydantic-style body validators (lightweight, no extra dep) ─────


# We don't pull in Pydantic just for these — aiohttp is the
# framework and ``app.py`` already validates by hand-rolled
# functions. Keeping the dependency surface narrow.


def _validate_username(name: str) -> str | None:
    if not name or not isinstance(name, str):
        return "username is required"
    if not _USERNAME_RE.match(name):
        return (
            f"username must match {_USERNAME_RE.pattern}; got {name!r}"
        )
    return None


def _validate_group_name(name: str) -> str | None:
    if not name or not isinstance(name, str):
        return "name is required"
    if not _GROUP_NAME_RE.match(name):
        return f"name must match {_GROUP_NAME_RE.pattern}; got {name!r}"
    return None


def _validate_role(role: str) -> str | None:
    if role not in ("operator", "viewer"):
        return (
            f"role must be one of 'operator'|'viewer'; got {role!r}"
        )
    return None


def _reject_non_str(value: Any, field: str, *, allow_none: bool) -> str | None:
    """Guard scalar-string body fields against structured JSON types.

    PF-R7-1: a JSON ``dict``/``list`` in ``user_id``/``group_id``/``email``
    reaches a SQLite bind and raises ``sqlite3.ProgrammingError`` ("type
    'dict' is not supported"), which the handlers' ``IntegrityError`` catch
    doesn't cover — so it escapes as a 500. Reject a non-``str`` here, before
    the write lock, with the same 400 ``validation_error`` envelope the
    handlers use elsewhere. ``float``/``bool``/``int`` already coerce and hit
    the IntegrityError→400 path, so only structured types need guarding; we
    reject any non-``str`` for a tight, predictable contract.

    ``allow_none`` covers optional fields (``email``); a ``None`` there means
    "unset", which binds fine.
    """
    if value is None:
        return None if allow_none else f"{field} is required"
    if not isinstance(value, str):
        return f"{field} must be a string; got {type(value).__name__}"
    return None


def _parse_bool_field(
    body: dict, field: str, *, default: bool = False,
) -> tuple[bool, str | None]:
    """Strictly parse a JSON-boolean body field. Returns ``(value, err)``.

    PF-R13-1: ``is_sysadmin`` (the only caller-supplied SECURITY boolean
    on this surface) was coerced with a bare ``bool()`` on the raw JSON
    value, so a truthy NON-boolean — a non-empty string like ``"false"``,
    a dict/list, a non-zero number — read as ``True`` and SILENTLY minted
    (or flipped) a sysadmin. Accept ONLY a real JSON boolean
    (``true``/``false``); an absent key yields ``default``. Any other
    type returns an error message the caller surfaces as a 400
    ``validation_error`` — the same tight, predictable contract as
    ``_reject_non_str`` (PF-R7-1), preferring reject-on-ambiguity over a
    surprising coercion.

    ``isinstance(True, int)`` is ``True`` in Python but
    ``isinstance(1, bool)`` is ``False``, so the ``isinstance(value,
    bool)`` guard accepts only JSON ``true``/``false`` and rejects a JSON
    number ``0``/``1`` as well as strings / objects / arrays.
    """
    if field not in body:
        return default, None
    value = body[field]
    if isinstance(value, bool):
        return value, None
    return default, (
        f"{field} must be a boolean (true/false); got "
        f"{type(value).__name__}"
    )


# ── Row shape helpers ──────────────────────────────────────────────


def _user_public_row(row: sqlite3.Row | dict) -> dict[str, Any]:
    """Public-safe user row. Drops password_hash unconditionally."""
    d = dict(row)
    d.pop("password_hash", None)
    # Normalise is_sysadmin to a bool for JSON serialisation
    # (SQLite stores it as INTEGER 0/1).
    d["is_sysadmin"] = bool(d.get("is_sysadmin", 0))
    return d


def _group_public_row(row: sqlite3.Row | dict, member_count: int = 0) -> dict[str, Any]:
    d = dict(row)
    d["is_sysadmin"] = bool(d.get("is_sysadmin", 0))
    d["member_count"] = member_count
    return d


# ── USERS handlers ─────────────────────────────────────────────────


async def list_users_handler(req: web.Request) -> web.Response:
    """``GET /agent-mcp/api/router/users``.

    Returns every row from the ``users`` table with the public-safe
    projection (no ``password_hash``). Used by the dashboard user
    list view.
    """
    _ensure_wave1a_schema()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT user_id, username, email, is_sysadmin, "
            "created_at, last_login_at "
            "FROM users ORDER BY username"
        ).fetchall()
    finally:
        conn.close()
    return _success({"users": [_user_public_row(r) for r in rows]})


async def create_user_handler(req: web.Request) -> web.Response:
    """``POST /agent-mcp/api/router/users``.

    Body: ``{username, password, email?, is_sysadmin?}``. Returns
    201 with the new public-safe row. argon2 hashing happens via
    ``identity.hash_password`` so the storage shape stays uniform
    with Phase 1 bootstrap.
    """
    _ensure_wave1a_schema()
    body = await _json_body(req)
    # PF-R8-1: reject a non-string ``username`` BEFORE ``.strip()`` — a
    # JSON dict/list makes ``(x or "").strip()`` raise AttributeError →
    # uncaught 500. ``_validate_username``'s isinstance check runs only
    # after the strip, too late. ``allow_none=True`` lets a missing field
    # fall through to the "" default so ``_validate_username`` still emits
    # its "username is required" message.
    username_err = _reject_non_str(
        body.get("username"), "username", allow_none=True,
    )
    if username_err is not None:
        return _error(error=_ERROR_VALIDATION, message=username_err, status=400)
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    email = body.get("email")
    # PF-R13-1: strict boolean parse — a truthy non-bool (e.g. "false")
    # must not silently mint a sysadmin.
    is_sysadmin, is_sysadmin_err = _parse_bool_field(
        body, "is_sysadmin", default=False,
    )
    if is_sysadmin_err is not None:
        return _error(
            error=_ERROR_VALIDATION, message=is_sysadmin_err, status=400,
        )

    # Granting sysadmin is sysadmin-only (self-escalation defence).
    if is_sysadmin and not _caller_is_sysadmin(req):
        return _forbid_sysadmin_write(req)

    err = _validate_username(username)
    if err is not None:
        return _error(error=_ERROR_VALIDATION, message=err, status=400)
    # Required-field guard stays local; the STRENGTH policy is delegated
    # to the canonical single source (identity.validate_password_strength).
    if not isinstance(password, str) or not password:
        return _error(
            error=_ERROR_VALIDATION, message="password is required",
            status=400,
        )
    try:
        identity.validate_password_strength(password)
    except identity.WeakPasswordError as exc:
        return _error(
            error=_ERROR_VALIDATION, message=str(exc), status=400,
        )
    email_err = _reject_non_str(email, "email", allow_none=True)
    if email_err is not None:
        return _error(error=_ERROR_VALIDATION, message=email_err, status=400)

    user_id = secrets.token_hex(8)
    password_hash = identity.hash_password(password)
    created_at = _now_iso()
    conn = _connect()
    try:
        try:
            conn.execute(
                "INSERT INTO users (user_id, username, email, "
                "password_hash, created_at, last_login_at, is_sysadmin) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?)",
                (
                    user_id, username, email, password_hash,
                    created_at, 1 if is_sysadmin else 0,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return _error(
                error=_ERROR_CONFLICT,
                message=f"username {username!r} already exists",
                status=409,
            )
        row = conn.execute(
            "SELECT user_id, username, email, is_sysadmin, "
            "created_at, last_login_at FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
    return _success({"user": _user_public_row(row)}, status=201)


async def edit_user_handler(req: web.Request) -> web.Response:
    """``PATCH /agent-mcp/api/router/users/<user_id>``.

    Partial update — only the fields supplied in the body are
    changed. Headline field for Wave 1b is ``is_sysadmin`` (the
    new Phase 3 tier). Email may also be updated. Password
    changes are out of scope for Wave 1b (separate
    self-serve flow planned for Wave 2/3).
    """
    _ensure_wave1a_schema()
    user_id = req.match_info["user_id"]
    body = await _json_body(req)
    # Setting OR clearing the sysadmin bit is sysadmin-only.
    if "is_sysadmin" in body and not _caller_is_sysadmin(req):
        return _forbid_sysadmin_write(req)
    sets: list[str] = []
    params: list[Any] = []
    # PF-R13-1: strict boolean parse — a truthy non-bool (e.g. "false")
    # must not silently flip the sysadmin bit. Parsed once here so the
    # ``demoting`` check below reflects the same value.
    has_is_sysadmin = "is_sysadmin" in body
    is_sysadmin_val = False
    if has_is_sysadmin:
        is_sysadmin_val, is_sysadmin_err = _parse_bool_field(
            body, "is_sysadmin", default=False,
        )
        if is_sysadmin_err is not None:
            return _error(
                error=_ERROR_VALIDATION, message=is_sysadmin_err,
                status=400,
            )
        sets.append("is_sysadmin = ?")
        params.append(1 if is_sysadmin_val else 0)
    if "email" in body:
        # email is nullable (setting None clears it); reject structured
        # JSON types before the write lock (PF-R7-1).
        email_err = _reject_non_str(body["email"], "email", allow_none=True)
        if email_err is not None:
            return _error(
                error=_ERROR_VALIDATION, message=email_err, status=400,
            )
        sets.append("email = ?")
        params.append(body["email"])
    if not sets:
        return _error(
            error=_ERROR_VALIDATION,
            message="no editable fields supplied",
            status=400,
        )
    # Demotion = clearing an existing sysadmin bit. Guarded below against
    # dropping the sysadmin count to zero (last-sysadmin lockout).
    demoting = has_is_sysadmin and not is_sysadmin_val
    conn = _connect()
    # Manual transaction so the last-sysadmin count check and the UPDATE
    # are atomic under one write-lock — two peers racing to demote the
    # last two sysadmins can't both pass the check (BEGIN IMMEDIATE
    # serialises them; the loser sees the winner's write and is rejected).
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT is_sysadmin FROM users WHERE user_id = ?", (user_id,),
        ).fetchone()
        if existing is None:
            conn.execute("ROLLBACK")
            return _error(
                error=_ERROR_NOT_FOUND,
                message=f"unknown user_id: {user_id!r}",
                status=404,
            )
        if demoting and existing["is_sysadmin"] and _is_last_sysadmin(
            conn, user_id,
        ):
            conn.execute("ROLLBACK")
            return _last_sysadmin_error("demote")
        params.append(user_id)
        conn.execute(
            f"UPDATE users SET {', '.join(sets)} WHERE user_id = ?",
            params,
        )
        row = conn.execute(
            "SELECT user_id, username, email, is_sysadmin, "
            "created_at, last_login_at FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        conn.execute("COMMIT")
    finally:
        conn.close()
    return _success({"user": _user_public_row(row)})


async def delete_user_handler(req: web.Request) -> web.Response:
    """``DELETE /agent-mcp/api/router/users/<user_id>``.

    Cascades to ``sessions`` + ``project_membership`` via the
    ON DELETE CASCADE clauses on the FKs (set up in the Phase 1
    initial migration). 404 if the user isn't found — we don't
    want to no-op-delete, that masks typos in the dashboard URL.
    """
    _ensure_wave1a_schema()
    user_id = req.match_info["user_id"]
    conn = _connect()
    # Manual transaction: the last-sysadmin count check + DELETE must be
    # atomic so two racing deletes can't each remove the final two
    # sysadmins (BEGIN IMMEDIATE serialises; the loser is rejected).
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT is_sysadmin FROM users WHERE user_id = ?", (user_id,),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return _error(
                error=_ERROR_NOT_FOUND,
                message=f"unknown user_id: {user_id!r}",
                status=404,
            )
        # Deleting a sysadmin account is sysadmin-only, mirroring the
        # demote guard in edit_user_handler (clearing the is_sysadmin bit
        # is _forbid_sysadmin_write, 403). Without this, a delegate with
        # system.users.manage could DELETE a sysadmin it cannot DEMOTE —
        # delete would supersede the demote guard (AZ-R9-1).
        if row["is_sysadmin"] and not _caller_is_sysadmin(req):
            conn.execute("ROLLBACK")
            return _forbid_sysadmin_write(req)
        if row["is_sysadmin"] and _is_last_sysadmin(conn, user_id):
            conn.execute("ROLLBACK")
            return _last_sysadmin_error("delete")
        conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        conn.execute("COMMIT")
    finally:
        conn.close()
    return _success({"deleted": user_id})


# ── GROUPS handlers ────────────────────────────────────────────────


def _group_member_count(conn: sqlite3.Connection, group_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM group_membership WHERE group_id = ?",
        (group_id,),
    ).fetchone()[0]


async def list_groups_handler(req: web.Request) -> web.Response:
    """``GET /agent-mcp/api/router/groups``.

    Each row carries a denormalised ``member_count`` so the
    dashboard list view can render "Engineers (12 members)"
    without a per-group follow-up fetch."""
    _ensure_wave1a_schema()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT group_id, name, is_sysadmin, created_at "
            "FROM groups ORDER BY name"
        ).fetchall()
        out = [_group_public_row(r, _group_member_count(conn, r["group_id"])) for r in rows]
    finally:
        conn.close()
    return _success({"groups": out})


async def create_group_handler(req: web.Request) -> web.Response:
    """``POST /agent-mcp/api/router/groups``.

    Body: ``{name, is_sysadmin?}``. The ``is_sysadmin`` flag is
    accepted at create time so an operator can mint a
    sysadmin-tier group in one shot rather than create + PATCH.
    """
    _ensure_wave1a_schema()
    body = await _json_body(req)
    # PF-R8-1: reject a non-string ``name`` BEFORE ``.strip()`` (see
    # create_user_handler). ``allow_none=True`` defers the required-field
    # message to ``_validate_group_name``.
    name_err = _reject_non_str(body.get("name"), "name", allow_none=True)
    if name_err is not None:
        return _error(error=_ERROR_VALIDATION, message=name_err, status=400)
    name = (body.get("name") or "").strip()
    # PF-R13-1: strict boolean parse — a truthy non-bool (e.g. "false")
    # must not silently mint a sysadmin-flagged group.
    is_sysadmin, is_sysadmin_err = _parse_bool_field(
        body, "is_sysadmin", default=False,
    )
    if is_sysadmin_err is not None:
        return _error(
            error=_ERROR_VALIDATION, message=is_sysadmin_err, status=400,
        )
    # A sysadmin-flagged group confers sysadmin to its members, so
    # minting one is sysadmin-only (self-escalation defence).
    if is_sysadmin and not _caller_is_sysadmin(req):
        return _forbid_sysadmin_write(req)
    err = _validate_group_name(name)
    if err is not None:
        return _error(error=_ERROR_VALIDATION, message=err, status=400)
    group_id = secrets.token_hex(8)
    conn = _connect()
    try:
        try:
            conn.execute(
                "INSERT INTO groups (group_id, name, is_sysadmin, created_at) "
                "VALUES (?, ?, ?, ?)",
                (group_id, name, 1 if is_sysadmin else 0, _now_iso()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return _error(
                error=_ERROR_CONFLICT,
                message=f"group name {name!r} already exists",
                status=409,
            )
        row = conn.execute(
            "SELECT group_id, name, is_sysadmin, created_at "
            "FROM groups WHERE group_id = ?",
            (group_id,),
        ).fetchone()
    finally:
        conn.close()
    return _success({"group": _group_public_row(row, 0)}, status=201)


async def edit_group_handler(req: web.Request) -> web.Response:
    """``PATCH /agent-mcp/api/router/groups/<group_id>``.

    Partial update — name and/or is_sysadmin.
    """
    _ensure_wave1a_schema()
    group_id = req.match_info["group_id"]
    body = await _json_body(req)
    # Setting OR clearing the sysadmin bit is sysadmin-only.
    if "is_sysadmin" in body and not _caller_is_sysadmin(req):
        return _forbid_sysadmin_write(req)
    sets: list[str] = []
    params: list[Any] = []
    if "name" in body:
        # PF-R8-1: reject a non-string ``name`` BEFORE ``.strip()`` (see
        # create_user_handler). ``allow_none=True`` defers the
        # required-field message to ``_validate_group_name``.
        name_err = _reject_non_str(body["name"], "name", allow_none=True)
        if name_err is not None:
            return _error(
                error=_ERROR_VALIDATION, message=name_err, status=400,
            )
        new_name = (body["name"] or "").strip()
        err = _validate_group_name(new_name)
        if err is not None:
            return _error(
                error=_ERROR_VALIDATION, message=err, status=400,
            )
        sets.append("name = ?")
        params.append(new_name)
    if "is_sysadmin" in body:
        # PF-R13-1: strict boolean parse — a truthy non-bool (e.g.
        # "false") must not silently flip the group's sysadmin bit.
        is_sysadmin_val, is_sysadmin_err = _parse_bool_field(
            body, "is_sysadmin", default=False,
        )
        if is_sysadmin_err is not None:
            return _error(
                error=_ERROR_VALIDATION, message=is_sysadmin_err,
                status=400,
            )
        sets.append("is_sysadmin = ?")
        params.append(1 if is_sysadmin_val else 0)
    if not sets:
        return _error(
            error=_ERROR_VALIDATION,
            message="no editable fields supplied",
            status=400,
        )
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT 1 FROM groups WHERE group_id = ?", (group_id,),
        ).fetchone()
        if existing is None:
            return _error(
                error=_ERROR_NOT_FOUND,
                message=f"unknown group_id: {group_id!r}",
                status=404,
            )
        params.append(group_id)
        try:
            conn.execute(
                f"UPDATE groups SET {', '.join(sets)} WHERE group_id = ?",
                params,
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return _error(
                error=_ERROR_CONFLICT,
                message="group name already taken",
                status=409,
            )
        row = conn.execute(
            "SELECT group_id, name, is_sysadmin, created_at "
            "FROM groups WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        member_count = _group_member_count(conn, group_id)
    finally:
        conn.close()
    return _success({"group": _group_public_row(row, member_count)})


async def delete_group_handler(req: web.Request) -> web.Response:
    """``DELETE /agent-mcp/api/router/groups/<group_id>``.

    Cascades to ``group_membership`` (both the row where this
    group is the parent AND any rows where this group is the
    member) via the FK ON DELETE CASCADE.
    """
    _ensure_wave1a_schema()
    group_id = req.match_info["group_id"]
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT is_sysadmin FROM groups WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        if row is None:
            return _error(
                error=_ERROR_NOT_FOUND,
                message=f"unknown group_id: {group_id!r}",
                status=404,
            )
        # Deleting a sysadmin-flagged group is sysadmin-only, mirroring
        # the demote guard in edit_group_handler (clearing the group's
        # is_sysadmin bit is _forbid_sysadmin_write, 403). Without this, a
        # delegate with only system.groups.manage could DELETE a sysadmin
        # group it cannot DEMOTE — destroying the group-conferred sysadmin
        # grant to every member, superseding the demote guard (AZ-R10-1).
        if row["is_sysadmin"] and not _caller_is_sysadmin(req):
            return _forbid_sysadmin_write(req)
        conn.execute("DELETE FROM groups WHERE group_id = ?", (group_id,))
        conn.commit()
    finally:
        conn.close()
    return _success({"deleted": group_id})


# ── GROUP MEMBERS handlers ─────────────────────────────────────────


async def list_group_members_handler(req: web.Request) -> web.Response:
    """``GET /agent-mcp/api/router/groups/<group_id>/members``.

    Joins ``group_membership`` against ``users`` and ``groups`` so
    each member row carries a renderable label (``username`` for
    user members, ``name`` for group members) — the dashboard
    list never has to follow up with separate user / group
    fetches.
    """
    _ensure_wave1a_schema()
    group_id = req.match_info["group_id"]
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT 1 FROM groups WHERE group_id = ?", (group_id,),
        ).fetchone()
        if existing is None:
            return _error(
                error=_ERROR_NOT_FOUND,
                message=f"unknown group_id: {group_id!r}",
                status=404,
            )
        # LEFT JOINs let us pull username + group-name in one go.
        rows = conn.execute(
            """
            SELECT
                gm.member_user_id   AS user_id,
                gm.member_group_id  AS group_id,
                gm.added_at         AS added_at,
                u.username          AS username,
                g.name              AS name,
                g.is_sysadmin       AS member_group_is_sysadmin
            FROM group_membership gm
            LEFT JOIN users u ON gm.member_user_id = u.user_id
            LEFT JOIN groups g ON gm.member_group_id = g.group_id
            WHERE gm.group_id = ?
            ORDER BY COALESCE(u.username, g.name)
            """,
            (group_id,),
        ).fetchall()
    finally:
        conn.close()
    members = []
    for r in rows:
        d = dict(r)
        # Strip None-valued fields so the JSON shape is "either
        # user_id+username OR group_id+name", never the union.
        if d.get("user_id") is None:
            d.pop("user_id", None)
            d.pop("username", None)
        if d.get("group_id") is None:
            d.pop("group_id", None)
            d.pop("name", None)
            d.pop("member_group_is_sysadmin", None)
        members.append(d)
    return _success({"members": members})


async def add_group_member_handler(req: web.Request) -> web.Response:
    """``POST /agent-mcp/api/router/groups/<group_id>/members``.

    Body: exactly one of ``{user_id}`` / ``{group_id}``. Returns
    400 if both or neither are supplied (defence in depth before
    the DB CHECK fires).

    For a group-into-group edge, cycle detection runs BEFORE the
    insert, reusing ``group_resolver``'s reachability logic. The
    check-and-insert happen inside one ``BEGIN IMMEDIATE`` transaction
    so two concurrent adders can't each pass the check and then close a
    cycle between them — the immediate write-lock serialises them, so
    the second adder's check sees the first's edge and rejects (409).
    """
    _ensure_wave1a_schema()
    parent_group_id = req.match_info["group_id"]
    body = await _json_body(req)
    member_user_id = body.get("user_id")
    member_group_id = body.get("group_id")
    # PF-R7-1: reject structured JSON types before the write lock — a dict/list
    # here would otherwise reach the INSERT bind and raise ProgrammingError
    # (uncaught → 500). Each id is individually optional (exactly-one enforced
    # just below), so a None passes and is handled by that check.
    for _val, _field in ((member_user_id, "user_id"), (member_group_id, "group_id")):
        _err = _reject_non_str(_val, _field, allow_none=True)
        if _err is not None:
            return _error(error=_ERROR_VALIDATION, message=_err, status=400)
    if bool(member_user_id) == bool(member_group_id):
        return _error(
            error=_ERROR_VALIDATION,
            message="exactly one of user_id or group_id is required",
            status=400,
        )
    conn = _connect()
    # Manual transaction control so BEGIN IMMEDIATE / COMMIT / ROLLBACK
    # are fully under our hand (default isolation_level auto-manages
    # DML and would fight the explicit BEGIN).
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT 1 FROM groups WHERE group_id = ?",
                (parent_group_id,),
            ).fetchone()
            if existing is None:
                conn.execute("ROLLBACK")
                return _error(
                    error=_ERROR_NOT_FOUND,
                    message=f"unknown group_id: {parent_group_id!r}",
                    status=404,
                )
            # #288 round-2: adding a member to a (transitively) sysadmin
            # group confers sysadmin to that member via the resolver's
            # transitive closure — a sysadmin-grant in disguise. #288
            # locked SETTING is_sysadmin + CREATING a sysadmin group but
            # not JOINING one; close that vector here. Reserved for
            # sysadmins regardless of member kind (self or a nested group).
            if not _caller_is_sysadmin(
                req
            ) and store.group_is_transitively_sysadmin(
                parent_group_id, conn=conn,
            ):
                conn.execute("ROLLBACK")
                return _forbid_sysadmin_membership(req)
            # SEC round-4 (AZ-2) — same amplification class as AZ-1. The
            # sysadmin-FLAG join is blocked above, but a group carrying
            # elevated ``system.*`` capabilities confers them to a new
            # member just the same (an independent amplification path). A
            # non-sysadmin delegate must not add a member (themselves or a
            # group they control) into a group whose RESOLVED caps exceed
            # what the delegate already holds.
            if not _caller_is_sysadmin(req):
                inherited = _group_resolved_capabilities(
                    conn, parent_group_id,
                )
                lacked = _caps_caller_lacks(req, inherited)
                if lacked:
                    conn.execute("ROLLBACK")
                    return _forbid_cap_amplification(req, lacked)
            # SEC round-6 (AZ-R6-1) — third amplification sibling of the two
            # above. The sysadmin-FLAG and system-CAPABILITY joins are blocked,
            # but a group that is a PROJECT MEMBER confers that project's role
            # on every member via ``group_resolver.resolve_user_project_role``
            # (the resolver the ``/api/<project>/`` data middleware gates on).
            # So joining such a group turns table-management authority into
            # cross-tenant DATA authority. A non-sysadmin delegate must not add
            # a member (themselves OR a nested group they control — both
            # transitively inherit) into a group whose inherited project roles
            # exceed what the delegate already holds on those projects. Reuse
            # the round-5 role-rank logic per conferred (project, role).
            if not _caller_is_sysadmin(req):
                for project, role in store.group_resolved_project_roles(
                    parent_group_id, conn=conn,
                ).items():
                    denied = _membership_grant_denied(req, project, role)
                    if denied is not None:
                        conn.execute("ROLLBACK")
                        return denied
            if member_group_id is not None and store.would_create_cycle(
                parent_group_id, member_group_id, conn=conn,
            ):
                conn.execute("ROLLBACK")
                return _error(
                    error=_ERROR_CONFLICT,
                    message=(
                        f"adding group {member_group_id!r} as a member of "
                        f"{parent_group_id!r} would close a cycle in the "
                        "membership DAG"
                    ),
                    status=409,
                )
            # Route through the single group_membership writer (arch R2
            # #1b), enlisted in this BEGIN IMMEDIATE via ``conn=``. The
            # amplification/cycle guards above already ran on this same
            # connection, so the store's own cycle check is a redundant
            # no-op here; ``sqlite3.IntegrityError`` from the idempotency
            # UNIQUE index still propagates to the handler below → 409.
            store.add_group_member(
                parent_group_id,
                member_user_id=member_user_id,
                member_group_id=member_group_id,
                conn=conn,
            )
            conn.execute("COMMIT")
        except sqlite3.IntegrityError as e:
            # UNIQUE → the membership already exists (idempotency guard,
            # router migration 0006): surface as a 409 conflict, mirroring
            # add_project_membership. FK (unknown member id) / CHECK
            # violations stay a 400 validation error.
            conn.execute("ROLLBACK")
            if "UNIQUE" in str(e).upper():
                return _error(
                    error=_ERROR_CONFLICT,
                    message=(
                        "membership already exists for this "
                        "group + member"
                    ),
                    status=409,
                )
            # SD-R6-2: a raw IntegrityError text (FK/CHECK) discloses the SQL
            # constraint + schema. Log server-side, return a generic message.
            logger.warning("add_group_member insert failed: %s", e)
            return _error(
                error=_ERROR_VALIDATION,
                message="could not add member",
                status=400,
            )
    finally:
        conn.close()
    member: dict[str, Any] = {}
    if member_user_id:
        member["user_id"] = member_user_id
    if member_group_id:
        member["group_id"] = member_group_id
    return _success({"member": member}, status=201)


async def remove_group_member_handler(req: web.Request) -> web.Response:
    """``DELETE /agent-mcp/api/router/groups/<group_id>/members/<member_id>``.

    ``<member_id>`` is matched against BOTH ``member_user_id`` and
    ``member_group_id`` — the surrogate is just an opaque id, the
    handler doesn't need to know which kind it is.
    """
    _ensure_wave1a_schema()
    parent_group_id = req.match_info["group_id"]
    member_id = req.match_info["member_id"]
    conn = _connect()
    try:
        # AZ-R12-1 (revoke mirror of add_group_member_handler's three
        # amplification guards): removing a member STRIPS the authority the
        # parent group confers on that member — the symmetric operation of
        # adding them. A non-sysadmin delegate holding only
        # ``system.groups.manage`` must not strip authority they could never
        # GRANT, or the revoke path supersedes the guarded add path. Deny
        # (regardless of member kind) when the parent group confers, via the
        # resolver's transitive closure, any of:
        #   * sysadmin (the group's transitive is_sysadmin flag),
        #   * a ``system.*`` capability the delegate lacks, or
        #   * a project role above the delegate's own.
        if not _caller_is_sysadmin(req):
            if store.group_is_transitively_sysadmin(parent_group_id, conn=conn):
                return _forbid_sysadmin_membership(req)
            inherited = _group_resolved_capabilities(conn, parent_group_id)
            lacked = _caps_caller_lacks(req, inherited)
            if lacked:
                return _forbid_cap_amplification(req, lacked)
            for project, role in store.group_resolved_project_roles(
                parent_group_id, conn=conn,
            ).items():
                denied = _membership_grant_denied(req, project, role)
                if denied is not None:
                    return denied
        cur = conn.execute(
            "DELETE FROM group_membership "
            "WHERE group_id = ? AND "
            "(member_user_id = ? OR member_group_id = ?)",
            (parent_group_id, member_id, member_id),
        )
        if cur.rowcount == 0:
            return _error(
                error=_ERROR_NOT_FOUND,
                message=(
                    f"no membership for member {member_id!r} in "
                    f"group {parent_group_id!r}"
                ),
                status=404,
            )
        conn.commit()
    finally:
        conn.close()
    return _success({"removed": member_id})


# ── PROJECT MEMBERSHIP handlers ────────────────────────────────────


def _split_membership_id(membership_id: str) -> tuple[str, str] | None:
    """Parse ``u:<id>`` / ``g:<id>`` → (kind, id). Returns None
    on a malformed surrogate."""
    if membership_id.startswith("u:"):
        return ("user", membership_id[2:])
    if membership_id.startswith("g:"):
        return ("group", membership_id[2:])
    return None


def _project_exists(name: str) -> bool:
    """Defer to the router's project registry."""
    try:
        from . import app as _app
        return name in _app._projects_dict()
    except Exception:  # pragma: no cover - defensive
        return False


async def list_project_memberships_handler(req: web.Request) -> web.Response:
    """``GET /agent-mcp/api/router/projects/<name>/memberships``.

    Joins on ``users`` + ``groups`` so each row carries a
    renderable label. Synthetic ``membership_id`` (``u:<id>`` /
    ``g:<id>``) makes per-row PATCH/DELETE addressable.
    """
    _ensure_wave1a_schema()
    project_name = req.match_info["name"]
    if not _project_exists(project_name):
        return _error(
            error=_ERROR_NOT_FOUND,
            message=f"unknown project: {project_name!r}",
            status=404,
        )
    # R3-F1: scope the READ like its mutation siblings (AZ-R5-1 swept the
    # three membership WRITE handlers via ``_membership_grant_denied`` but
    # missed this LIST — it stayed on the coarse deployment-wide
    # ``system.projects.manage`` gate alone). Without per-project scoping a
    # non-sysadmin delegate holding that cap could read the FULL roster of a
    # project hidden from their own ``/projects`` + ``/overview`` views —
    # cross-tenant disclosure. Admit only a sysadmin OR a caller with a
    # resolved role on this project; otherwise return the SAME 404
    # ``unknown_project`` a non-member sees from the data middleware
    # (auth_middleware PF-1) so "exists but I'm not a member" is
    # indistinguishable from "doesn't exist" — the 200-roster/404
    # differential was a project-existence oracle.
    principal = req.get("principal")
    caller_id = getattr(principal, "user_id", None) if principal else None
    if not _caller_is_sysadmin(req) and (
        caller_id is None
        or store.resolve_user_project_role(caller_id, project_name) is None
    ):
        return _error(
            error=_ERROR_NOT_FOUND,
            message=f"unknown project: {project_name!r}",
            status=404,
        )
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT
                pm.user_id      AS user_id,
                pm.group_id     AS group_id,
                pm.role         AS role,
                u.username      AS username,
                g.name          AS group_name
            FROM project_membership pm
            LEFT JOIN users u ON pm.user_id = u.user_id
            LEFT JOIN groups g ON pm.group_id = g.group_id
            WHERE pm.project_name = ?
            ORDER BY COALESCE(u.username, g.name)
            """,
            (project_name,),
        ).fetchall()
    finally:
        conn.close()
    memberships = []
    for r in rows:
        d = dict(r)
        # Build membership_id surrogate. user rows always have
        # user_id; group rows always have group_id. Pre-Phase-3
        # rows have user_id and a NULL group_id (defaulted to
        # role='operator' by the migration).
        if d.get("user_id"):
            d["membership_id"] = f"u:{d['user_id']}"
            d.pop("group_id", None)
            d.pop("group_name", None)
        elif d.get("group_id"):
            d["membership_id"] = f"g:{d['group_id']}"
            d.pop("user_id", None)
            d.pop("username", None)
        memberships.append(d)
    return _success({"memberships": memberships})


async def add_project_membership_handler(req: web.Request) -> web.Response:
    """``POST /agent-mcp/api/router/projects/<name>/memberships``.

    Body: exactly one of ``{user_id, role?}`` / ``{group_id, role?}``.
    Role defaults to ``operator``; ``viewer`` is the only other
    accepted value per the schema CHECK.
    """
    _ensure_wave1a_schema()
    project_name = req.match_info["name"]
    # R7-F1: close the project-existence ORACLE. Routing through the shared
    # ``_deny_cross_tenant_project_read`` (sysadmin-admit → non-member gets the
    # SAME 404 ``unknown_project`` for an existing-hidden project as for a
    # nonexistent one) makes "exists but I'm not a member" indistinguishable
    # from "doesn't exist". Previously ``_project_exists``→404 then
    # ``_membership_grant_denied``→403 gave a non-member a 403-vs-404
    # differential (the 403 body leaked the project name + the caller's own
    # held role) — the missed WRITE-side sibling of the R6-F2 sweep (#478). The
    # ``_membership_grant_denied`` role-rank 403 below now only ever fires for
    # an actual MEMBER (you can't confer a role above your own).
    from .admin_api import _deny_cross_tenant_project_read

    denied = _deny_cross_tenant_project_read(req, project_name)
    if denied is not None:
        return denied
    body = await _json_body(req)
    user_id = body.get("user_id")
    group_id = body.get("group_id")
    role = body.get("role", "operator")
    # PF-R7-1: reject structured JSON types before the INSERT bind (a dict/list
    # would raise ProgrammingError, uncaught → 500). Each id is individually
    # optional (exactly-one enforced just below).
    for _val, _field in ((user_id, "user_id"), (group_id, "group_id")):
        _err = _reject_non_str(_val, _field, allow_none=True)
        if _err is not None:
            return _error(error=_ERROR_VALIDATION, message=_err, status=400)
    if bool(user_id) == bool(group_id):
        return _error(
            error=_ERROR_VALIDATION,
            message="exactly one of user_id or group_id is required",
            status=400,
        )
    err = _validate_role(role)
    if err is not None:
        return _error(error=_ERROR_VALIDATION, message=err, status=400)
    denied = _membership_grant_denied(req, project_name, role)
    if denied is not None:
        return denied
    conn = _connect()
    try:
        try:
            # Route both grant shapes through the single project_membership
            # writer (arch R2 #1b), enlisted in this connection via
            # ``conn=``. Plain INSERT (``or_ignore`` defaults False) so a
            # duplicate raises ``IntegrityError`` → the 409 below.
            if user_id:
                store.add_project_membership(
                    project_name, user_id=user_id, role=role, conn=conn,
                )
            else:
                store.add_project_membership(
                    project_name, group_id=group_id, role=role, conn=conn,
                )
            conn.commit()
        except sqlite3.IntegrityError as e:
            # SD-R6-2: don't reflect the raw IntegrityError (SQL/schema
            # disclosure). Log server-side, return a generic message.
            logger.warning("add_project_membership insert failed: %s", e)
            return _error(
                error=_ERROR_CONFLICT,
                message="could not add membership",
                status=409,
            )
    finally:
        conn.close()
    out: dict[str, Any] = {"role": role}
    if user_id:
        out["user_id"] = user_id
        out["membership_id"] = f"u:{user_id}"
    if group_id:
        out["group_id"] = group_id
        out["membership_id"] = f"g:{group_id}"
    return _success({"membership": out}, status=201)


async def change_project_membership_role_handler(
    req: web.Request,
) -> web.Response:
    """``PATCH /agent-mcp/api/router/projects/<name>/memberships/<membership_id>``.

    Body: ``{role}``. The only mutable field for Wave 1b. Use
    DELETE + POST to swap a user for a group on the same project.
    """
    _ensure_wave1a_schema()
    project_name = req.match_info["name"]
    membership_id = req.match_info["membership_id"]
    # R7-F1: close the project-existence oracle — uniform 404 for a non-member
    # (existing-hidden ≡ nonexistent). See add_project_membership_handler.
    from .admin_api import _deny_cross_tenant_project_read

    denied = _deny_cross_tenant_project_read(req, project_name)
    if denied is not None:
        return denied
    parsed = _split_membership_id(membership_id)
    if parsed is None:
        return _error(
            error=_ERROR_VALIDATION,
            message=(
                f"membership_id must be 'u:<id>' or 'g:<id>'; got "
                f"{membership_id!r}"
            ),
            status=400,
        )
    kind, target_id = parsed
    body = await _json_body(req)
    role = body.get("role")
    if role is None:
        return _error(
            error=_ERROR_VALIDATION,
            message="role is required",
            status=400,
        )
    err = _validate_role(role)
    if err is not None:
        return _error(error=_ERROR_VALIDATION, message=err, status=400)
    denied = _membership_grant_denied(req, project_name, role)
    if denied is not None:
        return denied
    conn = _connect()
    try:
        # AZ-R12-1 (revoke mirror) — a PATCH changes a role, so like the
        # cap REPLACE it must guard the SYMMETRIC delta, not just the NEW
        # role. Guarding only the new role lets a viewer-delegate DOWNGRADE
        # an operator to viewer: the new role (viewer) is within their
        # authority, but the operator role they STRIP is not. That is the
        # same cross-tenant revoke as the DELETE path (a downgrade-to-viewer
        # is a near-equivalent lockout of operator-tier data access), so it
        # would otherwise be a trivial bypass of the DELETE guard. Look up
        # the EXISTING role and apply the same grant guard to it — the
        # caller must be authorised for BOTH the role they set and the role
        # they remove. 404 (unchanged) when no such row.
        if kind == "user":
            existing = conn.execute(
                "SELECT role FROM project_membership "
                "WHERE project_name = ? AND user_id = ?",
                (project_name, target_id),
            ).fetchone()
        else:
            existing = conn.execute(
                "SELECT role FROM project_membership "
                "WHERE project_name = ? AND group_id = ?",
                (project_name, target_id),
            ).fetchone()
        if existing is None:
            return _error(
                error=_ERROR_NOT_FOUND,
                message=(
                    f"no membership for {membership_id!r} in "
                    f"project {project_name!r}"
                ),
                status=404,
            )
        denied = _membership_grant_denied(req, project_name, existing["role"])
        if denied is not None:
            return denied
        if kind == "user":
            conn.execute(
                "UPDATE project_membership SET role = ? "
                "WHERE project_name = ? AND user_id = ?",
                (role, project_name, target_id),
            )
        else:
            conn.execute(
                "UPDATE project_membership SET role = ? "
                "WHERE project_name = ? AND group_id = ?",
                (role, project_name, target_id),
            )
        conn.commit()
    finally:
        conn.close()
    out: dict[str, Any] = {
        "role": role,
        "membership_id": membership_id,
    }
    if kind == "user":
        out["user_id"] = target_id
    else:
        out["group_id"] = target_id
    return _success({"membership": out})


async def list_group_capabilities_handler(
    req: web.Request,
) -> web.Response:
    """``GET /agent-mcp/api/router/groups/<group_id>/capabilities``.

    Wave 9 PR 5 — sysadmin-facing UI lists the capabilities currently
    granted to ``<group_id>``. Reads through
    :func:`agent_mcp.repositories.group_capability_repository.fetch`
    so the wire shape matches what the resolver actually sees when it
    rolls a Principal up to a capability set.

    Response: ``{"success": true, "capabilities": ["tasks.create", ...]}``.
    The list is alphabetically sorted to give the dashboard a stable
    render order without needing a second sort pass on the client.
    """
    _ensure_wave1a_schema()
    group_id = req.match_info["group_id"]
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT 1 FROM groups WHERE group_id = ?", (group_id,),
        ).fetchone()
        if existing is None:
            return _error(
                error=_ERROR_NOT_FOUND,
                message=f"unknown group_id: {group_id!r}",
                status=404,
            )
    finally:
        conn.close()

    from ..repositories import group_capability_repository as _gcap

    caps = sorted(_gcap.fetch(group_id))
    return _success({"capabilities": caps})


async def replace_group_capabilities_handler(
    req: web.Request,
) -> web.Response:
    """``PUT /agent-mcp/api/router/groups/<group_id>/capabilities``.

    Wave 9 PR 5 — sysadmin sets the COMPLETE new cap list for the
    group. Body shape: ``{"capabilities": ["tasks.create", ...]}``.
    The handler validates every cap string is a member of
    :data:`agent_mcp.core.capabilities.KNOWN_CAPABILITIES` BEFORE
    touching the DB; unknown caps fail closed with a 400 carrying
    the ``unknown_capability`` error code so the dashboard can
    surface "this cap string isn't real, did you typo it?".

    The replace is atomic (DELETE-then-INSERT inside one transaction
    per :func:`group_capability_repository.replace`); a malformed
    body never leaves the group in a half-written state.
    """
    _ensure_wave1a_schema()
    group_id = req.match_info["group_id"]
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT 1 FROM groups WHERE group_id = ?", (group_id,),
        ).fetchone()
        if existing is None:
            return _error(
                error=_ERROR_NOT_FOUND,
                message=f"unknown group_id: {group_id!r}",
                status=404,
            )
    finally:
        conn.close()

    body = await _json_body(req)
    raw_caps = body.get("capabilities")
    if not isinstance(raw_caps, list):
        return _error(
            error=_ERROR_VALIDATION,
            message="body must be {\"capabilities\": [...]} with a JSON array",
            status=400,
        )
    # Validate types + drop duplicates (preserving caller order so the
    # error message in the unknown-cap case quotes the first offender
    # the operator typed).
    seen: set[str] = set()
    ordered: list[str] = []
    for entry in raw_caps:
        if not isinstance(entry, str):
            return _error(
                error=_ERROR_VALIDATION,
                message=(
                    f"capabilities entries must be strings; got "
                    f"{type(entry).__name__}"
                ),
                status=400,
            )
        if entry in seen:
            continue
        seen.add(entry)
        ordered.append(entry)

    from ..core.capabilities import KNOWN_CAPABILITIES

    unknown = [c for c in ordered if c not in KNOWN_CAPABILITIES]
    if unknown:
        return _error(
            error="unknown_capability",
            message=(
                f"unknown capability string(s): "
                f"{', '.join(repr(c) for c in unknown)}"
            ),
            status=400,
            extra={"unknown": unknown},
        )

    from ..repositories import group_capability_repository as _gcap

    # SEC round-4 (AZ-1) — capability-grant privilege amplification
    # (confused deputy). The route admits any caller holding
    # ``system.groups.capabilities.manage``, but that management cap
    # alone must NOT let a non-sysadmin grant their own group the
    # sysadmin-equivalent ``system.*`` management caps and self-amplify.
    # A non-sysadmin may only grant caps they already hold; a sysadmin
    # may grant anything in KNOWN_CAPABILITIES (validated above).
    #
    # AZ-R12-1 (revoke mirror) — the guard must cover the SYMMETRIC
    # DIFFERENCE, not just the NEW list. This is an atomic REPLACE, so a
    # shrinking PUT (or ``[]``) REMOVES caps; a non-sysadmin must not STRIP
    # a cap they don't hold any more than they may GRANT one — otherwise
    # they could revoke authority they could never confer. Delta = caps
    # added (new − current) ∪ caps removed (current − new).
    current = _gcap.fetch(group_id)
    new_caps = frozenset(ordered)
    delta = (new_caps - current) | (current - new_caps)
    lacked = _caps_caller_lacks(req, delta)
    if lacked:
        return _forbid_cap_amplification(req, lacked)

    _gcap.replace(group_id, new_caps)
    # Re-read so the response body matches what a subsequent GET would
    # return (sorted, de-duped) — the dashboard uses this to confirm
    # the round-trip.
    return _success({"capabilities": sorted(_gcap.fetch(group_id))})


async def delete_project_membership_handler(
    req: web.Request,
) -> web.Response:
    """``DELETE /agent-mcp/api/router/projects/<name>/memberships/<membership_id>``.

    Removes the (project, user|group) tuple. 404 if no such row.
    """
    _ensure_wave1a_schema()
    project_name = req.match_info["name"]
    membership_id = req.match_info["membership_id"]
    # R7-F1: close the project-existence oracle — uniform 404 for a non-member
    # (existing-hidden ≡ nonexistent). See add_project_membership_handler.
    from .admin_api import _deny_cross_tenant_project_read

    denied = _deny_cross_tenant_project_read(req, project_name)
    if denied is not None:
        return denied
    parsed = _split_membership_id(membership_id)
    if parsed is None:
        return _error(
            error=_ERROR_VALIDATION,
            message=(
                f"membership_id must be 'u:<id>' or 'g:<id>'; got "
                f"{membership_id!r}"
            ),
            status=400,
        )
    kind, target_id = parsed
    conn = _connect()
    try:
        # AZ-R12-1 (revoke mirror of add_project_membership_handler's
        # ``_membership_grant_denied`` guard): DELETE is gated only by the
        # delegable ``system.projects.manage`` cap, but revoking a
        # membership row is cross-tenant DATA authority just like granting
        # one — a non-sysadmin delegate with NO role on the project could
        # lock a victim out (or a viewer-delegate could strip an operator).
        # Look up the role being revoked and apply the SAME grant guard so
        # the DELETE path can't supersede the ADD-side check: a caller may
        # revoke only a role at or below their own, and only on a project
        # they hold a membership on. 404 (unchanged) when no such row.
        if kind == "user":
            existing = conn.execute(
                "SELECT role FROM project_membership "
                "WHERE project_name = ? AND user_id = ?",
                (project_name, target_id),
            ).fetchone()
        else:
            existing = conn.execute(
                "SELECT role FROM project_membership "
                "WHERE project_name = ? AND group_id = ?",
                (project_name, target_id),
            ).fetchone()
        if existing is None:
            return _error(
                error=_ERROR_NOT_FOUND,
                message=(
                    f"no membership for {membership_id!r} in "
                    f"project {project_name!r}"
                ),
                status=404,
            )
        denied = _membership_grant_denied(req, project_name, existing["role"])
        if denied is not None:
            return denied
        if kind == "user":
            conn.execute(
                "DELETE FROM project_membership "
                "WHERE project_name = ? AND user_id = ?",
                (project_name, target_id),
            )
        else:
            conn.execute(
                "DELETE FROM project_membership "
                "WHERE project_name = ? AND group_id = ?",
                (project_name, target_id),
            )
        conn.commit()
    finally:
        conn.close()
    return _success({"removed": membership_id})


# ── Route registration ─────────────────────────────────────────────


def register_admin_users_routes(app: web.Application) -> None:
    """Wire every users/groups/memberships route into ``app``.

    Called from ``router.app.make_app()`` alongside the existing
    ``admin_api.register_admin_routes``. Each handler is wrapped
    with ``_rest_gated`` so the same Accept-header gate (PR-A)
    applies as for the projects collection.

    Phase 3 Wave 2 (v5.0.69): every mutating handler in this module
    is additionally wrapped with a system-perm gate. The system
    perm matrix reserves user CRUD, group CRUD, and project-
    membership management for sysadmins; non-sysadmin operators
    fall through to a 403 with the standard envelope shape.

    SECURITY (viewer-read-gating finding 2, 2026-07-08): the
    collection ``GET`` reads (``list_users`` / ``list_groups`` /
    ``list_group_members`` / ``list_project_memberships``) are gated
    on the SAME capability as their sibling mutations. The Wave 1b
    "reads stay open to any logged-in operator" stance leaked the
    full identity matrix — every user + email + is_sysadmin flag,
    every group, every membership — to a plain project *viewer* who
    hit the router API directly. These reads expose exactly the data
    the mutations manage, so a caller who may not mutate the matrix
    must not enumerate it either. Sysadmins still admit via the
    wildcard; an operator delegated the cap via a group admits too.

    Wave 9 PR 4 (prancy-napping-pie): each mutating route moved
    from ``require_sysadmin`` to a capability-shaped gate. The
    cap per resource family:

      * user CRUD → ``system.users.manage``
      * group CRUD + group-member CRUD → ``system.groups.manage``
      * project-membership CRUD → ``system.projects.manage``

    Sysadmins still admit unconditionally (their cap set is the
    wildcard); the cap shape ALSO lets a sysadmin grant the cap to
    a delegated group via the Wave 9 PR 5 dashboard UI without
    promoting the operator to sysadmin.
    """
    from . import app as _app
    from .perm_gates import require_capability

    gated = _app._rest_gated
    users_gate = require_capability("system.users.manage")
    groups_gate = require_capability("system.groups.manage")
    group_caps_gate = require_capability("system.groups.capabilities.manage")
    projects_gate = require_capability("system.projects.manage")

    # Users
    app.router.add_get(
        "/agent-mcp/api/router/users",
        gated(users_gate(list_users_handler)),
    )
    app.router.add_post(
        "/agent-mcp/api/router/users",
        gated(users_gate(create_user_handler)),
    )
    app.router.add_patch(
        "/agent-mcp/api/router/users/{user_id}",
        gated(users_gate(edit_user_handler)),
    )
    app.router.add_delete(
        "/agent-mcp/api/router/users/{user_id}",
        gated(users_gate(delete_user_handler)),
    )

    # Groups
    app.router.add_get(
        "/agent-mcp/api/router/groups",
        gated(groups_gate(list_groups_handler)),
    )
    app.router.add_post(
        "/agent-mcp/api/router/groups",
        gated(groups_gate(create_group_handler)),
    )
    app.router.add_patch(
        "/agent-mcp/api/router/groups/{group_id}",
        gated(groups_gate(edit_group_handler)),
    )
    app.router.add_delete(
        "/agent-mcp/api/router/groups/{group_id}",
        gated(groups_gate(delete_group_handler)),
    )

    # Group members
    app.router.add_get(
        "/agent-mcp/api/router/groups/{group_id}/members",
        gated(groups_gate(list_group_members_handler)),
    )
    app.router.add_post(
        "/agent-mcp/api/router/groups/{group_id}/members",
        gated(groups_gate(add_group_member_handler)),
    )
    app.router.add_delete(
        "/agent-mcp/api/router/groups/{group_id}/members/{member_id}",
        gated(groups_gate(remove_group_member_handler)),
    )

    # Group capabilities — Wave 9 PR 5 (gated by the matching cap
    # ``system.groups.capabilities.manage``). PR 5 originally inlined
    # ``require_sysadmin`` here with a TODO to swap to the cap-shaped
    # decorator once Wave 9 PR 4 landed. PR 4 has landed (the
    # ``require_capability`` aiohttp wrapper is the canonical
    # router-side gate now) so we use the cap directly — sysadmins
    # still admit via the wildcard short-circuit in
    # :meth:`Principal.has_capability`.
    app.router.add_get(
        "/agent-mcp/api/router/groups/{group_id}/capabilities",
        gated(group_caps_gate(list_group_capabilities_handler)),
    )
    app.router.add_put(
        "/agent-mcp/api/router/groups/{group_id}/capabilities",
        gated(group_caps_gate(replace_group_capabilities_handler)),
    )

    # Project memberships
    app.router.add_get(
        "/agent-mcp/api/router/projects/{name}/memberships",
        gated(projects_gate(list_project_memberships_handler)),
    )
    app.router.add_post(
        "/agent-mcp/api/router/projects/{name}/memberships",
        gated(projects_gate(add_project_membership_handler)),
    )
    app.router.add_patch(
        "/agent-mcp/api/router/projects/{name}/memberships/{membership_id}",
        gated(projects_gate(change_project_membership_role_handler)),
    )
    app.router.add_delete(
        "/agent-mcp/api/router/projects/{name}/memberships/{membership_id}",
        gated(projects_gate(delete_project_membership_handler)),
    )


__all__ = ["register_admin_users_routes"]
