"""Reusable DB operations for the `task_notes` side table.

Introduced alongside the `TaskNote` model in db-review PR-H. The
side table replaces the legacy JSON-list-in-TEXT `tasks.notes`
column (kept in place for one release per the migration's safety
note); both stores coexist during the deprecation window.

This module is the only sanctioned read/write surface for
`task_notes`:

* `add_note(task_id, author, text)` -> Optional[int] (note_id, None on err)
* `edit_note(note_id, requester, new_text, is_admin)` -> Tuple[bool, str]
  - Returns (success, error_message); error_message is "" on success.
* `delete_note(note_id, requester, is_admin)` -> Tuple[bool, str]
* `list_notes_for_task(task_id)` -> List[Dict[str, Any]]
* `get_note(note_id)` -> Optional[Dict[str, Any]]

Author check semantics: only the author of a note or a
manager-tier+ caller may edit/delete it. The boolean ``is_admin``
is determined upstream via ``verify_token(token, "manager")`` so
manager-role agents can moderate worker notes. The parameter
name stays ``is_admin`` for back-compat with the rest of the
call chain; the semantic widening is captured in the upstream
docstring at ``task_notes_tools._resolve_caller``.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.exc import SQLAlchemyError

from ...core.config import logger
from ..engine import get_session
from ..models import TaskNote


def _note_to_dict(row: TaskNote) -> Dict[str, Any]:
    return {
        "note_id": row.note_id,
        "task_id": row.task_id,
        "author": row.author,
        "timestamp": row.timestamp,
        "text": row.text,
    }


def add_note(
    task_id: str, author: Optional[str], text: str,
) -> Optional[int]:
    """INSERT a new note and return the autoincrement note_id.

    Returns None on DB error. Empty `text` is rejected (returns None)
    — callers should validate before reaching the DB.
    """
    if not task_id or not text:
        return None
    timestamp = datetime.datetime.now().isoformat()
    try:
        with get_session() as session:
            row = TaskNote(
                task_id=task_id,
                author=author,
                timestamp=timestamp,
                text=text,
            )
            session.add(row)
            session.commit()
            return int(row.note_id)
    except SQLAlchemyError as e:
        logger.error(
            f"Database error inserting note for task '{task_id}': {e}",
            exc_info=True,
        )
        return None
    except Exception as e:
        logger.error(
            f"Unexpected error inserting note for task '{task_id}': {e}",
            exc_info=True,
        )
        return None


def get_note(note_id: int) -> Optional[Dict[str, Any]]:
    try:
        with get_session() as session:
            row = (
                session.query(TaskNote)
                .filter(TaskNote.note_id == note_id)
                .one_or_none()
            )
            return _note_to_dict(row) if row is not None else None
    # OverflowError (PF-R39-1): binding a note_id outside sqlite's
    # signed-64-bit range makes the sqlite3 driver raise a BARE
    # OverflowError that escapes SQLAlchemyError. Such an id can never
    # match a real row, so treat it as "no such note" rather than crash.
    except (SQLAlchemyError, OverflowError) as e:
        logger.error(
            f"Database error fetching note '{note_id}': {e}", exc_info=True,
        )
        return None


def list_notes_for_task(task_id: str) -> List[Dict[str, Any]]:
    """Return every note for `task_id`, oldest first (timestamp ASC).

    Order is timestamp-based rather than note_id because the side
    table was seeded from a JSON list whose authoring timestamps
    may not be monotonically increasing with insertion order.
    """
    try:
        with get_session() as session:
            rows = (
                session.query(TaskNote)
                .filter(TaskNote.task_id == task_id)
                .order_by(TaskNote.timestamp.asc(), TaskNote.note_id.asc())
                .all()
            )
            return [_note_to_dict(r) for r in rows]
    except SQLAlchemyError as e:
        logger.error(
            f"Database error listing notes for task '{task_id}': {e}",
            exc_info=True,
        )
        return []


def edit_note(
    note_id: int, requester: str, new_text: str, is_admin: bool,
) -> Tuple[bool, str]:
    """Update a note's text. Only the original author or admin may edit.

    Returns (success, error_message). On success the error_message is
    the empty string.
    """
    if not new_text:
        return False, "Note text cannot be empty"
    try:
        with get_session() as session:
            row = (
                session.query(TaskNote)
                .filter(TaskNote.note_id == note_id)
                .one_or_none()
            )
            if row is None:
                return False, f"Note {note_id} not found"
            if not is_admin and row.author != requester:
                return (
                    False,
                    f"Note {note_id} is owned by {row.author!r}; "
                    f"only the author or admin can edit it.",
                )
            row.text = new_text
            session.commit()
            return True, ""
    # OverflowError (PF-R39-1): an oversized note_id overflows the
    # sqlite3 int bind with a bare OverflowError outside SQLAlchemyError.
    # Return the same clean error tuple instead of crashing.
    except (SQLAlchemyError, OverflowError) as e:
        logger.error(
            f"Database error editing note '{note_id}': {e}", exc_info=True,
        )
        return False, "Database error"


def delete_note(
    note_id: int, requester: str, is_admin: bool,
) -> Tuple[bool, str]:
    """DELETE a note. Only the original author or admin may delete."""
    try:
        with get_session() as session:
            row = (
                session.query(TaskNote)
                .filter(TaskNote.note_id == note_id)
                .one_or_none()
            )
            if row is None:
                return False, f"Note {note_id} not found"
            if not is_admin and row.author != requester:
                return (
                    False,
                    f"Note {note_id} is owned by {row.author!r}; "
                    f"only the author or admin can delete it.",
                )
            session.delete(row)
            session.commit()
            return True, ""
    # OverflowError (PF-R39-1): see edit_note above — an oversized
    # note_id overflows the sqlite3 int bind; return the clean error
    # tuple instead of crashing.
    except (SQLAlchemyError, OverflowError) as e:
        logger.error(
            f"Database error deleting note '{note_id}': {e}", exc_info=True,
        )
        return False, "Database error"
