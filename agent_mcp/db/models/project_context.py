# Agent-MCP/agent_mcp/db/models/project_context.py
"""`project_context` ORM model.

Mirrors the schema that `agent_mcp.db.schema.init_database()` creates
for fresh DBs. Keeping the column set + types identical means the
ORM can read/write rows on a DB that was bootstrapped by raw SQL,
which matters during the incremental cutover: tools migrate to the
ORM PR-by-PR while `init_database()` keeps owning the create-table
side until every table has a corresponding model + migration.

PR 7b will extend this model with creator-ownership columns
(`created_at`, `created_by`) and rename `last_updated` → `updated_at`
behind an Alembic migration. Do NOT add those here.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..engine import Base


class ProjectContext(Base):
    __tablename__ = "project_context"

    context_key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    last_updated: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return f"<ProjectContext key={self.context_key!r}>"
