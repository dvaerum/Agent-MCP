//! Per-project database (`.agent/mcp_state.db`) baseline migration --
//! the complete schema at Alembic's real HEAD revision
//! (`0026_rename_task_notes_to_task_comments`), confirmed live against
//! both production project databases before this file was written
//! (`SELECT version_num FROM alembic_version` on both returned exactly
//! that revision).
//!
//! Starts from `crate::schema::init_schema`'s already-tested DDL
//! (reused verbatim below via `execute_unprepared`, confirmed to
//! support multi-statement strings against the real sqlx-sqlite
//! backend) and closes the 3 gaps a direct migration-history audit
//! found between that DDL and the true post-migration-26 shape (see
//! the plan's own "schema.rs vs. Alembic HEAD" research):
//!
//! 1. `mcp_sessions` -- created by Alembic 0004/0005, entirely absent
//!    from `schema.rs` and from every sea-orm `Entity`. Added here
//!    with its real ORM shape (`agent_mcp/db/models/mcp_session.py`)
//!    and its FK to `agents.agent_id` (Alembic 0008).
//! 2. Three FK constraints Alembic 0007/0008/0012 added and 0014
//!    never dropped, omitted from `schema.rs` only because the Python
//!    ORM models ALSO omit them (by explicit, documented design --
//!    declaring them would race `create_all()` against Alembic's own
//!    DDL): `agents.current_task -> tasks.task_id`,
//!    `tasks.parent_task -> tasks.task_id` (self), `agent_messages.
//!    parent_message_id -> agent_messages.message_id` (self,
//!    `ON DELETE SET NULL`).
//! 3. Four composite indexes Alembic's table-rebuild migrations
//!    (0007/0008/0012/0014) restore a `DESC` sort direction on every
//!    time SQLite's `batch_alter_table` strips it on rebuild;
//!    `schema.rs` (and the Python ORM's own `Index()` declarations)
//!    both use plain ascending DDL. Functional risk is low (SQLite
//!    can satisfy `ORDER BY ... DESC` via a backward scan of an
//!    ascending index for these single-sort-column cases), but this
//!    baseline matches the real on-disk shape exactly rather than
//!    silently carrying the drift forward.

use sea_orm_migration::prelude::*;

#[derive(DeriveMigrationName)]
pub struct Migration;

#[async_trait::async_trait]
impl MigrationTrait for Migration {
    async fn up(&self, manager: &SchemaManager) -> Result<(), DbErr> {
        let db = manager.get_connection();
        db.execute_unprepared(
            r#"
            CREATE TABLE IF NOT EXISTS agents (
                token                TEXT PRIMARY KEY,
                agent_id             TEXT UNIQUE NOT NULL,
                created_at           TEXT NOT NULL,
                status               TEXT NOT NULL,
                current_task         TEXT REFERENCES tasks(task_id),
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

            CREATE TABLE IF NOT EXISTS claude_code_sessions (
                session_id         TEXT PRIMARY KEY,
                pid                INTEGER NOT NULL,
                parent_pid         INTEGER NOT NULL,
                first_detected     TEXT NOT NULL,
                last_activity      TEXT NOT NULL,
                working_directory  TEXT,
                agent_id           TEXT,
                status             TEXT DEFAULT 'detected',
                git_commits        TEXT,
                metadata           TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_claude_sessions_pid
                ON claude_code_sessions (pid, parent_pid);
            CREATE INDEX IF NOT EXISTS idx_claude_sessions_activity
                ON claude_code_sessions (last_activity);
            CREATE INDEX IF NOT EXISTS idx_claude_sessions_agent
                ON claude_code_sessions (agent_id);
            CREATE INDEX IF NOT EXISTS idx_claude_sessions_status
                ON claude_code_sessions (status);

            -- Alembic 0004/0005, never ported to schema.rs or any
            -- sea-orm Entity until this baseline (see module doc gap
            -- 1). Shape matches agent_mcp/db/models/mcp_session.py
            -- exactly; `agent_id` carries the real FK Alembic 0008
            -- added.
            CREATE TABLE IF NOT EXISTS mcp_sessions (
                session_id          TEXT PRIMARY KEY,
                agent_id            TEXT NOT NULL REFERENCES agents(agent_id),
                opened_at           TEXT NOT NULL,
                last_seen_at        TEXT NOT NULL,
                bearer_token_hash   TEXT NOT NULL,
                alias_used          TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_mcp_sessions_agent
                ON mcp_sessions (agent_id);
            CREATE INDEX IF NOT EXISTS idx_mcp_sessions_last_seen
                ON mcp_sessions (last_seen_at);
            CREATE INDEX IF NOT EXISTS idx_mcp_sessions_alias_used
                ON mcp_sessions (alias_used, last_seen_at);

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

            -- `parent_message_id` FK is gap 2 (Alembic 0012, survives
            -- 0014); the 3 DESC-sorted indexes below are gap 3.
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
                parent_message_id   TEXT REFERENCES agent_messages(message_id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_agent_messages_recipient_timestamp
                ON agent_messages (recipient_id, timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_agent_messages_sender_timestamp
                ON agent_messages (sender_id, timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_agent_messages_unread
                ON agent_messages (recipient_id, read, timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_agent_messages_delivered
                ON agent_messages (delivered);
            CREATE INDEX IF NOT EXISTS idx_agent_messages_parent
                ON agent_messages (parent_message_id);

            -- `parent_task` FK is gap 2 (Alembic 0007, survives 0014);
            -- the assigned_to/updated_at DESC index is gap 3.
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
                parent_task        TEXT REFERENCES tasks(task_id),
                child_tasks        TEXT,
                depends_on_tasks   TEXT,
                notes              TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_assigned_to_updated_at
                ON tasks (assigned_to, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status);
            CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks (priority);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_single_root
                ON tasks ((parent_task IS NULL)) WHERE parent_task IS NULL;

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

            CREATE TABLE IF NOT EXISTS task_comments (
                note_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id     TEXT NOT NULL,
                author      TEXT,
                timestamp   TEXT NOT NULL,
                text        TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_task_comments_task
                ON task_comments (task_id);

            CREATE TRIGGER IF NOT EXISTS trg_task_comments_terminal_guard_insert
            BEFORE INSERT ON task_comments
            FOR EACH ROW
            WHEN (SELECT status FROM tasks WHERE task_id = NEW.task_id) IN ('completed', 'cancelled', 'failed')
            BEGIN
              SELECT RAISE(ABORT, 'terminal_task_guard: cannot add a task_comment; parent task is in a terminal state (completed/cancelled/failed)');
            END;
            CREATE TRIGGER IF NOT EXISTS trg_task_comments_terminal_guard_update
            BEFORE UPDATE ON task_comments
            FOR EACH ROW
            WHEN (SELECT status FROM tasks WHERE task_id = OLD.task_id) IN ('completed', 'cancelled', 'failed')
            BEGIN
              SELECT RAISE(ABORT, 'terminal_task_guard: cannot edit a task_comment; parent task is in a terminal state (completed/cancelled/failed)');
            END;
            "#,
        )
        .await?;
        Ok(())
    }
}
