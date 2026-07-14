# Agent-MCP/agent_mcp/repositories/project_settings_repository.py
"""ProjectSettingsRepository — cursor-based CRUD for `project_settings`.

Wave 11 PR 0 (ADR-0016): the operational-config sibling of
``project_context_repository`` — same plain-function shape, same
``connection=`` unit-of-work cursor seam, same BL-R22-1
``description_provided`` partial-update semantics; only the table name
differs. ``project_settings`` holds the operator-only ``config_*``
rows the hard-cutover migration
(``0016_move_config_to_project_settings``) moved out of
``project_context``; see ``docs/adr/0016-separate-config-from-memory.md``
for the memory-vs-settings terminology and access model.

Deliberately a **module of plain functions**, not a class + lifespan
singleton — ``project_settings`` has no in-memory cache and no
EventBus events, exactly like ``project_context`` (whose repository
docstring carries the full rationale).

Every function requires a ``connection`` (a live ``sqlite3.Cursor``,
row_factory=``sqlite3.Row``) — there is no standalone/self-opening
path. Callers always supply ``unit_of_work().cursor``.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional, Tuple


def _row_to_dict(row: Any) -> Dict[str, Any]:
    """Project a ``sqlite3.Row`` into the plain-dict shape consumers
    (settings tools, REST settings-data) expect."""
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
    """Fetch a single ``project_settings`` row by key, or ``None``.

    Reads through the caller's open cursor so a pending (uncommitted)
    write earlier in the same transaction is visible — required by
    :func:`upsert`'s existence check.
    """
    connection.execute(
        "SELECT context_key, value, description, created_at, created_by, "
        "updated_at, updated_by FROM project_settings WHERE context_key = ?",
        (context_key,),
    )
    row = connection.fetchone()
    return _row_to_dict(row) if row is not None else None


def list_all(*, connection: Any) -> List[Dict[str, Any]]:
    """Fetch every ``project_settings`` row, ordered by key.

    Used by the settings read surfaces (``view_project_settings`` /
    ``GET /api/settings-data``), which want the full snapshot — the
    store is small by construction (a handful of ``config_*`` rows).
    """
    connection.execute(
        "SELECT context_key, value, description, created_at, created_by, "
        "updated_at, updated_by FROM project_settings ORDER BY context_key"
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
    """INSERT-or-UPDATE a ``project_settings`` row.

    On INSERT: ``description`` is stored exactly as passed;
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
            INSERT INTO project_settings (
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
        UPDATE project_settings
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
    already exists — the caller maps that to a ``Conflict`` result.
    Returns the freshly-inserted row dict on success."""
    if get(context_key, connection=connection) is not None:
        return None

    now_iso = datetime.datetime.now().isoformat()
    connection.execute(
        """
        INSERT INTO project_settings (
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
    (each carrying ``context_key`` + ``description``) — keys with no
    matching row are silently omitted. Empty input returns ``[]``
    without touching the DB.
    """
    if not context_keys:
        return []

    placeholders = ",".join("?" for _ in context_keys)
    connection.execute(
        f"SELECT context_key, description FROM project_settings "
        f"WHERE context_key IN ({placeholders})",
        context_keys,
    )
    rows = [dict(r) for r in connection.fetchall()]

    for row in rows:
        connection.execute(
            "DELETE FROM project_settings WHERE context_key = ?",
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
