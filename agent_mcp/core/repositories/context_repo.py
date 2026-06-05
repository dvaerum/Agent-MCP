# Agent-MCP/agent_mcp/core/repositories/context_repo.py
"""ContextRepository — ORM-aware repo for ``project_context``.

``project_context`` does not have a separate in-memory cache in
``state.*`` today (reads are infrequent and the ``access.py`` toggle
lookups already cache their own results inside that module). This
repo is ORM-aware: it talks to the ``ProjectContext`` SQLAlchemy
model directly rather than going through
:mod:`agent_mcp.db.actions.context_db`, because that actions module
is a stub left over from the early Phase-7 ORM cutover plan.

Public contract mirrors the other three repos for uniformity:

Reads
    * :func:`get_context` — fetch a single context row by key.
    * :func:`list_context_keys` — enumerate all context keys (no
      values, for backup / health endpoints).

Writes
    * :func:`set_context` — INSERT or UPDATE a context row, refresh
      cache (no-op today), publish ``"context.updated"`` to the bus
      with the ``"*"`` broadcast recipient (no single agent owns
      context).
    * :func:`delete_context` — DELETE a context row, publish
      ``"context.deleted"``.

Test mode
    * :func:`disable_cache` — no-op context manager (no cache to
      disable).
"""
from __future__ import annotations

import contextlib
import datetime
from typing import Any, Dict, Iterator, List, Optional

from sqlalchemy import delete as sa_delete

from ..config import logger
from ...db.engine import get_session
from ...db.models import ProjectContext
from . import _event_bus_shim


_cache_disabled: bool = False


@contextlib.contextmanager
def disable_cache() -> Iterator[None]:
    """No-op cache toggle — keeps the repo interface uniform across the
    four repos. There is no project_context cache today.
    """
    global _cache_disabled
    prev = _cache_disabled
    _cache_disabled = True
    try:
        yield
    finally:
        _cache_disabled = prev


def reset() -> None:
    """No-op — there is no in-memory project_context cache to clear."""
    return None


# --- read interface -----------------------------------------------------


def _row_to_dict(row: ProjectContext) -> Dict[str, Any]:
    """Project a ``ProjectContext`` ORM row into the dict shape
    consumers expect. Mirrors the projection in
    :mod:`agent_mcp.tools.project_context_tools` so the two surfaces
    return rows that compare equal."""
    return {
        "context_key": row.context_key,
        "value": row.value,
        "description": row.description,
        "created_at": row.created_at,
        "created_by": row.created_by,
        "updated_at": row.updated_at,
        "updated_by": row.updated_by,
    }


def get_context(context_key: str) -> Optional[Dict[str, Any]]:
    """Fetch a single project_context row by key. ``None`` if absent."""
    try:
        with get_session() as session:
            row = (
                session.query(ProjectContext)
                .filter(ProjectContext.context_key == context_key)
                .one_or_none()
            )
            return _row_to_dict(row) if row is not None else None
    except Exception as e:  # pragma: no cover — defensive
        logger.error(
            f"context_repo.get_context({context_key!r}) failed: {e}",
            exc_info=True,
        )
        return None


def list_context_keys() -> List[str]:
    """Return every context key in the DB. For backup / health views."""
    try:
        with get_session() as session:
            rows = session.query(ProjectContext.context_key).all()
            return [r[0] for r in rows]
    except Exception as e:  # pragma: no cover — defensive
        logger.error(f"context_repo.list_context_keys failed: {e}", exc_info=True)
        return []


# --- write interface ----------------------------------------------------


def set_context(
    *,
    context_key: str,
    value: str,
    description: Optional[str],
    updated_by: str,
) -> bool:
    """INSERT or UPDATE a project_context row.

    On INSERT: stamps ``created_at`` / ``created_by`` to "now" /
    ``updated_by`` (the first-writer-wins ownership rule from
    Phase-7b).
    On UPDATE: leaves ``created_at`` / ``created_by`` frozen,
    refreshes ``updated_at`` / ``updated_by``.

    Returns True on success, False on DB error.
    """
    now = datetime.datetime.now().isoformat()
    try:
        with get_session() as session:
            row = (
                session.query(ProjectContext)
                .filter(ProjectContext.context_key == context_key)
                .one_or_none()
            )
            if row is None:
                session.add(
                    ProjectContext(
                        context_key=context_key,
                        value=value,
                        description=description,
                        created_at=now,
                        created_by=updated_by,
                        updated_at=now,
                        updated_by=updated_by,
                    )
                )
            else:
                row.value = value
                if description is not None:
                    row.description = description
                row.updated_at = now
                row.updated_by = updated_by
            session.commit()
    except Exception as e:
        logger.error(
            f"context_repo.set_context({context_key!r}) failed: {e}",
            exc_info=True,
        )
        return False

    _event_bus_shim.publish(
        "*",
        "context.updated",
        {"context_key": context_key, "updated_by": updated_by},
    )
    return True


def delete_context(context_key: str, *, deleted_by: str) -> bool:
    """DELETE a project_context row. Returns False if the row didn't
    exist or the DB call errored."""
    try:
        with get_session() as session:
            result = session.execute(
                sa_delete(ProjectContext).where(
                    ProjectContext.context_key == context_key
                )
            )
            session.commit()
            # ``Result`` has no public ``rowcount`` attribute on every
            # backend; SQLite + ORM-level delete() exposes it, but mypy
            # types it on ``CursorResult`` only. Cast to silence
            # ``--strict`` while keeping the runtime check.
            rowcount = getattr(result, "rowcount", 0) or 0
            if rowcount == 0:
                return False
    except Exception as e:
        logger.error(
            f"context_repo.delete_context({context_key!r}) failed: {e}",
            exc_info=True,
        )
        return False

    _event_bus_shim.publish(
        "*",
        "context.deleted",
        {"context_key": context_key, "deleted_by": deleted_by},
    )
    return True


__all__ = [
    "delete_context",
    "disable_cache",
    "get_context",
    "list_context_keys",
    "reset",
    "set_context",
]
