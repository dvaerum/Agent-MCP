# Agent-MCP/agent_mcp/db/models/agent_action.py
"""`agent_actions` ORM model (PR-W3, ORM big-bang).

Append-only audit log of agent activity. Every interesting tool call
(`assigned_task`, `started_work`, `completed_task`, `updated_context`,
`locked_file`, ...) writes one row. The dashboard reads it for the
agent-detail timeline and `/api/all-data`.

The two indexes match the hot read patterns:

* `(agent_id, timestamp DESC)` — the agent-detail timeline query.
* `(task_id, timestamp DESC)` — the task-tree timeline query, which
  filters by task and orders by recency.

Column rationale:

* `action_id`: INTEGER PK AUTOINCREMENT — stable identifier so a
  future edit/redact path can target a specific action. Reusing the
  highest deleted rowid (which a plain INTEGER PK would do) would
  let a stale reference hit a different action's row.
* `agent_id`: NOT NULL TEXT — can be a real agent_id or the literal
  string `'admin'` for actions taken by the synthetic Admin row.
* `action_type`: NOT NULL TEXT — free-form action label.
* `task_id`: nullable — actions that aren't task-bound (e.g.
  `updated_context`) carry NULL.
* `timestamp`: NOT NULL ISO-8601 string.
* `details`: nullable JSON-as-TEXT blob for action-specific extras
  (context_key, filepath, tool args, ...).
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..engine import Base


class AgentAction(Base):
    __tablename__ = "agent_actions"

    action_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    timestamp: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index(
            "idx_agent_actions_agent_id_timestamp",
            "agent_id",
            "timestamp",
        ),
        Index(
            "idx_agent_actions_task_id_timestamp",
            "task_id",
            "timestamp",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return (
            f"<AgentAction action_id={self.action_id!r} "
            f"agent_id={self.agent_id!r} type={self.action_type!r}>"
        )
