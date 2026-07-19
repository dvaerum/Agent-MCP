# Agent-MCP/agent_mcp/db/models/scheduled_directive.py
"""`scheduled_directive` ORM model (event-loop scheduled directives).

A **directive** is its own concept — an imperative "do X" command an
agent self-registers (or a manager/operator registers for it) to fire
recurrently *when the agent next checks in* at-or-after the interval.
It is NOT a message (agent↔agent communication) and NOT a task (tracked
lifecycle work); it rides the shared event infrastructure as its own
`directive` event type with its own store (this table).

Firing is wait-loop-native: this row is pure state (`next_due_at`); the
`wait_for_events` slice loop is the sole driver. There is no background
sweeper. When a directive is due AND the agent is checking in, the loop
emits a `directive` event, bumps `run_count`, and resets
`next_due_at = <delivery> + interval` (interval-reset-from-delivery, so
a busy agent never piles up fires).

Column rationale:

* `directive_id`: TEXT PK — the stable id CRUD tools target.
* `agent_id`: TEXT NOT NULL — target agent (logical FK -> agents.agent_id).
* `prompt`: TEXT NOT NULL — the imperative text delivered.
* `interval_seconds`: INT NOT NULL — spacing between deliveries; enforced
  >= the ``config_min_schedule_interval_seconds`` floor at create/update.
* `next_due_at`: TEXT NOT NULL — ISO wall-clock; the wait loop reads this
  as a wake condition and to decide when to fire.
* `enabled`: INT NOT NULL DEFAULT 1 — pause/resume + idle-stop suppression
  gate (an enabled schedule keeps the agent alive past idle-stop).
* `status`: TEXT NOT NULL DEFAULT 'active' — active | paused | completed.
  Terminal `completed` (with `enabled=0`) is set when an end-condition
  trips; the row is kept (listable), not deleted.
* `until_at`: TEXT NULL — end-condition (stop firing at/after this time).
* `max_runs`: INT NULL — end-condition (stop after this many fires).
* `run_count`: INT NOT NULL DEFAULT 0 — fires delivered so far.
* `created_at/by`, `updated_at/by`: audit.

Index ``idx_scheduled_directive_due`` on (agent_id, enabled, next_due_at)
backs the per-slice ``SELECT min(next_due_at)`` the wait loop runs.

## SQLite ALTER TABLE / ORM coexistence

Same pattern as the sibling models: ``init_database()`` runs
``Base.metadata.create_all()`` first, so on a FRESH DB this table is
created from this model and the migration's ``CREATE TABLE`` is a no-op
(the ``_table_exists`` gate). The migration's raw ``CREATE TABLE`` only
runs when upgrading a legacy DB created before this table existed.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Index, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from ..engine import Base


class ScheduledDirective(Base):
    __tablename__ = "scheduled_directive"

    directive_id: Mapped[str] = mapped_column(Text, primary_key=True)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    next_due_at: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1"),
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="active", server_default=text("'active'"),
    )
    until_at: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    max_runs: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    run_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0"),
    )
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index(
            "idx_scheduled_directive_due",
            "agent_id",
            "enabled",
            "next_due_at",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return (
            f"<ScheduledDirective directive_id={self.directive_id!r} "
            f"agent_id={self.agent_id!r} enabled={self.enabled!r}>"
        )
