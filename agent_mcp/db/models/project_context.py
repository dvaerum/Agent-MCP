# Agent-MCP/agent_mcp/db/models/project_context.py
"""`project_context` ORM model.

Mirrors the schema that `agent_mcp.db.schema.init_database()` creates
for fresh DBs (post-Phase-7b: ownership columns + `updated_at` rename).
Keeping the column set + types identical means the ORM can read/write
rows on a DB that was bootstrapped by raw SQL, which matters during
the incremental cutover: tools migrate to the ORM PR-by-PR while
`init_database()` keeps owning the create-table side until every
table has a corresponding model + migration.

Phase 7b columns:
- `created_at` / `created_by`: stamped on first INSERT, never mutated
  on subsequent UPDATEs. Used by the ownership rules in
  `project_context_tools.py`.
- `updated_at` (renamed from `last_updated`): refreshed on every UPDATE.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Text
from sqlalchemy.orm import Mapped, mapped_column

from ..engine import Base


class ProjectContext(Base):
    __tablename__ = "project_context"

    context_key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Ownership columns (Phase 7b). Nullable in SQL (legacy rows pre-7b
    # have NULLs until the migration backfills them) but the application
    # always supplies them on writes.
    created_at: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return f"<ProjectContext key={self.context_key!r}>"
