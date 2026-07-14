# Agent-MCP/agent_mcp/db/models/project_settings.py
"""`project_settings` ORM model (ADR-0016, Wave 11).

The operational-config sibling of `project_context`: **settings** =
operator-only ``config_*`` toggles/knobs (never RAG-indexed), while
**memory** = agent-authored shared knowledge (`project_context`,
RAG-indexed). Migration ``0016_move_config_to_project_settings``
hard-cuts every ``config_*`` row over to this table.

The column set deliberately mirrors `project_context` byte-for-byte:
values stay JSON-encoded TEXT so the coercion helpers
(``tools/access.py::_get_config_bool`` / ``_get_config_int``) work
unchanged against the new table — only their ``FROM`` clause changed.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Text
from sqlalchemy.orm import Mapped, mapped_column

from ..engine import Base


class ProjectSettings(Base):
    __tablename__ = "project_settings"

    context_key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Nullable in SQL (rows migrated from pre-Phase-7b project_context
    # may carry NULLs) but the application always supplies them on writes.
    created_at: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return f"<ProjectSettings key={self.context_key!r}>"
