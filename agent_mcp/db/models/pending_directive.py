# Agent-MCP/agent_mcp/db/models/pending_directive.py
"""`pending_directive` ORM model — the one-shot **poke** queue.

An operator/admin **poke** pushes a single directive to an agent
out-of-band (plan §2 decision 11). It is delivered **immediately** if the
agent is listening (waiter-wake) or **queued as highest priority** for its
next check-in. Unlike scheduled fires (derived from
``scheduled_directive.next_due_at``), a poke is a persisted row here: it
survives until the agent's ``wait_for_events`` / ``fetch_events_since``
loop collects it and stamps ``delivered_at``.

Column rationale:

* ``poke_id``: TEXT PK.
* ``agent_id``: TEXT NOT NULL — target (logical FK -> agents.agent_id).
* ``prompt``: TEXT NOT NULL — the imperative text delivered.
* ``priority``: TEXT NOT NULL DEFAULT 'urgent' — sorts the delivered
  ``directive`` event to the FRONT of the returned batch (priority-aware
  feed ordering).
* ``created_at``: TEXT NOT NULL, ``created_by``: TEXT — operator/admin
  identity (audit).
* ``delivered_at``: TEXT NULL — set when the wait loop collects it; the
  undelivered predicate is ``delivered_at IS NULL``.

Index ``idx_pending_directive_undelivered`` on (agent_id, delivered_at)
backs the per-check-in "any undelivered pokes for me?" query.

Fresh-DB create_all makes this table (parity with the migration, which
no-ops when it already exists) — same coexistence contract as the sibling
models.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Index, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from ..engine import Base


class PendingDirective(Base):
    __tablename__ = "pending_directive"

    poke_id: Mapped[str] = mapped_column(Text, primary_key=True)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(
        Text, nullable=False, default="urgent",
        server_default=text("'urgent'"),
    )
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index(
            "idx_pending_directive_undelivered",
            "agent_id",
            "delivered_at",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return (
            f"<PendingDirective poke_id={self.poke_id!r} "
            f"agent_id={self.agent_id!r} delivered={self.delivered_at!r}>"
        )
