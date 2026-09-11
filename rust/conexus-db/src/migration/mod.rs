//! Schema-authority replacement for Alembic (Phase F, operator-decided:
//! `sea-orm-migration`, not a design still to be picked -- see the
//! plan's own "Migration-tool detail: sea-orm-migration" section).
//!
//! Two independent [`sea_orm_migration::MigratorTrait`] implementations,
//! matching the two physically separate SQLite files this workspace has
//! always kept apart (`schema::init_schema` vs. `schema::
//! init_router_schema`, same split): [`Migrator`] owns the per-project
//! `.agent/mcp_state.db` schema, [`RouterMigrator`] owns `router.db`.
//!
//! **Baseline, not a 32-file replay**: rather than re-deriving each of
//! Alembic's 26 (per-project) + 6 (router) historical migrations —
//! most of which exist only to get from an EARLIER shape to the
//! CURRENT one, and several of which are genuinely destructive/
//! forward-only (orphan deletes, a hard-cutover table move) that must
//! never be replayed against data that already went through them once
//! — each `Migrator` ships exactly ONE migration: the complete
//! current-HEAD schema. A fresh install runs it for real. The 3 real
//! production databases (2 project DBs + `router.db`), already
//! confirmed live at Alembic head (`0026_rename_task_notes_to_task_
//! comments` / `0006_group_membership_unique`) before this module was
//! written, get their `seaql_migrations` tracking table seeded to mark
//! the baseline as already-applied — see the migration's own module
//! doc for the seeding procedure and why it must never simply `up()`
//! against a live file directly.
//!
//! Verified via direct correctness checks: `PRAGMA foreign_key_check`
//! never firing after `up()`; a real trigger enforcement test (attempt
//! a completed-task's own protected fields, terminal_task_guard fires);
//! `PRAGMA table_info` per table diffed against the confirmed Alembic-
//! head shape.

mod m20260911_000001_baseline;
mod m20260911_000002_router_baseline;

pub use sea_orm_migration::prelude::*;

pub struct Migrator;

#[async_trait::async_trait]
impl MigratorTrait for Migrator {
    fn migrations() -> Vec<Box<dyn MigrationTrait>> {
        vec![Box::new(m20260911_000001_baseline::Migration)]
    }
}

pub struct RouterMigrator;

#[async_trait::async_trait]
impl MigratorTrait for RouterMigrator {
    fn migrations() -> Vec<Box<dyn MigrationTrait>> {
        vec![Box::new(m20260911_000002_router_baseline::Migration)]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use sea_orm::{ConnectionTrait, Database, Statement};

    async fn fresh_db() -> sea_orm::DatabaseConnection {
        let db = Database::connect("sqlite::memory:").await.unwrap();
        db.execute_unprepared("PRAGMA foreign_keys = ON;")
            .await
            .unwrap();
        db
    }

    async fn table_names(db: &sea_orm::DatabaseConnection) -> Vec<String> {
        let stmt = Statement::from_string(
            db.get_database_backend(),
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name",
        );
        let rows = db.query_all_raw(stmt).await.unwrap();
        rows.into_iter()
            .map(|r| r.try_get::<String>("", "name").unwrap())
            .collect()
    }

    async fn index_sql(db: &sea_orm::DatabaseConnection, index_name: &str) -> String {
        let stmt = Statement::from_sql_and_values(
            db.get_database_backend(),
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            [index_name.into()],
        );
        let rows = db.query_all_raw(stmt).await.unwrap();
        rows[0].try_get::<String>("", "sql").unwrap()
    }

    #[tokio::test]
    async fn per_project_baseline_creates_every_table_including_mcp_sessions() {
        let db = fresh_db().await;
        Migrator::up(&db, None).await.unwrap();

        let tables = table_names(&db).await;
        for expected in [
            "agent_actions",
            "agent_messages",
            "agents",
            "claude_code_sessions",
            "file_metadata",
            "mcp_sessions",
            "pending_directive",
            "project_context",
            "project_settings",
            "rag_chunks",
            "rag_meta",
            "scheduled_directive",
            "task_comments",
            "tasks",
        ] {
            assert!(
                tables.contains(&expected.to_string()),
                "missing table {expected:?}; got {tables:?}"
            );
        }
    }

    #[tokio::test]
    async fn per_project_baseline_enforces_the_three_previously_missing_fks() {
        let db = fresh_db().await;
        Migrator::up(&db, None).await.unwrap();

        // agents.current_task -> tasks.task_id
        db.execute_unprepared(
            "INSERT INTO agents (token, agent_id, created_at, status, current_task, working_directory) \
             VALUES ('tok', 'a1', '2026-01-01T00:00:00Z', 'active', 'no-such-task', '/tmp')",
        )
        .await
        .expect_err("current_task FK must reject an unknown task_id");

        // tasks.parent_task -> tasks.task_id (self)
        db.execute_unprepared(
            "INSERT INTO tasks (task_id, title, created_by, status, priority, created_at, updated_at, parent_task) \
             VALUES ('t1', 'x', 'a1', 'unassigned', 'medium', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'no-such-parent')",
        )
        .await
        .expect_err("parent_task self-FK must reject an unknown task_id");

        // agent_messages.parent_message_id -> agent_messages.message_id, ON DELETE SET NULL
        db.execute_unprepared(
            "INSERT INTO tasks (task_id, title, created_by, status, priority, created_at, updated_at) \
             VALUES ('t2', 'x', 'a1', 'unassigned', 'medium', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
             INSERT INTO agent_messages (message_id, sender_id, recipient_id, message_content, timestamp) \
             VALUES ('m1', 'a1', 'a2', 'hi', '2026-01-01T00:00:00Z');
             INSERT INTO agent_messages (message_id, sender_id, recipient_id, message_content, timestamp, parent_message_id) \
             VALUES ('m2', 'a2', 'a1', 're: hi', '2026-01-01T00:00:01Z', 'm1');",
        )
        .await
        .unwrap();
        db.execute_unprepared("DELETE FROM agent_messages WHERE message_id = 'm1'")
            .await
            .unwrap();
        let stmt = Statement::from_string(
            db.get_database_backend(),
            "SELECT parent_message_id FROM agent_messages WHERE message_id = 'm2'",
        );
        let rows = db.query_all_raw(stmt).await.unwrap();
        let parent: Option<String> = rows[0].try_get("", "parent_message_id").unwrap();
        assert_eq!(
            parent, None,
            "ON DELETE SET NULL must clear the reply's parent_message_id"
        );
    }

    #[tokio::test]
    async fn per_project_baseline_desc_indexes_carry_the_real_sort_direction() {
        let db = fresh_db().await;
        Migrator::up(&db, None).await.unwrap();

        for (name, col) in [
            ("idx_tasks_assigned_to_updated_at", "updated_at"),
            ("idx_agent_messages_recipient_timestamp", "timestamp"),
            ("idx_agent_messages_sender_timestamp", "timestamp"),
            ("idx_agent_messages_unread", "timestamp"),
        ] {
            let sql = index_sql(&db, name).await;
            assert!(
                sql.to_uppercase()
                    .contains(&format!("{} DESC", col.to_uppercase())),
                "{name} must sort {col} DESC, got: {sql}"
            );
        }
    }

    #[tokio::test]
    async fn per_project_baseline_terminal_task_guard_trigger_still_fires() {
        let db = fresh_db().await;
        Migrator::up(&db, None).await.unwrap();

        db.execute_unprepared(
            "INSERT INTO tasks (task_id, title, created_by, status, priority, created_at, updated_at) \
             VALUES ('t1', 'x', 'a1', 'completed', 'medium', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
        )
        .await
        .unwrap();
        db.execute_unprepared("UPDATE tasks SET title = 'changed' WHERE task_id = 't1'")
            .await
            .expect_err("terminal_task_guard trigger must block editing a completed task's title");
    }

    #[tokio::test]
    async fn per_project_baseline_up_is_idempotent() {
        let db = fresh_db().await;
        Migrator::up(&db, None).await.unwrap();
        // A second `up()` must be a no-op (nothing pending), not an
        // attempt to re-run CREATE TABLE against tables that already
        // exist -- exactly the property the real seeding-and-cutover
        // procedure depends on.
        Migrator::up(&db, None).await.unwrap();
    }

    #[tokio::test]
    async fn router_baseline_creates_every_table() {
        let db = fresh_db().await;
        RouterMigrator::up(&db, None).await.unwrap();

        let tables = table_names(&db).await;
        for expected in [
            "group_capability",
            "group_membership",
            "groups",
            "project_membership",
            "sessions",
            "users",
        ] {
            assert!(
                tables.contains(&expected.to_string()),
                "missing table {expected:?}; got {tables:?}"
            );
        }
    }

    #[tokio::test]
    async fn router_baseline_exactly_one_of_check_constraints_hold() {
        let db = fresh_db().await;
        RouterMigrator::up(&db, None).await.unwrap();

        db.execute_unprepared(
            "INSERT INTO users (user_id, username, created_at) VALUES ('u1', 'alice', '2026-01-01T00:00:00Z');
             INSERT INTO groups (group_id, name, created_at) VALUES ('g1', 'admins', '2026-01-01T00:00:00Z');",
        )
        .await
        .unwrap();

        // group_membership: exactly one of member_user_id/member_group_id.
        db.execute_unprepared(
            "INSERT INTO group_membership (group_id, member_user_id, member_group_id, added_at) \
             VALUES ('g1', 'u1', 'g1', '2026-01-01T00:00:00Z')",
        )
        .await
        .expect_err("group_membership must reject both member_user_id and member_group_id set");
        db.execute_unprepared(
            "INSERT INTO group_membership (group_id, added_at) VALUES ('g1', '2026-01-01T00:00:00Z')",
        )
        .await
        .expect_err("group_membership must reject neither member_user_id nor member_group_id set");
        db.execute_unprepared(
            "INSERT INTO group_membership (group_id, member_user_id, added_at) \
             VALUES ('g1', 'u1', '2026-01-01T00:00:00Z')",
        )
        .await
        .unwrap();

        // project_membership: exactly one of user_id/group_id.
        db.execute_unprepared(
            "INSERT INTO project_membership (project_name, user_id, group_id) VALUES ('p1', 'u1', 'g1')",
        )
        .await
        .expect_err("project_membership must reject both user_id and group_id set");
    }

    #[tokio::test]
    async fn router_baseline_up_is_idempotent() {
        let db = fresh_db().await;
        RouterMigrator::up(&db, None).await.unwrap();
        RouterMigrator::up(&db, None).await.unwrap();
    }
}
