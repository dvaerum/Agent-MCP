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
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from . import identity


logger = logging.getLogger(__name__)


# ── Error codes (extended from app._ERROR_*) ───────────────────────


_ERROR_VALIDATION = "validation_error"
_ERROR_NOT_FOUND = "not_found"
_ERROR_CONFLICT = "conflict"
_ERROR_INTERNAL = "internal_error"


_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_GROUP_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_PASSWORD_MIN_LENGTH = 8


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
    except json.JSONDecodeError as exc:
        raise web.HTTPBadRequest(
            text=json.dumps({
                "success": False, "error": _ERROR_VALIDATION,
                "message": f"request body is not valid JSON: {exc.msg}",
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


def _connect() -> sqlite3.Connection:
    """Open a router.db connection with FK + row factory enabled.

    Local helper rather than reusing ``identity._connect`` (a
    private context-manager) so the handlers can hold the
    connection across multiple statements when needed (e.g. an
    insert-then-select). Callers MUST close the connection — use
    ``try / finally`` or wrap with ``with``.
    """
    db_path = identity.get_router_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


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


def _validate_password(pw: str) -> str | None:
    if not pw or not isinstance(pw, str):
        return "password is required"
    if len(pw) < _PASSWORD_MIN_LENGTH:
        return (
            f"password must be at least {_PASSWORD_MIN_LENGTH} characters"
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
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    email = body.get("email")
    is_sysadmin = bool(body.get("is_sysadmin", False))

    for err in (_validate_username(username), _validate_password(password)):
        if err is not None:
            return _error(
                error=_ERROR_VALIDATION, message=err, status=400,
            )

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
    sets: list[str] = []
    params: list[Any] = []
    if "is_sysadmin" in body:
        sets.append("is_sysadmin = ?")
        params.append(1 if bool(body["is_sysadmin"]) else 0)
    if "email" in body:
        sets.append("email = ?")
        params.append(body["email"])
    if not sets:
        return _error(
            error=_ERROR_VALIDATION,
            message="no editable fields supplied",
            status=400,
        )
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT 1 FROM users WHERE user_id = ?", (user_id,),
        ).fetchone()
        if existing is None:
            return _error(
                error=_ERROR_NOT_FOUND,
                message=f"unknown user_id: {user_id!r}",
                status=404,
            )
        params.append(user_id)
        conn.execute(
            f"UPDATE users SET {', '.join(sets)} WHERE user_id = ?",
            params,
        )
        conn.commit()
        row = conn.execute(
            "SELECT user_id, username, email, is_sysadmin, "
            "created_at, last_login_at FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
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
    try:
        cur = conn.execute(
            "DELETE FROM users WHERE user_id = ?", (user_id,),
        )
        if cur.rowcount == 0:
            return _error(
                error=_ERROR_NOT_FOUND,
                message=f"unknown user_id: {user_id!r}",
                status=404,
            )
        conn.commit()
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
    name = (body.get("name") or "").strip()
    is_sysadmin = bool(body.get("is_sysadmin", False))
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
    sets: list[str] = []
    params: list[Any] = []
    if "name" in body:
        new_name = (body["name"] or "").strip()
        err = _validate_group_name(new_name)
        if err is not None:
            return _error(
                error=_ERROR_VALIDATION, message=err, status=400,
            )
        sets.append("name = ?")
        params.append(new_name)
    if "is_sysadmin" in body:
        sets.append("is_sysadmin = ?")
        params.append(1 if bool(body["is_sysadmin"]) else 0)
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
        cur = conn.execute(
            "DELETE FROM groups WHERE group_id = ?", (group_id,),
        )
        if cur.rowcount == 0:
            return _error(
                error=_ERROR_NOT_FOUND,
                message=f"unknown group_id: {group_id!r}",
                status=404,
            )
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
    the DB CHECK fires). Cycle detection is left to Wave 1a's
    helper — calling code can validate via that helper once it
    lands; for now we accept whatever the operator supplies and
    rely on the FK to reject unknown ids.
    """
    _ensure_wave1a_schema()
    parent_group_id = req.match_info["group_id"]
    body = await _json_body(req)
    member_user_id = body.get("user_id")
    member_group_id = body.get("group_id")
    if bool(member_user_id) == bool(member_group_id):
        return _error(
            error=_ERROR_VALIDATION,
            message="exactly one of user_id or group_id is required",
            status=400,
        )
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT 1 FROM groups WHERE group_id = ?", (parent_group_id,),
        ).fetchone()
        if existing is None:
            return _error(
                error=_ERROR_NOT_FOUND,
                message=f"unknown group_id: {parent_group_id!r}",
                status=404,
            )
        try:
            conn.execute(
                "INSERT INTO group_membership "
                "(group_id, member_user_id, member_group_id, added_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    parent_group_id, member_user_id, member_group_id,
                    _now_iso(),
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError as e:
            # FK violation (unknown member id) or CHECK violation.
            return _error(
                error=_ERROR_VALIDATION,
                message=f"could not add member: {e}",
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
    if not _project_exists(project_name):
        return _error(
            error=_ERROR_NOT_FOUND,
            message=f"unknown project: {project_name!r}",
            status=404,
        )
    body = await _json_body(req)
    user_id = body.get("user_id")
    group_id = body.get("group_id")
    role = body.get("role", "operator")
    if bool(user_id) == bool(group_id):
        return _error(
            error=_ERROR_VALIDATION,
            message="exactly one of user_id or group_id is required",
            status=400,
        )
    err = _validate_role(role)
    if err is not None:
        return _error(error=_ERROR_VALIDATION, message=err, status=400)
    conn = _connect()
    try:
        try:
            if user_id:
                conn.execute(
                    "INSERT INTO project_membership "
                    "(project_name, user_id, role) VALUES (?, ?, ?)",
                    (project_name, user_id, role),
                )
            else:
                conn.execute(
                    "INSERT INTO project_membership "
                    "(project_name, user_id, group_id, role) "
                    "VALUES (?, NULL, ?, ?)",
                    (project_name, group_id, role),
                )
            conn.commit()
        except sqlite3.IntegrityError as e:
            return _error(
                error=_ERROR_CONFLICT,
                message=f"could not add membership: {e}",
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
    if not _project_exists(project_name):
        return _error(
            error=_ERROR_NOT_FOUND,
            message=f"unknown project: {project_name!r}",
            status=404,
        )
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
    conn = _connect()
    try:
        if kind == "user":
            cur = conn.execute(
                "UPDATE project_membership SET role = ? "
                "WHERE project_name = ? AND user_id = ?",
                (role, project_name, target_id),
            )
        else:
            cur = conn.execute(
                "UPDATE project_membership SET role = ? "
                "WHERE project_name = ? AND group_id = ?",
                (role, project_name, target_id),
            )
        if cur.rowcount == 0:
            return _error(
                error=_ERROR_NOT_FOUND,
                message=(
                    f"no membership for {membership_id!r} in "
                    f"project {project_name!r}"
                ),
                status=404,
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


async def delete_project_membership_handler(
    req: web.Request,
) -> web.Response:
    """``DELETE /agent-mcp/api/router/projects/<name>/memberships/<membership_id>``.

    Removes the (project, user|group) tuple. 404 if no such row.
    """
    _ensure_wave1a_schema()
    project_name = req.match_info["name"]
    membership_id = req.match_info["membership_id"]
    if not _project_exists(project_name):
        return _error(
            error=_ERROR_NOT_FOUND,
            message=f"unknown project: {project_name!r}",
            status=404,
        )
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
        if kind == "user":
            cur = conn.execute(
                "DELETE FROM project_membership "
                "WHERE project_name = ? AND user_id = ?",
                (project_name, target_id),
            )
        else:
            cur = conn.execute(
                "DELETE FROM project_membership "
                "WHERE project_name = ? AND group_id = ?",
                (project_name, target_id),
            )
        if cur.rowcount == 0:
            return _error(
                error=_ERROR_NOT_FOUND,
                message=(
                    f"no membership for {membership_id!r} in "
                    f"project {project_name!r}"
                ),
                status=404,
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
    """
    from . import app as _app

    gated = _app._rest_gated

    # Users
    app.router.add_get(
        "/agent-mcp/api/router/users", gated(list_users_handler),
    )
    app.router.add_post(
        "/agent-mcp/api/router/users", gated(create_user_handler),
    )
    app.router.add_patch(
        "/agent-mcp/api/router/users/{user_id}",
        gated(edit_user_handler),
    )
    app.router.add_delete(
        "/agent-mcp/api/router/users/{user_id}",
        gated(delete_user_handler),
    )

    # Groups
    app.router.add_get(
        "/agent-mcp/api/router/groups", gated(list_groups_handler),
    )
    app.router.add_post(
        "/agent-mcp/api/router/groups", gated(create_group_handler),
    )
    app.router.add_patch(
        "/agent-mcp/api/router/groups/{group_id}",
        gated(edit_group_handler),
    )
    app.router.add_delete(
        "/agent-mcp/api/router/groups/{group_id}",
        gated(delete_group_handler),
    )

    # Group members
    app.router.add_get(
        "/agent-mcp/api/router/groups/{group_id}/members",
        gated(list_group_members_handler),
    )
    app.router.add_post(
        "/agent-mcp/api/router/groups/{group_id}/members",
        gated(add_group_member_handler),
    )
    app.router.add_delete(
        "/agent-mcp/api/router/groups/{group_id}/members/{member_id}",
        gated(remove_group_member_handler),
    )

    # Project memberships
    app.router.add_get(
        "/agent-mcp/api/router/projects/{name}/memberships",
        gated(list_project_memberships_handler),
    )
    app.router.add_post(
        "/agent-mcp/api/router/projects/{name}/memberships",
        gated(add_project_membership_handler),
    )
    app.router.add_patch(
        "/agent-mcp/api/router/projects/{name}/memberships/{membership_id}",
        gated(change_project_membership_role_handler),
    )
    app.router.add_delete(
        "/agent-mcp/api/router/projects/{name}/memberships/{membership_id}",
        gated(delete_project_membership_handler),
    )


__all__ = ["register_admin_users_routes"]
