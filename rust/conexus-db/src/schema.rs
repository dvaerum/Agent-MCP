//! Schema DDL for tables owned by `conexus-db`'s repositories.
//!
//! Source of truth for the REAL schema stays the Python SQLAlchemy
//! ORM (`agent_mcp/db/models/*.py`) — confirmed by
//! `tests/test_orm_is_source_of_truth.py` — and Alembic remains the
//! authoritative migration owner until every Python backend is
//! decommissioned (Phase F). This DDL exists only so Rust unit/
//! differential tests can stand up a throwaway SQLite file shaped
//! exactly like a real one; it is never run against a live project
//! database.

use rusqlite::{Connection, Result};

/// Create every table this crate's repositories touch, if not already
/// present. Idempotent — safe to call against an already-migrated
/// database (a no-op in that case).
pub fn init_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS agents (
            token                TEXT PRIMARY KEY,
            agent_id             TEXT UNIQUE NOT NULL,
            created_at           TEXT NOT NULL,
            status               TEXT NOT NULL,
            current_task         TEXT,
            working_directory    TEXT NOT NULL,
            color                TEXT,
            terminated_at        TEXT,
            updated_at           TEXT,
            aoe_session_id       TEXT,
            auto_event_loop      INTEGER NOT NULL DEFAULT 1,
            last_event_seen_at   TEXT,
            last_activity_at     TEXT,
            agent_role           TEXT NOT NULL DEFAULT 'worker'
                                 CHECK (agent_role IN ('worker', 'manager')),
            profile              TEXT,
            profile_updated_at   TEXT,
            profile_reviewed_at  TEXT,
            profile_updated_by   TEXT
        );

        CREATE TABLE IF NOT EXISTS project_context (
            context_key   TEXT PRIMARY KEY,
            value         TEXT NOT NULL,
            description   TEXT,
            created_at    TEXT,
            created_by    TEXT,
            updated_at    TEXT NOT NULL,
            updated_by    TEXT NOT NULL
        );
        "#,
    )
}

/// Create the ROUTER-database tables `group_capability_repository`
/// touches. Deliberately separate from [`init_schema`] above: `agents`/
/// `project_context` live in the per-project agent DB
/// (`<project_dir>/.agent/mcp_state.db`), while `groups`/
/// `group_capability` live in the entirely different router DB
/// (`router.db`) — two physically separate SQLite files in
/// production, whose schemas happen to be owned by two different
/// Alembic migration chains (`agent_mcp/db/` vs.
/// `agent_mcp/router/migrations/`). A test standing up an in-memory
/// router-DB-shaped connection should call this, not [`init_schema`],
/// to accurately reflect what tables actually coexist on that file.
pub fn init_router_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS groups (
            group_id     TEXT PRIMARY KEY,
            name         TEXT NOT NULL UNIQUE,
            is_sysadmin  INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS group_capability (
            group_id    TEXT NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
            capability  TEXT NOT NULL,
            PRIMARY KEY (group_id, capability)
        );
        "#,
    )
}
