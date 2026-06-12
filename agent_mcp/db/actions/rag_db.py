"""Reusable DB operations for the RAG tables (``rag_chunks``,
``rag_embeddings``, ``rag_meta``).

.. deprecated:: PR F of the round-2 architecture-review series — the
   final repository in the four-concept set (tasks, agents, messages,
   RAG; follow-up to #146 / #147 / #153 / #154 / #155).

   This module was an **empty placeholder** before this PR — the
   architecture-review smell that motivated the work. Every other
   concept table had a populated ``db/actions/*_db.py`` module or a
   shim that re-exported from its repository; only RAG had neither.
   Now the SQL bodies live inside
   :mod:`agent_mcp.repositories.rag_repository` and this shim re-
   exports the small surface a future legacy importer might want
   (``_chunk_to_dict``, ``_embeddings_table_exists``) so the
   convention stays uniform.

   New code should import :class:`agent_mcp.repositories.RagRepository`
   via the ``rag_repo`` singleton instead — it's the single owner of
   rag_chunks / rag_embeddings / rag_meta, including the sqlite-vec
   ``vec0`` ``MATCH`` + ``k = ?`` dialect.

The shim is intentionally tiny because RAG never had a
module-of-functions form: there are no pre-existing call sites that
hand-imported helpers from here. The re-exports exist for shape
parity with ``agent_messages_db`` / ``task_db`` / ``agents_db`` so a
reader scanning ``db/actions/`` sees the same pattern across all
four concepts.
"""

from __future__ import annotations

from ...repositories.rag_repository import (
    _chunk_to_dict,
    _embeddings_table_exists,
)

__all__ = [
    "_chunk_to_dict",
    "_embeddings_table_exists",
]
