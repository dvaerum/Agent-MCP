# Agent-MCP/agent_mcp/db/models/agent.py
"""`agents` ORM model (db-review PR-G2).

Second model in the incremental SQLAlchemy adoption that started with
`ProjectContext`. The schema mirrors what `agent_mcp.db.schema.init_database()`
creates for fresh DBs — keeping the column set + types identical means
the ORM can read/write rows on a DB that was bootstrapped by raw SQL.

This PR ships the model + parity test + a cutover of
`agent_mcp.db.actions.agent_db` (the reader surface used by lifespan
startup + tool authorisation). The tool-side writes (admin_tools,
task_tools) keep raw SQL for now; follow-up PRs migrate them.

Column rationale:

* `token`: TEXT PRIMARY KEY — the bearer token. NOT exposed to clients;
  used internally for authentication.
* `agent_id`: TEXT UNIQUE NOT NULL — the display id (e.g. "alice").
  Referenced by the four FK constraints landed in PR #96 and the
  three deferred FKs landed in PR-G1.
* `capabilities`: JSON-as-TEXT, nullable in DDL (defaults to "[]" on
  writes from the tool surface).
* `created_at` / `updated_at`: ISO-8601 strings. `created_at` is set
  on INSERT only; `updated_at` refreshed by `update_agent_db_field`.
* `status`: free-form string — common values are 'created', 'active',
  'terminated', 'system' (the synthetic admin pseudo-agent), and
  'tombstone' (the synthetic `[deleted-<id>]` row created by the
  purge cascade in routes.py).
* `aoe_session_id`: 16-hex side-channel session id for the AoE
  notification stream. Added by migration 0003.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Text
from sqlalchemy.orm import Mapped, mapped_column

from ..engine import Base


class Agent(Base):
    __tablename__ = "agents"

    token: Mapped[str] = mapped_column(Text, primary_key=True)
    agent_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    capabilities: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    current_task: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    working_directory: Mapped[str] = mapped_column(Text, nullable=False)
    color: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    terminated_at: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    aoe_session_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return f"<Agent agent_id={self.agent_id!r} status={self.status!r}>"
