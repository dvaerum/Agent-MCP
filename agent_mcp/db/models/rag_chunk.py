# Agent-MCP/agent_mcp/db/models/rag_chunk.py
"""`rag_chunks` ORM model (PR-W3, ORM big-bang).

One row per indexed text chunk. `chunk_id` (INTEGER PK AUTOINCREMENT)
matches the `rowid` of the `rag_embeddings` virtual table, which is
how `vec0` links its vector data back to the source chunk row.

The `(source_type, source_ref)` index covers the hot read pattern in
the indexer's "is this chunk already indexed?" check before embedding.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..engine import Base


class RagChunk(Base):
    __tablename__ = "rag_chunks"

    chunk_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    indexed_at: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[Optional[str]] = mapped_column(
        "metadata", Text, nullable=True,
    )

    __table_args__ = (
        Index(
            "idx_rag_chunks_source_type_ref",
            "source_type",
            "source_ref",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return (
            f"<RagChunk chunk_id={self.chunk_id!r} "
            f"source={self.source_type!r}:{self.source_ref!r}>"
        )
