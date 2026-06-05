# Agent-MCP/agent_mcp/db/models/claude_code_session.py
"""`claude_code_sessions` ORM model (PR-W3, ORM big-bang).

Captures Claude Code process metadata detected by the `git-agentmcp`
hook integration. Each row is a single Claude Code session
(identified by PID at detection time) along with its parent PID,
working directory, optional linked `agents.agent_id`, status, and
two JSON-as-TEXT blobs (`git_commits`, `metadata`).

The four indexes match the hot dashboard read patterns:

* `(pid, parent_pid)` — process-tree lookups during detection.
* `(last_activity DESC)` — admin overview "recent sessions" sort.
* `(agent_id)` — link from an agent to its session(s).
* `(status)` — admin filter by status (detected/registered/...).

Column rationale:

* `session_id`: TEXT PRIMARY KEY — uuid generated at detection.
* `pid` / `parent_pid`: NOT NULL INTEGER — captured at detection.
* `first_detected` / `last_activity`: NOT NULL ISO-8601 strings.
* `working_directory`: nullable — the hook may not always be able
  to resolve it.
* `agent_id`: nullable — set when an agent registers against the
  session (otherwise the session is "detected but unbound").
* `status`: nullable; the legacy DDL declares `DEFAULT 'detected'`
  but does NOT mark it NOT NULL. The ORM mirrors that.
* `git_commits` / `metadata`: nullable JSON-as-TEXT blobs.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Index, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from ..engine import Base


class ClaudeCodeSession(Base):
    __tablename__ = "claude_code_sessions"

    session_id: Mapped[str] = mapped_column(Text, primary_key=True)
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_pid: Mapped[int] = mapped_column(Integer, nullable=False)
    first_detected: Mapped[str] = mapped_column(Text, nullable=False)
    last_activity: Mapped[str] = mapped_column(Text, nullable=False)
    working_directory: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
    )
    agent_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, server_default=text("'detected'"),
    )
    git_commits: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_: Mapped[Optional[str]] = mapped_column(
        "metadata", Text, nullable=True,
    )

    __table_args__ = (
        Index("idx_claude_sessions_pid", "pid", "parent_pid"),
        Index("idx_claude_sessions_activity", "last_activity"),
        Index("idx_claude_sessions_agent", "agent_id"),
        Index("idx_claude_sessions_status", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return (
            f"<ClaudeCodeSession session_id={self.session_id!r} "
            f"pid={self.pid!r}>"
        )
