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

        CREATE TABLE IF NOT EXISTS project_settings (
            context_key   TEXT PRIMARY KEY,
            value         TEXT NOT NULL,
            description   TEXT,
            created_at    TEXT,
            created_by    TEXT,
            updated_at    TEXT NOT NULL,
            updated_by    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS file_metadata (
            filepath      TEXT PRIMARY KEY,
            metadata      TEXT NOT NULL,
            last_updated  TEXT NOT NULL,
            updated_by    TEXT NOT NULL,
            content_hash  TEXT
        );

        CREATE TABLE IF NOT EXISTS agent_actions (
            action_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id      TEXT NOT NULL,
            action_type   TEXT NOT NULL,
            task_id       TEXT,
            timestamp     TEXT NOT NULL,
            details       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_agent_actions_agent_id_timestamp
            ON agent_actions (agent_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_agent_actions_task_id_timestamp
            ON agent_actions (task_id, timestamp);

        CREATE TABLE IF NOT EXISTS pending_directive (
            poke_id       TEXT PRIMARY KEY,
            agent_id      TEXT NOT NULL,
            prompt        TEXT NOT NULL,
            priority      TEXT NOT NULL DEFAULT 'urgent',
            created_at    TEXT NOT NULL,
            created_by    TEXT,
            delivered_at  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pending_directive_undelivered
            ON pending_directive (agent_id, delivered_at);

        CREATE TABLE IF NOT EXISTS scheduled_directive (
            directive_id      TEXT PRIMARY KEY,
            agent_id          TEXT NOT NULL,
            prompt            TEXT NOT NULL,
            interval_seconds  INTEGER NOT NULL,
            next_due_at       TEXT NOT NULL,
            enabled           INTEGER NOT NULL DEFAULT 1,
            status            TEXT NOT NULL DEFAULT 'active',
            until_at          TEXT,
            max_runs          INTEGER,
            run_count         INTEGER NOT NULL DEFAULT 0,
            created_at        TEXT NOT NULL,
            created_by        TEXT,
            updated_at        TEXT,
            updated_by        TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_scheduled_directive_due
            ON scheduled_directive (agent_id, enabled, next_due_at);

        CREATE TABLE IF NOT EXISTS rag_chunks (
            chunk_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type  TEXT NOT NULL,
            source_ref   TEXT NOT NULL,
            chunk_text   TEXT NOT NULL,
            indexed_at   TEXT NOT NULL,
            metadata     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_rag_chunks_source_type_ref
            ON rag_chunks (source_type, source_ref);

        CREATE TABLE IF NOT EXISTS rag_meta (
            meta_key    TEXT PRIMARY KEY,
            meta_value  TEXT
        );

        CREATE TABLE IF NOT EXISTS agent_messages (
            message_id          TEXT PRIMARY KEY,
            sender_id           TEXT NOT NULL,
            recipient_id        TEXT NOT NULL,
            message_content     TEXT NOT NULL,
            message_type        TEXT NOT NULL DEFAULT 'text',
            priority            TEXT NOT NULL DEFAULT 'normal',
            timestamp           TEXT NOT NULL,
            delivered           INTEGER NOT NULL DEFAULT 0,
            read                INTEGER NOT NULL DEFAULT 0,
            subject             TEXT,
            parent_message_id   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_agent_messages_recipient_timestamp
            ON agent_messages (recipient_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_agent_messages_sender_timestamp
            ON agent_messages (sender_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_agent_messages_unread
            ON agent_messages (recipient_id, read, timestamp);
        CREATE INDEX IF NOT EXISTS idx_agent_messages_delivered
            ON agent_messages (delivered);
        CREATE INDEX IF NOT EXISTS idx_agent_messages_parent
            ON agent_messages (parent_message_id);

        CREATE TABLE IF NOT EXISTS tasks (
            task_id            TEXT PRIMARY KEY,
            title              TEXT NOT NULL,
            description        TEXT,
            assigned_to        TEXT,
            created_by         TEXT NOT NULL,
            status             TEXT NOT NULL,
            priority           TEXT NOT NULL,
            created_at         TEXT NOT NULL,
            updated_at         TEXT NOT NULL,
            parent_task        TEXT,
            child_tasks        TEXT,
            depends_on_tasks   TEXT,
            notes              TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_assigned_to_updated_at
            ON tasks (assigned_to, updated_at);
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status);
        CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks (priority);
        -- Single-root-task invariant (R15-BL-1): a plain UNIQUE(parent_task)
        -- wouldn't work because SQLite treats every NULL as distinct: an
        -- expression index on the constant boolean `(parent_task IS NULL)`,
        -- filtered to only rows where it's true, makes every root task
        -- collide on the same indexed value, so a second root violates
        -- uniqueness.
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_single_root
            ON tasks ((parent_task IS NULL)) WHERE parent_task IS NULL;

        -- Verbatim from agent_mcp/migrations/versions/0025_terminal_task_guard_trigger.py's
        -- _TASKS_SQL. Only the `tasks`-table trigger is ported here —
        -- the two `task_notes`/`task_comments` side-table triggers
        -- from that same migration are deliberately NOT included:
        -- comments are owned by a different, not-yet-ported module
        -- (`db/actions/task_comments_db.py`, not one of the 8
        -- `agent_mcp/repositories/*.py` files Phase B covers), so
        -- creating that table/trigger pair here would be scope creep
        -- into a module this phase doesn't touch.
        CREATE TRIGGER IF NOT EXISTS trg_tasks_terminal_state_guard
        BEFORE UPDATE ON tasks
        FOR EACH ROW
        WHEN OLD.status IN ('completed', 'cancelled', 'failed')
          AND (
            NEW.status IS NOT OLD.status
            OR NEW.priority IS NOT OLD.priority
            OR NEW.notes IS NOT OLD.notes
            OR NEW.title IS NOT OLD.title
            OR NEW.description IS NOT OLD.description
            OR (NEW.assigned_to IS NOT OLD.assigned_to AND NEW.assigned_to IS NOT NULL)
          )
        BEGIN
          SELECT RAISE(ABORT, 'terminal_task_guard: task is in a terminal state (completed/cancelled/failed); status/priority/notes/title/description are frozen and assigned_to may only be cleared, never reassigned');
        END;
        "#,
    )
}

/// Create the `rag_embeddings` sqlite-vec `vec0` virtual table.
/// Deliberately NOT part of [`init_schema`] above: unlike every other
/// table there, this one requires the sqlite-vec extension to already
/// be registered on the process (see `conexus-vec`), and forcing that
/// dependency onto every unrelated repository's tests (agents,
/// project_context, ...) would be wrong — this table is a
/// `rag_repository`-specific opt-in, matching Python's own schema
/// bootstrap, which also creates this table conditionally, separate
/// from `Base.metadata.create_all()`'s unconditional ORM tables.
pub fn init_rag_embeddings_table(conn: &Connection, dimension: u32) -> Result<()> {
    conn.execute_batch(&format!(
        "CREATE VIRTUAL TABLE IF NOT EXISTS rag_embeddings USING vec0(embedding FLOAT[{dimension}])"
    ))
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

        -- Edge in the group-membership graph (ported from
        -- `agent_mcp/router/migrations/versions/0002_groups_and_roles.py`
        -- + `0006_group_membership_unique.py`), needed by
        -- `group_membership_repository::resolve_user_groups` for
        -- `conexus-auth`'s `resolve_capabilities`. Each edge is EITHER a
        -- user-into-group or a group-into-group membership; the CHECK
        -- constraint enforces exactly-one-set at the storage layer, same
        -- as Python. `member_user_id` deliberately has NO `REFERENCES
        -- users(user_id)` here (unlike the real Alembic migration) --
        -- the `users` table itself isn't ported to this crate yet (it
        -- belongs to Phase E2's router port); add the FK back when it
        -- is.
        CREATE TABLE IF NOT EXISTS group_membership (
            group_id         TEXT NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
            member_user_id   TEXT,
            member_group_id  TEXT REFERENCES groups(group_id) ON DELETE CASCADE,
            added_at         TEXT NOT NULL,
            CHECK ((member_user_id IS NOT NULL) <> (member_group_id IS NOT NULL))
        );

        CREATE INDEX IF NOT EXISTS idx_group_membership_group_id
            ON group_membership(group_id);
        CREATE INDEX IF NOT EXISTS idx_group_membership_member_user_id
            ON group_membership(member_user_id);
        CREATE INDEX IF NOT EXISTS idx_group_membership_member_group_id
            ON group_membership(member_group_id);

        CREATE UNIQUE INDEX IF NOT EXISTS uq_group_membership_user
            ON group_membership(group_id, member_user_id)
            WHERE member_user_id IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_group_membership_group
            ON group_membership(group_id, member_group_id)
            WHERE member_group_id IS NOT NULL;
        "#,
    )
}
