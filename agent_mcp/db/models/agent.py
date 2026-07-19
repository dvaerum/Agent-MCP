# Agent-MCP/agent_mcp/db/models/agent.py
"""`agents` ORM model (db-review PR-G2).

Second model in the incremental SQLAlchemy adoption that started with
`ProjectContext`. The schema mirrors what `agent_mcp.db.schema.init_database()`
creates for fresh DBs — keeping the column set + types identical means
the ORM can read/write rows on a DB that was bootstrapped by raw SQL.

This PR ships the model + parity test + a cutover of the reader
surface used by lifespan startup + tool authorisation — originally
`agent_mcp.db.actions.agent_db`, a re-export shim arch-deepening R3
#2b deleted in favour of importing
`agent_mcp.repositories.agent_repository` directly. The tool-side
writes (admin_tools, task_tools) keep raw SQL for now; follow-up PRs
migrate them.

Column rationale:

* `token`: TEXT PRIMARY KEY — the bearer token. NOT exposed to clients;
  used internally for authentication.
* `agent_id`: TEXT UNIQUE NOT NULL — the display id (e.g. "alice").
  Referenced by the four FK constraints landed in PR #96 and the
  three deferred FKs landed in PR-G1.
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

from sqlalchemy import Boolean, CheckConstraint, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from ..engine import Base


class Agent(Base):
    __tablename__ = "agents"

    token: Mapped[str] = mapped_column(Text, primary_key=True)
    agent_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    current_task: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    working_directory: Mapped[str] = mapped_column(Text, nullable=False)
    color: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    terminated_at: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    aoe_session_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Event-coord PR-1: per-agent wake-loop toggle (default TRUE; FALSE
    # disables the wake-loop bootstrap shipped in PR-2). NOT NULL with
    # DEFAULT 1 in DDL.
    auto_event_loop: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1"),
    )
    # Event-coord PR-1: cursor for fetch_events_since (PR-2). NULL ⇒
    # "from the beginning".
    last_event_seen_at: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
    )
    # Phase 2 Wave 1a: per-agent privilege tier. 'worker' (default) is
    # the existing behaviour; 'manager' is introduced in Wave 2 as a
    # supervisor tier that can edit subordinates + assign tasks but
    # cannot mutate `config_*` keys or spawn new agents. The column
    # exists in this PR but is not yet read by any code path — the
    # @requires_role decorator that consumes it ships in Wave 2.
    # Default to 'worker' so existing agents stay in the least-
    # privileged tier; the CHECK constraint pins the domain.
    agent_role: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="worker",
        server_default=text("'worker'"),
    )
    # Agent self-service profiles (migration 0018). A single free-text
    # ``profile`` (self-authored "what I do / how I work / what to ask
    # me about") plus the review/change bookkeeping the governance story
    # rides on. All four are nullable so every existing row is valid
    # without a backfill:
    #   * profile             — the prose; NULL/'' = never set.
    #   * profile_updated_at  — bumped ONLY on content change (drives the
    #                           peer-broadcast event).
    #   * profile_reviewed_at — bumped on EVERY review, even a no-op
    #                           confirm (drives the staleness nudge).
    #   * profile_updated_by  — agent_id of whoever last changed the
    #                           content (NULL = system/seed). The
    #                           peer-broadcast excludes the EDITOR.
    profile: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    profile_updated_at: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
    )
    profile_reviewed_at: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
    )
    profile_updated_by: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "agent_role IN ('worker', 'manager')",
            name="ck_agents_agent_role_domain",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return f"<Agent agent_id={self.agent_id!r} status={self.status!r}>"
