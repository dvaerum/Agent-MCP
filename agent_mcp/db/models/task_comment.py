# Agent-MCP/agent_mcp/db/models/task_comment.py
"""`task_comments` ORM model (db-review PR-H).

Replaces the JSON-list-in-TEXT pattern of `tasks.notes` (one
appendable blob per task) with a proper side table — one row per
comment — so individual comments can be edited and deleted (the chief
limitation called out by PR #74's caveat).

The legacy `tasks.notes` column is **kept in place for one release**
(per the migration's safety note); both the JSON column and the side
table coexist during the deprecation window. New tools
(`add_task_comment`, `edit_task_comment`, `delete_task_comment`) write
to the side table; the existing append-only writers in `task_tools.py`
/ `app/routes.py` still mutate the JSON column. A follow-up PR
flips the legacy writers over and drops the column.

Column rationale:

* `note_id`: INTEGER PK AUTOINCREMENT — the stable identifier the
  edit/delete tools target. AUTOINCREMENT (not just INTEGER PK)
  matters here because sqlite would otherwise reuse the highest
  deleted rowid; a stale tool call referencing the deleted note_id
  would silently hit a different comment's row. Kept as `note_id`
  (not renamed to `comment_id`) — the PK column name is a
  compatibility-neutral detail, not part of the user-facing
  "note→comment" identity; see migration 0026's docstring.
* `task_id`: NOT NULL TEXT — the parent task. The migration does
  NOT declare a SQL FK to `tasks(task_id)` for the same conservative
  reason as PR-G1: defer FK declaration to a follow-up after watching
  the new table in production.
* `author`: nullable — historic comments may have lacked an author
  field; preserve None rather than coercing to a synthetic value.
* `timestamp` / `text`: NOT NULL.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..engine import Base


class TaskComment(Base):
    __tablename__ = "task_comments"

    note_id: Mapped[int] = mapped_column(
        # nullable=True (not the mapped_column default of False for a PK)
        # matches migration 0009's raw `CREATE TABLE` text verbatim
        # (`note_id INTEGER PRIMARY KEY AUTOINCREMENT`, no explicit
        # `NOT NULL`) — SQLite's integer-PK/rowid-alias semantics reject
        # a NULL value at the storage layer regardless of this DDL-text
        # annotation, so this is a schema-reflection-parity detail, not
        # a behavior change. Declaring it NOT NULL here (SQLAlchemy's
        # default DDL for a PK column) is invisible on a fresh
        # `init_database()` boot — that path always wins the race and
        # creates the table before any migration does — but becomes
        # visible schema drift the moment a migration replay ever has
        # to fall back to 0009's own `CREATE TABLE` text (e.g. this
        # project's from-scratch migration-chain test harness), because
        # then the two DDL sources genuinely disagree on the column's
        # NULL-ability text. See migration
        # 0026_rename_task_notes_to_task_comments's docstring for the
        # table-name race this column sits inside of.
        Integer, primary_key=True, autoincrement=True, nullable=True,
    )
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    timestamp: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    # PR-W3 (ORM big-bang): single-column index on task_id (was
    # previously only in init_database()'s raw SQL).
    #
    # `implicit_returning=False`: `note_id` above is `nullable=True`
    # for schema-reflection parity with migration 0009's raw DDL (see
    # that column's own comment). SQLAlchemy 2.0's `insertmanyvalues`
    # optimization auto-selects an autoincrement PK as the implicit
    # "sentinel" column used to correlate RETURNING rows back to ORM
    # objects when 2+ new rows are flushed in one `session.commit()`;
    # it refuses to do so for a nullable sentinel with no default
    # generator (`InvalidRequestError: ... has been marked as a
    # sentinel column with no default generation function`). Disabling
    # RETURNING for this table falls back to SQLite's plain
    # `lastrowid`-per-INSERT path, which needs no sentinel at all —
    # correct and sufficient here since nothing depends on RETURNING
    # semantics for this table.
    __table_args__ = (
        Index("idx_task_comments_task", "task_id"),
        {"implicit_returning": False},
    )

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return (
            f"<TaskComment note_id={self.note_id!r} "
            f"task_id={self.task_id!r}>"
        )
