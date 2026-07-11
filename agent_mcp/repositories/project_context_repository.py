# Agent-MCP/agent_mcp/repositories/project_context_repository.py
"""ProjectContextRepository — cursor-based CRUD for `project_context`.

arch-deepening R4 candidate #6. Before this module, every write body in
``agent_mcp.tools.project_context_tools`` opened its own raw
``SessionLocal()`` ORM session and, to log an atomic audit row, reached
THROUGH the ORM session to grab the underlying DBAPI cursor
(``session.connection().connection.cursor()``) — five call sites doing
this identical drill-through. Each body then hand-rolled its own
``session.commit()`` / ``session.rollback()`` / ``session.close()``,
and at least one early-return branch (the authorization-failure path in
``delete_project_context_tool_impl``) skipped the explicit rollback
call, relying on SQLAlchemy's ``Session.close()`` implicit-rollback
behavior rather than a structural guarantee.

This module is the parameterized-SQL replacement: every write goes
through the caller's ``connection`` (always a raw ``sqlite3.Cursor``
handed in from an open ``agent_mcp.db.unit_of_work.unit_of_work()``
scope), matching the ``connection=`` seam ``TaskRepository`` /
``AgentRepository`` already established. The unit-of-work scope owns
BEGIN/COMMIT/ROLLBACK; this module never commits or rolls back on its
own.

Deliberately a **module of plain functions**, not a class + lifespan
singleton (unlike ``TaskRepository`` / ``AgentRepository`` /
``MessageRepository`` / ``RagRepository``): those classes exist to own
an in-memory cache invariant (``state.tasks``, ``state.active_agents``,
...) alongside the DB. ``project_context`` has no in-memory cache
anywhere in the codebase and no EventBus events are published for it
today — a class+singleton would add lifecycle ceremony with no
consumer benefit. If a cache or event story is added for
``project_context`` later, promoting this module to a class (mirroring
``MessageRepository``'s "thinner seam, no cache" shape) is the natural
next step.

Every function requires a ``connection`` (a live ``sqlite3.Cursor``,
row_factory=``sqlite3.Row``) — there is no standalone/self-opening
path. Callers always supply ``unit_of_work().cursor``.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional, Tuple


def _row_to_dict(row: Any) -> Dict[str, Any]:
    """Project a ``sqlite3.Row`` into the plain-dict shape every
    consumer (backup, health analysis, ownership checks) expects."""
    return {
        "context_key": row["context_key"],
        "value": row["value"],
        "description": row["description"],
        "created_at": row["created_at"],
        "created_by": row["created_by"],
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
    }


def get(context_key: str, *, connection: Any) -> Optional[Dict[str, Any]]:
    """Fetch a single ``project_context`` row by key, or ``None``.

    Reads through the caller's open cursor so a pending (uncommitted)
    write earlier in the same transaction is visible — required by
    :func:`upsert`'s existence check and by the per-key
    creator-ownership authorization gate in
    ``project_context_tools._check_write_authorization``.
    """
    connection.execute(
        "SELECT context_key, value, description, created_at, created_by, "
        "updated_at, updated_by FROM project_context WHERE context_key = ?",
        (context_key,),
    )
    row = connection.fetchone()
    return _row_to_dict(row) if row is not None else None


def list_all(*, connection: Any) -> List[Dict[str, Any]]:
    """Fetch every ``project_context`` row, ordered by key.

    Used by the backup + consistency-validation surfaces, which need a
    full snapshot rather than a single-key lookup.
    """
    connection.execute(
        "SELECT context_key, value, description, created_at, created_by, "
        "updated_at, updated_by FROM project_context ORDER BY context_key"
    )
    return [_row_to_dict(r) for r in connection.fetchall()]


def upsert(
    context_key: str,
    value: str,
    description: Optional[str],
    *,
    description_provided: bool,
    actor: str,
    connection: Any,
) -> Tuple[Dict[str, Any], bool]:
    """INSERT-or-UPDATE a ``project_context`` row.

    On INSERT: ``description`` is stored exactly as passed (the caller
    resolves any create-time default — e.g. the bulk path's
    ``"Bulk update operation N"`` filler — before calling this);
    ``created_at`` / ``created_by`` are stamped from ``actor`` + now.

    On UPDATE: ``value`` / ``updated_at`` / ``updated_by`` always
    refresh. ``description`` is overwritten ONLY when
    ``description_provided`` is True — BL-R22-1 partial-update parity:
    a value-only update must preserve the existing description rather
    than NULLing it. ``created_at`` / ``created_by`` are never touched
    on UPDATE.

    Returns ``(row_dict, created)`` where ``created`` is True iff this
    call performed an INSERT.
    """
    now_iso = datetime.datetime.now().isoformat()
    existing = get(context_key, connection=connection)

    if existing is None:
        connection.execute(
            """
            INSERT INTO project_context (
                context_key, value, description, created_at, created_by,
                updated_at, updated_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (context_key, value, description, now_iso, actor, now_iso, actor),
        )
        return (
            {
                "context_key": context_key,
                "value": value,
                "description": description,
                "created_at": now_iso,
                "created_by": actor,
                "updated_at": now_iso,
                "updated_by": actor,
            },
            True,
        )

    new_description = existing["description"]
    if description_provided:
        new_description = description
    connection.execute(
        """
        UPDATE project_context
        SET value = ?, updated_at = ?, updated_by = ?, description = ?
        WHERE context_key = ?
        """,
        (value, now_iso, actor, new_description, context_key),
    )
    return (
        {
            **existing,
            "value": value,
            "description": new_description,
            "updated_at": now_iso,
            "updated_by": actor,
        },
        False,
    )


def create_new(
    context_key: str,
    value: str,
    description: Optional[str],
    *,
    actor: str,
    connection: Any,
) -> Optional[Dict[str, Any]]:
    """INSERT-only. Returns ``None`` (no write performed) if the key
    already exists — the caller maps that to a ``Conflict`` result,
    matching ``create_project_context``'s "fails if the key already
    exists, use update_project_context to overwrite" contract. Returns
    the freshly-inserted row dict on success."""
    if get(context_key, connection=connection) is not None:
        return None

    now_iso = datetime.datetime.now().isoformat()
    connection.execute(
        """
        INSERT INTO project_context (
            context_key, value, description, created_at, created_by,
            updated_at, updated_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (context_key, value, description, now_iso, actor, now_iso, actor),
    )
    return {
        "context_key": context_key,
        "value": value,
        "description": description,
        "created_at": now_iso,
        "created_by": actor,
        "updated_at": now_iso,
        "updated_by": actor,
    }


def delete_many(
    context_keys: List[str], *, connection: Any,
) -> List[Dict[str, Any]]:
    """DELETE rows for the given keys.

    Returns the list of rows that actually existed and were deleted
    (each carrying ``context_key`` + ``description``, matching what
    ``delete_project_context_tool_impl`` needs for its response) — keys
    with no matching row are silently omitted, same as the legacy
    ``existing_map`` behavior. Empty input returns ``[]`` without
    touching the DB.
    """
    if not context_keys:
        return []

    placeholders = ",".join("?" for _ in context_keys)
    connection.execute(
        f"SELECT context_key, description FROM project_context "
        f"WHERE context_key IN ({placeholders})",
        context_keys,
    )
    rows = [dict(r) for r in connection.fetchall()]

    for row in rows:
        connection.execute(
            "DELETE FROM project_context WHERE context_key = ?",
            (row["context_key"],),
        )

    return rows


__all__ = [
    "get",
    "list_all",
    "upsert",
    "create_new",
    "delete_many",
]
