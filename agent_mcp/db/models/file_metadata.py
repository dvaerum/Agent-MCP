# Agent-MCP/agent_mcp/db/models/file_metadata.py
"""`file_metadata` ORM model (PR-W3, ORM big-bang).

Per-file metadata captured by the indexer + file-lock tooling. Keyed
by normalised absolute filepath; one row per file. The `metadata`
column is an opaque JSON-as-TEXT blob with indexer-defined keys
(language, line count, last seen lock holder, ...).

`content_hash` is a SHA-256 hex string of the file body, used by the
RAG indexer to skip re-embedding unchanged content.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Text
from sqlalchemy.orm import Mapped, mapped_column

from ..engine import Base


class FileMetadata(Base):
    __tablename__ = "file_metadata"

    filepath: Mapped[str] = mapped_column(Text, primary_key=True)
    metadata_: Mapped[str] = mapped_column(
        "metadata", Text, nullable=False,
    )
    last_updated: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return f"<FileMetadata filepath={self.filepath!r}>"
