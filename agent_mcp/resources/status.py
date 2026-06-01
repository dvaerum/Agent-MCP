"""Status resource — ambient counters for the calling agent."""

from __future__ import annotations

import json

from ..db.connection import get_db_connection

_TERMINAL_TASK_STATUSES = ("completed", "cancelled", "failed")


def render_status(agent_id: str) -> str:
    """Return the status counter JSON for `agent_id`.

    Counters:

    * ``unread_messages`` — count of `agent_messages` rows where
      `recipient_id = agent_id` AND `read = 0`.
    * ``unfinished_tasks`` — count of `tasks` rows where
      `assigned_to = agent_id` AND `status NOT IN
      ('completed', 'cancelled', 'failed')`.

    Additive: future counters slot in without breaking the schema
    (the consumer reads the keys it knows; unknown keys are
    ignored).
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) AS n FROM agent_messages "
            "WHERE recipient_id = ? AND read = 0",
            (agent_id,),
        )
        unread_messages = int(cursor.fetchone()["n"])

        placeholders = ",".join("?" * len(_TERMINAL_TASK_STATUSES))
        cursor.execute(
            "SELECT COUNT(*) AS n FROM tasks "
            f"WHERE assigned_to = ? AND status NOT IN ({placeholders})",
            (agent_id, *_TERMINAL_TASK_STATUSES),
        )
        unfinished_tasks = int(cursor.fetchone()["n"])
    finally:
        conn.close()

    return json.dumps(
        {
            "agent_id": agent_id,
            "unread_messages": unread_messages,
            "unfinished_tasks": unfinished_tasks,
        },
        ensure_ascii=False,
    )
