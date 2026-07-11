# Agent-MCP/agent_mcp/db/models/task.py
"""`tasks` ORM model (db-review PR-G3).

Third model in the incremental SQLAlchemy adoption (after
`ProjectContext` and `Agent`). The schema mirrors what
`agent_mcp.db.schema.init_database()` creates for fresh DBs —
keeping the column set + types identical means the ORM can read/write
rows on a DB that was bootstrapped by raw SQL.

This PR ships the model + parity test + a cutover of the reader
surface used by lifespan startup, dashboard API, and the all-tasks
route — originally `agent_mcp.db.actions.task_db`, a re-export shim
arch-deepening R3 #2b deleted in favour of importing
`agent_mcp.repositories.task_repository` directly. The tool-side
writes (task_tools.py, routes.py purge cascade) keep raw SQL for now;
follow-up PRs migrate them.

Column rationale:

* `task_id`: TEXT PRIMARY KEY — `task_<12-hex>` format. Minted by
  `task_repository._generate_task_id` (the canonical scheme as of
  arch-deepening R4 #7; `task_tools._generate_task_id` is now a thin
  delegator kept for call sites that need the id before the row
  exists). Referenced by two FK constraints (PR #96):
  `agents.current_task` and `tasks.parent_task` (self-ref).
* `title` / `created_by` / `status` / `priority` / `created_at` /
  `updated_at`: NOT NULL — required for every task row.
* `description` / `assigned_to` / `parent_task`: nullable.
* `assigned_to`: agent_id (NOT a token) of the worker assigned to
  this task. FK to `agents.agent_id` (PR #96).
* `child_tasks` / `depends_on_tasks` / `notes`: JSON-as-TEXT,
  nullable in DDL (defaults to `"[]"` on writes from the tool
  surface). `notes` is the legacy embedded format
  `[{timestamp, author, content}, ...]` — PR-H migrates this to a
  side table while leaving the column in place for one release.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..engine import Base


class Task(Base):
    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    assigned_to: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    parent_task: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    child_tasks: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    depends_on_tasks: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Event-coord PR-1: JSON list of lowercase capability labels. NULL
    # ⇒ "anyone can claim" (matches the empty-set broadcast semantics
    # locked in the plan). Normalized at write time via
    # `agent_mcp.utils.capability_normalization.normalize_capabilities`.
    required_capabilities: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
    )

    # PR-W3 (ORM big-bang): the three hot-path indexes (composite
    # for wait_for_events, single-column for status/priority filters)
    # were previously only in init_database()'s raw SQL.
    __table_args__ = (
        Index(
            "idx_tasks_assigned_to_updated_at",
            "assigned_to",
            "updated_at",
        ),
        Index("idx_tasks_status", "status"),
        Index("idx_tasks_priority", "priority"),
    )

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return f"<Task task_id={self.task_id!r} status={self.status!r}>"
