"""Reusable DB operations for the `tasks` table.

Cutover to SQLAlchemy in db-review PR-G3 — the model lives in
`agent_mcp.db.models.task::Task`. Function signatures + return
shapes (Optional[Dict[str, Any]] / List[Dict[str, Any]] keyed by
column name, JSON-typed fields deserialised to Python lists) are
preserved 1:1 so consumers (cli, app/routes, dashboard API) don't
need to change.

The raw-SQL update path in `update_task_fields_in_db` keeps its
allowlist of mutable fields — that allowlist doubles as
anti-injection protection. The ORM cutover replaces the
`f"UPDATE ... SET {field} = ?"` template with `setattr(row, field,
value)` on the Task instance, but the allowlist guard stays.
"""

from __future__ import annotations

import datetime
import json
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import SQLAlchemyError

from ...core.config import logger
from ..engine import get_session
from ..models import Task


# Columns the caller is allowed to mutate via `update_task_fields_in_db`.
# Used both as an allowlist (anti-injection / anti-typo) and to
# centralise the JSON-serialisation rule for the list-typed fields.
_MUTABLE_FIELDS: set[str] = {
    "title",
    "description",
    "assigned_to",
    "status",
    "priority",
    "parent_task",
    "child_tasks",
    "depends_on_tasks",
    "notes",
}

# Subset of _MUTABLE_FIELDS that store a JSON-encoded list in their
# TEXT column. Callers pass a Python list; we json.dumps before write.
_JSON_LIST_FIELDS: set[str] = {
    "child_tasks",
    "depends_on_tasks",
    "notes",
}


def _task_to_dict(row: Task) -> Dict[str, Any]:
    """Project a `Task` ORM row into the dict shape consumers expect.

    Mirrors the pre-cutover `dict(sqlite_row)` projection then
    `_parse_task_json_fields`: every column is exposed by name, and
    the three JSON-typed columns are deserialised to Python lists.
    Parse failures fall back to an empty list with a warning (matches
    the legacy helper's behaviour exactly).
    """
    data: Dict[str, Any] = {
        "task_id": row.task_id,
        "title": row.title,
        "description": row.description,
        "assigned_to": row.assigned_to,
        "created_by": row.created_by,
        "status": row.status,
        "priority": row.priority,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "parent_task": row.parent_task,
        "child_tasks": row.child_tasks,
        "depends_on_tasks": row.depends_on_tasks,
        "notes": row.notes,
    }
    for field_key in _JSON_LIST_FIELDS:
        raw = data.get(field_key)
        if isinstance(raw, str):
            try:
                data[field_key] = json.loads(raw or "[]")
            except json.JSONDecodeError:
                logger.warning(
                    f"Failed to parse JSON for field '{field_key}' in "
                    f"task '{data.get('task_id', 'Unknown')}'. Raw: {raw}"
                )
                data[field_key] = []
        elif raw is None:
            data[field_key] = []
    return data


def get_task_by_id(task_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single task's details from the database by task_id.

    Parses JSON fields (child_tasks, depends_on_tasks, notes) into
    Python lists. Returns None if the task is not found.
    """
    try:
        with get_session() as session:
            row = (
                session.query(Task)
                .filter(Task.task_id == task_id)
                .one_or_none()
            )
            return _task_to_dict(row) if row is not None else None
    except SQLAlchemyError as e:
        logger.error(
            f"Database error fetching task by ID '{task_id}': {e}",
            exc_info=True,
        )
        return None
    except Exception as e:
        logger.error(
            f"Unexpected error fetching task by ID '{task_id}': {e}",
            exc_info=True,
        )
        return None


def get_all_tasks_from_db() -> List[Dict[str, Any]]:
    """Fetch all tasks from the database, newest first.

    Used by `application_startup` (populates `g.tasks`) and by the
    `/api/all-data` dashboard route.
    """
    try:
        with get_session() as session:
            rows = (
                session.query(Task)
                .order_by(Task.created_at.desc())
                .all()
            )
            return [_task_to_dict(r) for r in rows]
    except SQLAlchemyError as e:
        logger.error(
            f"Database error fetching all tasks: {e}", exc_info=True,
        )
        return []
    except Exception as e:
        logger.error(
            f"Unexpected error fetching all tasks: {e}", exc_info=True,
        )
        return []


def get_tasks_by_agent_id(
    agent_id: str, status_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch tasks assigned to a specific agent, optionally filtered
    by status. Parses JSON fields for each task."""
    try:
        with get_session() as session:
            query = session.query(Task).filter(Task.assigned_to == agent_id)
            if status_filter:
                query = query.filter(Task.status == status_filter)
            rows = query.order_by(Task.created_at.desc()).all()
            return [_task_to_dict(r) for r in rows]
    except SQLAlchemyError as e:
        logger.error(
            f"Database error fetching tasks for agent '{agent_id}': {e}",
            exc_info=True,
        )
        return []
    except Exception as e:
        logger.error(
            f"Unexpected error fetching tasks for agent '{agent_id}': {e}",
            exc_info=True,
        )
        return []


def update_task_fields_in_db(
    task_id: str, fields_to_update: Dict[str, Any],
) -> bool:
    """Update specified fields for a task in the database.

    Always refreshes `updated_at`. JSON-typed list fields
    (notes/child_tasks/depends_on_tasks) are serialised to TEXT.
    Returns True on success, False on any failure (unknown task,
    no valid fields, or DB error).

    The allowlist is non-negotiable — callers must not be able to
    mutate `task_id`, `created_by`, `created_at`, or `updated_at`
    directly via this surface.
    """
    if not task_id or not fields_to_update:
        logger.warning(
            "update_task_fields_in_db called with no task_id or no "
            "fields to update."
        )
        return False

    # Pre-filter to the allowlist so the ORM `setattr` loop can't
    # touch anything off-limits. Mirrors the legacy
    # safe_field_mapping[field] KeyError-on-unknown guard.
    sanitised: Dict[str, Any] = {}
    for field, value in fields_to_update.items():
        if field not in _MUTABLE_FIELDS:
            logger.warning(
                f"Attempted to update invalid task field: {field} for "
                f"task {task_id}. Skipping."
            )
            continue
        if field in _JSON_LIST_FIELDS:
            sanitised[field] = json.dumps(value or [])
        else:
            sanitised[field] = value

    if not sanitised:
        logger.info(f"No valid fields to update for task {task_id}.")
        return False

    try:
        with get_session() as session:
            row = (
                session.query(Task)
                .filter(Task.task_id == task_id)
                .one_or_none()
            )
            if row is None:
                logger.warning(
                    f"Task '{task_id}' not found or update had no "
                    f"effect in DB."
                )
                return False
            for field, value in sanitised.items():
                setattr(row, field, value)
            # Always bump updated_at, even if the caller passed one
            # (matches the legacy behaviour: updated_at was always
            # appended to the SET clause unconditionally).
            row.updated_at = datetime.datetime.now().isoformat()
            session.commit()
            logger.info(
                f"Task '{task_id}' updated in DB with fields: "
                f"{list(sanitised.keys())}."
            )
            return True
    except SQLAlchemyError as e:
        logger.error(
            f"Database error updating task '{task_id}': {e}", exc_info=True,
        )
        return False
    except Exception as e:
        logger.error(
            f"Unexpected error updating task '{task_id}': {e}", exc_info=True,
        )
        return False
