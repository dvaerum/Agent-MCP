# Agent-MCP/agent_mcp/db/models/task_note.py
"""`task_notes` ORM model (db-review PR-H).

Replaces the JSON-list-in-TEXT pattern of `tasks.notes` (one
appendable blob per task) with a proper side table — one row per
note — so individual notes can be edited and deleted (the chief
limitation called out by PR #74's caveat).

The legacy `tasks.notes` column is **kept in place for one release**
(per the migration's safety note); both the JSON column and the side
table coexist during the deprecation window. New tools
(`add_task_note`, `edit_task_note`, `delete_task_note`) write to
the side table; the existing append-only writers in `task_tools.py`
/ `app/routes.py` still mutate the JSON column. A follow-up PR
flips the legacy writers over and drops the column.

Column rationale:

* `note_id`: INTEGER PK AUTOINCREMENT — the stable identifier the
  edit/delete tools target. AUTOINCREMENT (not just INTEGER PK)
  matters here because sqlite would otherwise reuse the highest
  deleted rowid; a stale tool call referencing the deleted note_id
  would silently hit a different note's row.
* `task_id`: NOT NULL TEXT — the parent task. The migration does
  NOT declare a SQL FK to `tasks(task_id)` for the same conservative
  reason as PR-G1: defer FK declaration to a follow-up after watching
  the new table in production.
* `author`: nullable — historic notes may have lacked an author
  field; preserve None rather than coercing to a synthetic value.
* `timestamp` / `text`: NOT NULL.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..engine import Base


class TaskNote(Base):
    __tablename__ = "task_notes"

    note_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    timestamp: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return (
            f"<TaskNote note_id={self.note_id!r} "
            f"task_id={self.task_id!r}>"
        )
