# Agent-MCP/agent_mcp/db/models/rag_meta.py
"""`rag_meta` ORM model (PR-W3, ORM big-bang).

A simple key/value side-table used by the RAG indexer to track
per-source last-indexed timestamps and per-file content hashes
(`hash_<filepath>` keys). Values are opaque strings; the indexer
parses them as ISO timestamps or sha-256 hex on a per-key basis.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Text
from sqlalchemy.orm import Mapped, mapped_column

from ..engine import Base


class RagMeta(Base):
    __tablename__ = "rag_meta"

    meta_key: Mapped[str] = mapped_column(Text, primary_key=True)
    meta_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return f"<RagMeta meta_key={self.meta_key!r}>"
