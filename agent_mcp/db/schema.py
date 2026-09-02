# Agent-MCP/agent_mcp/db/schema.py
"""Database schema bootstrap.

PR-W3 (ORM big-bang, v5.0.19) made the SQLAlchemy ORM the single
source of truth for the schema. This module is now a thin runner:

* `init_database()` calls `Base.metadata.create_all(engine)` against
  every model registered under `agent_mcp.db.models`. That covers
  every persistent table the application owns (agents, tasks,
  task_comments, agent_actions, project_context, file_metadata,
  rag_chunks, rag_meta, agent_messages, claude_code_sessions,
  mcp_sessions) along with their canonical indexes.

* The `rag_embeddings` virtual table is sqlite-vec's `vec0` module,
  which is NOT a SQLAlchemy-modellable shape. `init_database()`
  still emits its CREATE VIRTUAL TABLE statement directly, gated on
  the runtime VSS-loadability check.

* The `rag_meta` default rows (last_indexed_<source> = epoch) are
  inserted after `create_all()` so a fresh DB starts at the
  expected indexer baseline.

* The pre-existing embedding-dimension compatibility check still
  runs before the virtual-table create, so a config change between
  runs causes the indexer to re-embed everything.

The Alembic migration chain (0001-0011) still applies after this on
existing databases; for fresh DBs the migrations are effectively
no-ops because `create_all()` lands at the final shape directly.
The 0011 migration is a marker recording the cut-over.
"""

from __future__ import annotations

import sqlite3

from sqlalchemy import text as sa_text

from ..core.config import logger, embedding_settings
from .connection import get_db_connection, check_vss_loadability, is_vss_loadable
from .engine import Base, get_engine
from . import models  # noqa: F401  — registers every ORM model on Base.metadata


# Default rows inserted into rag_meta on a fresh DB. The indexer
# treats a missing key as "never indexed", which is fine semantically
# — these seed values make the dashboard's "last indexed" panels
# render with the canonical baseline immediately rather than after
# the first indexer run.
_DEFAULT_RAG_META_ENTRIES = [
    ("last_indexed_markdown", "1970-01-01T00:00:00Z"),
    ("last_indexed_code", "1970-01-01T00:00:00Z"),
    ("last_indexed_context", "1970-01-01T00:00:00Z"),
    ("last_indexed_filemeta", "1970-01-01T00:00:00Z"),
    ("last_indexed_tasks", "1970-01-01T00:00:00Z"),
]


def check_embedding_dimension_compatibility(conn: sqlite3.Connection) -> bool:
    """Check whether the current rag_embeddings table matches the
    configured embedding dimension (``embedding_settings().dimension``).

    Returns True if compatible (or the table is absent), False if
    the on-disk dimension differs from the configured one.
    """
    required_dimension = embedding_settings().dimension
    cursor = conn.cursor()

    cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type IN ('table', 'virtual') "
        "AND name='rag_embeddings'"
    )
    result = cursor.fetchone()

    if result is None:
        logger.debug(
            f"rag_embeddings table does not exist - will create with "
            f"dimension {required_dimension}"
        )
        return True

    create_sql = result[0]
    logger.debug(f"Found existing rag_embeddings table: {create_sql}")

    import re

    dimension_match = re.search(r"FLOAT\[(\d+)\]", create_sql)

    if dimension_match:
        current_dim = int(dimension_match.group(1))
        logger.info(
            f"Current embedding table dimension: {current_dim}, "
            f"Required dimension: {required_dimension}"
        )

        if current_dim != required_dimension:
            logger.warning("Embedding dimension mismatch detected!")
            logger.warning(f"  Current table: {current_dim} dimensions")
            logger.warning(f"  Config expects: {required_dimension} dimensions")
            logger.info(
                f"Will trigger migration from {current_dim}D to "
                f"{required_dimension}D"
            )
            return False
        else:
            logger.debug(
                f"Embedding dimensions match ({current_dim}D) - no "
                f"migration needed"
            )
            return True
    else:
        logger.warning(f"Could not parse dimension from table schema: {create_sql}")
        logger.warning("Assuming incompatible and will recreate table for safety")
        return False


def handle_embedding_dimension_change(conn: sqlite3.Connection) -> None:
    """Drop and recreate the embeddings table when the configured
    embedding dimension changes between runs. Existing embeddings are
    deleted so the indexer will re-embed everything next pass."""
    required_dimension = embedding_settings().dimension
    cursor = conn.cursor()

    logger.info("=" * 60)
    logger.info("STARTING EMBEDDING DIMENSION MIGRATION")
    logger.info("=" * 60)

    try:
        cursor.execute("SELECT COUNT(*) FROM rag_embeddings")
        old_embedding_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM rag_chunks")
        chunk_count = cursor.fetchone()[0]
        logger.info("Migration stats:")
        logger.info(f"   - Existing embeddings: {old_embedding_count}")
        logger.info(f"   - Text chunks: {chunk_count}")
    except Exception as e:
        logger.debug(f"Could not get pre-migration stats: {e}")
        old_embedding_count = "unknown"
        chunk_count = "unknown"

    logger.info("Removing old embeddings and vector table...")

    try:
        cursor.execute("DELETE FROM rag_embeddings")
        logger.debug("Deleted all existing embeddings")

        cursor.execute("DROP TABLE IF EXISTS rag_embeddings")
        logger.debug("Dropped old rag_embeddings table")

        cursor.execute("DELETE FROM rag_meta WHERE meta_key LIKE 'hash_%'")
        hash_count = cursor.rowcount
        logger.debug(f"Cleared {hash_count} stored file hashes")

        cursor.execute(
            "UPDATE rag_meta SET meta_value = '1970-01-01T00:00:00Z' "
            "WHERE meta_key LIKE 'last_indexed_%'"
        )
        timestamp_count = cursor.rowcount
        logger.debug(f"Reset {timestamp_count} indexing timestamps")

        conn.commit()

        logger.info("Migration preparation completed successfully")
        logger.info("Next steps:")
        logger.info(
            f"   - New vector table will be created with "
            f"{required_dimension} dimensions"
        )
        logger.info(
            f"   - RAG indexer will automatically re-process all "
            f"{chunk_count} chunks"
        )
        logger.info("   - This may take a few minutes and will use OpenAI API tokens")
        logger.info("=" * 60)

    except Exception as e:
        logger.error(f"Error during migration: {e}")
        conn.rollback()
        raise RuntimeError(f"Embedding dimension migration failed: {e}") from e


def _emit_rag_embeddings_virtual_table(conn: sqlite3.Connection) -> None:
    """Create the sqlite-vec `rag_embeddings` virtual table.

    This shape can't be modelled in the SQLAlchemy ORM (vec0 is a
    virtual table module, not a regular table). We keep the raw DDL
    here, gated on the same runtime VSS-loadability check the
    pre-PR-W3 init_database() used.
    """
    dimension = embedding_settings().dimension
    if not isinstance(dimension, int) or dimension <= 0:
        raise ValueError(f"Invalid embedding dimension: {dimension}")

    cursor = conn.cursor()
    # dimension is validated above; safe to f-string.
    create_table_sql = (
        f"CREATE VIRTUAL TABLE IF NOT EXISTS rag_embeddings USING vec0("
        f"embedding FLOAT[{dimension}])"
    )
    cursor.execute(create_table_sql)
    logger.info(
        f"Vector table 'rag_embeddings' (using vec0 with dimension "
        f"{dimension}) ensured."
    )


def _seed_rag_meta_defaults(conn: sqlite3.Connection) -> None:
    """Insert the canonical `last_indexed_*` rows into rag_meta if absent."""
    cursor = conn.cursor()
    cursor.executemany(
        "INSERT OR IGNORE INTO rag_meta (meta_key, meta_value) VALUES (?, ?)",
        _DEFAULT_RAG_META_ENTRIES,
    )


def init_database() -> None:
    """Initialise the SQLite database for the current project.

    Driven by `Base.metadata.create_all()` against every model
    registered in `agent_mcp.db.models`. The legacy raw-SQL
    `CREATE TABLE` blocks all moved onto their ORM classes (PR-W3).

    The sqlite-vec virtual table is the one shape that can't live on
    a regular Declarative model — it's still emitted via raw DDL,
    gated on the VSS-loadability runtime check.
    """
    logger.info("Initializing database schema...")

    if not check_vss_loadability():
        logger.warning(
            "Initial VSS loadability check failed or VSS not available. "
            "RAG virtual table might not be created."
        )

    vss_is_actually_loadable = is_vss_loadable()

    conn = None
    try:
        # Open the connection first so the sqlite-vec extension is
        # loaded (the get_db_connection() helper handles that) and
        # the PRAGMA block runs.
        conn = get_db_connection()

        # Drive the canonical schema from the ORM. This is the
        # single source of truth for every regular (non-virtual)
        # table and its indexes. Mirrors the engine.py PRAGMAs so
        # behaviour is identical to opening through get_engine().
        engine = get_engine()
        Base.metadata.create_all(engine)
        logger.debug("ORM-defined tables and indexes ensured.")

        # Seed default rag_meta rows. Must run after create_all()
        # so the table exists.
        _seed_rag_meta_defaults(conn)
        logger.debug("Rag_meta default entries ensured.")

        # Virtual table for vec0 embeddings — kept as raw DDL.
        if vss_is_actually_loadable:
            if not check_embedding_dimension_compatibility(conn):
                logger.warning(
                    "Embedding dimension has changed. Recreating "
                    "embeddings table..."
                )
                handle_embedding_dimension_change(conn)

            try:
                _emit_rag_embeddings_virtual_table(conn)
            except sqlite3.OperationalError as e_vec:
                logger.error(
                    f"Failed to create VIRTUAL vector table "
                    f"'rag_embeddings': {e_vec}. RAG search functionality "
                    f"will be impaired."
                )
            except Exception as e_vec_other:
                logger.error(
                    f"Unexpected error creating vector table "
                    f"'rag_embeddings': {e_vec_other}",
                    exc_info=True,
                )
        else:
            logger.warning(
                "Skipping creation of RAG virtual table 'rag_embeddings' "
                "as sqlite-vec extension is not loadable or available."
            )

        conn.commit()
        logger.info("Database schema initialized successfully.")

    except sqlite3.Error as e:
        logger.error(
            f"A database error occurred during schema initialization: {e}",
            exc_info=True,
        )
        if conn:
            conn.rollback()
        raise RuntimeError(f"Failed to initialize database schema: {e}") from e
    except Exception as e:
        logger.error(
            f"An unexpected error occurred during schema initialization: {e}",
            exc_info=True,
        )
        if conn:
            conn.rollback()
        raise RuntimeError(
            f"Unexpected error during database schema initialization: {e}"
        ) from e
    finally:
        if conn:
            conn.close()
            logger.debug(
                "Database connection closed after schema initialization."
            )
