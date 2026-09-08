//! `claude_code_sessions` table -- port of the DB half of
//! `agent_mcp/features/claude_session_monitor.py`. Tracks Claude Code
//! sessions discovered via `.agent/registry.json` (the git-agentmcp
//! hook's own coordination file), independent of this crate's
//! `agents`/MCP-session concepts entirely -- a session here is a
//! detected Claude Code *process*, not an MCP bearer.
//!
//! Phase G (sea-orm migration): the third repository converted. Every
//! function here is simple single-table CRUD against
//! `claude_code_sessions` alone (unlike `task_comments_repository`'s
//! `task_status`, there's no second table to reach for here), so
//! sea-orm's builder API covers it natively -- no raw-SQL escape hatch
//! needed.

use sea_orm::{
    ActiveValue::Set, ColumnTrait, DatabaseConnection, DbErr, EntityTrait, QueryFilter, QueryOrder,
};

pub use crate::entity::claude_code_session::Model as ClaudeCodeSessionRow;
use crate::entity::claude_code_session::{ActiveModel, Column, Entity};

pub async fn get_by_id(
    db: &DatabaseConnection,
    session_id: &str,
) -> Result<Option<ClaudeCodeSessionRow>, DbErr> {
    Entity::find_by_id(session_id.to_string()).one(db).await
}

/// Fields for a newly-detected (or re-detected -- `INSERT OR REPLACE`,
/// matching Python exactly) session.
pub struct NewSession<'a> {
    pub session_id: &'a str,
    pub pid: i64,
    pub parent_pid: i64,
    pub working_directory: Option<&'a str>,
    pub metadata: &'a str,
}

/// `INSERT OR REPLACE` a session row with `status = 'detected'` and
/// both timestamps set to `now` (or `last_activity` from the
/// caller-supplied value when the registry entry carries its own,
/// matching Python's `session_data.get("last_activity", now)`).
///
/// The `ON CONFLICT` clause deliberately lists EVERY column but the
/// primary key, including `agent_id`/`git_commits` (never part of
/// this function's own INSERT values, always `Set(None)` here) --
/// SQLite's real `INSERT OR REPLACE` deletes the conflicting row
/// outright before inserting the new one, so a re-detected session
/// loses any `agent_id`/`git_commits` a *different* writer had set on
/// it, not just leaves them alone. Dropping either column from
/// `update_columns` would silently change that to a preserve-on-
/// conflict merge instead, which is NOT what the rusqlite version
/// this replaces did (same reasoning `file_metadata_repository::
/// upsert`'s `content_hash` column already established for this exact
/// migration).
pub async fn register_new_session(
    db: &DatabaseConnection,
    session: &NewSession<'_>,
    last_activity: &str,
    now: &str,
) -> Result<(), DbErr> {
    let am = ActiveModel {
        session_id: Set(session.session_id.to_string()),
        pid: Set(session.pid),
        parent_pid: Set(session.parent_pid),
        first_detected: Set(now.to_string()),
        last_activity: Set(last_activity.to_string()),
        working_directory: Set(session.working_directory.map(str::to_string)),
        agent_id: Set(None),
        status: Set(Some("detected".to_string())),
        git_commits: Set(None),
        metadata: Set(Some(session.metadata.to_string())),
    };
    Entity::insert(am)
        .on_conflict(
            sea_orm::sea_query::OnConflict::column(Column::SessionId)
                .update_columns([
                    Column::Pid,
                    Column::ParentPid,
                    Column::FirstDetected,
                    Column::LastActivity,
                    Column::WorkingDirectory,
                    Column::AgentId,
                    Column::Status,
                    Column::GitCommits,
                    Column::Metadata,
                ])
                .to_owned(),
        )
        .exec(db)
        .await?;
    Ok(())
}

/// Refresh an existing session's activity/metadata, flipping it back
/// to `'active'` (a session that drifted to some other status while
/// still present in the registry is corrected here, matching Python's
/// unconditional `SET ... status = 'active'`).
pub async fn update_activity(
    db: &DatabaseConnection,
    session_id: &str,
    last_activity: &str,
    metadata: &str,
) -> Result<bool, DbErr> {
    let result = Entity::update_many()
        .col_expr(
            Column::LastActivity,
            sea_orm::sea_query::Expr::value(last_activity),
        )
        .col_expr(Column::Metadata, sea_orm::sea_query::Expr::value(metadata))
        .col_expr(Column::Status, sea_orm::sea_query::Expr::value("active"))
        .filter(Column::SessionId.eq(session_id))
        .exec(db)
        .await?;
    Ok(result.rows_affected > 0)
}

/// A session that dropped out of the registry (process exited, or the
/// hook stopped reporting it) -- marks it `'inactive'` without
/// deleting the row (history preserved, matching Python).
pub async fn mark_inactive(
    db: &DatabaseConnection,
    session_id: &str,
    now: &str,
) -> Result<bool, DbErr> {
    let result = Entity::update_many()
        .col_expr(Column::Status, sea_orm::sea_query::Expr::value("inactive"))
        .col_expr(Column::LastActivity, sea_orm::sea_query::Expr::value(now))
        .filter(Column::SessionId.eq(session_id))
        .exec(db)
        .await?;
    Ok(result.rows_affected > 0)
}

/// BL-R4-2: purging an agent must not leave a dangling
/// `claude_code_sessions` row referencing it -- `agent_id` has no
/// write path in this crate's own monitor (`register_new_session`
/// never sets it, matching Python's real INSERT, which also omits the
/// column), but the column exists and Python's own migrations FK it
/// to `agents.agent_id`, so ANY writer (this crate's own future code,
/// a raw-SQL fixture, a schema drift) that does populate it must still
/// be cleaned up on purge -- matching the observable "no row survives
/// referencing a deleted agent" contract, not just the current write
/// path's own behavior. Deletes rather than reattributing to a
/// tombstone (unlike `agent_actions`) since a session record is a
/// disposable liveness signal, not an audit trail Python preserves.
pub async fn delete_by_agent_id(db: &DatabaseConnection, agent_id: &str) -> Result<usize, DbErr> {
    let result = Entity::delete_many()
        .filter(Column::AgentId.eq(agent_id))
        .exec(db)
        .await?;
    Ok(result.rows_affected as usize)
}

/// Every session currently `'detected'` or `'active'`, newest-activity
/// first.
pub async fn list_active(db: &DatabaseConnection) -> Result<Vec<ClaudeCodeSessionRow>, DbErr> {
    Entity::find()
        .filter(Column::Status.is_in(["detected", "active"]))
        .order_by_desc(Column::LastActivity)
        .all(db)
        .await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::init_schema;
    use sea_orm::Database;

    async fn test_conn() -> (tempfile::TempDir, DatabaseConnection) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.db");
        {
            let c = rusqlite::Connection::open(&path).unwrap();
            init_schema(&c).unwrap();
        }
        let db = Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        (dir, db)
    }

    #[tokio::test]
    async fn register_new_session_creates_a_detected_row() {
        let (_dir, db) = test_conn().await;
        register_new_session(
            &db,
            &NewSession {
                session_id: "s1",
                pid: 111,
                parent_pid: 222,
                working_directory: Some("/repo"),
                metadata: "{}",
            },
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();

        let row = get_by_id(&db, "s1").await.unwrap().unwrap();
        assert_eq!(row.pid, 111);
        assert_eq!(row.parent_pid, 222);
        assert_eq!(row.status.as_deref(), Some("detected"));
        assert_eq!(row.working_directory.as_deref(), Some("/repo"));
    }

    #[tokio::test]
    async fn register_new_session_is_insert_or_replace_on_a_re_detected_id() {
        let (_dir, db) = test_conn().await;
        let seed = NewSession {
            session_id: "s1",
            pid: 111,
            parent_pid: 222,
            working_directory: Some("/repo"),
            metadata: "{}",
        };
        register_new_session(&db, &seed, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
            .await
            .unwrap();
        // Re-detected with a different pid (process restarted under
        // the same session_id) -- must overwrite, not conflict.
        let seed2 = NewSession { pid: 999, ..seed };
        register_new_session(&db, &seed2, "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z")
            .await
            .unwrap();

        let row = get_by_id(&db, "s1").await.unwrap().unwrap();
        assert_eq!(row.pid, 999);
    }

    /// `register_new_session`'s `ON CONFLICT` clause must reproduce
    /// `INSERT OR REPLACE`'s real semantics: a re-detected session
    /// WIPES any `agent_id`/`git_commits` a different writer had set,
    /// it does not preserve them.
    #[tokio::test]
    async fn register_new_session_wipes_agent_id_and_git_commits_on_re_detection() {
        let (_dir, db) = test_conn().await;
        let am = ActiveModel {
            session_id: Set("s1".to_string()),
            pid: Set(1),
            parent_pid: Set(2),
            first_detected: Set("2026-01-01T00:00:00Z".to_string()),
            last_activity: Set("2026-01-01T00:00:00Z".to_string()),
            working_directory: Set(None),
            agent_id: Set(Some("alice".to_string())),
            status: Set(Some("active".to_string())),
            git_commits: Set(Some("deadbeef".to_string())),
            metadata: Set(Some("{}".to_string())),
        };
        Entity::insert(am).exec(&db).await.unwrap();

        register_new_session(
            &db,
            &NewSession {
                session_id: "s1",
                pid: 2,
                parent_pid: 3,
                working_directory: None,
                metadata: "{}",
            },
            "2026-01-02T00:00:00Z",
            "2026-01-02T00:00:00Z",
        )
        .await
        .unwrap();

        let row = get_by_id(&db, "s1").await.unwrap().unwrap();
        assert_eq!(row.agent_id, None);
        assert_eq!(row.git_commits, None);
        assert_eq!(row.first_detected, "2026-01-02T00:00:00Z");
    }

    #[tokio::test]
    async fn update_activity_refreshes_and_reactivates() {
        let (_dir, db) = test_conn().await;
        register_new_session(
            &db,
            &NewSession {
                session_id: "s1",
                pid: 1,
                parent_pid: 2,
                working_directory: None,
                metadata: "{}",
            },
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();
        mark_inactive(&db, "s1", "2026-01-02T00:00:00Z")
            .await
            .unwrap();

        let updated = update_activity(&db, "s1", "2026-01-03T00:00:00Z", "{\"k\":1}")
            .await
            .unwrap();
        assert!(updated);

        let row = get_by_id(&db, "s1").await.unwrap().unwrap();
        assert_eq!(row.status.as_deref(), Some("active"));
        assert_eq!(row.last_activity, "2026-01-03T00:00:00Z");
        assert_eq!(row.metadata.as_deref(), Some("{\"k\":1}"));
    }

    #[tokio::test]
    async fn update_activity_on_an_unknown_id_is_a_clean_false() {
        let (_dir, db) = test_conn().await;
        assert!(!update_activity(&db, "ghost", "2026-01-01T00:00:00Z", "{}")
            .await
            .unwrap());
    }

    #[tokio::test]
    async fn mark_inactive_on_an_unknown_id_is_a_clean_false() {
        let (_dir, db) = test_conn().await;
        assert!(!mark_inactive(&db, "ghost", "2026-01-01T00:00:00Z")
            .await
            .unwrap());
    }

    #[tokio::test]
    async fn list_active_excludes_inactive_and_orders_by_activity_desc() {
        let (_dir, db) = test_conn().await;
        for (id, activity) in [
            ("older", "2026-01-01T00:00:00Z"),
            ("newer", "2026-01-03T00:00:00Z"),
        ] {
            register_new_session(
                &db,
                &NewSession {
                    session_id: id,
                    pid: 1,
                    parent_pid: 2,
                    working_directory: None,
                    metadata: "{}",
                },
                activity,
                "2026-01-01T00:00:00Z",
            )
            .await
            .unwrap();
        }
        register_new_session(
            &db,
            &NewSession {
                session_id: "gone",
                pid: 1,
                parent_pid: 2,
                working_directory: None,
                metadata: "{}",
            },
            "2026-01-02T00:00:00Z",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();
        mark_inactive(&db, "gone", "2026-01-02T00:00:00Z")
            .await
            .unwrap();

        let active = list_active(&db).await.unwrap();
        let ids: Vec<&str> = active.iter().map(|r| r.session_id.as_str()).collect();
        assert_eq!(ids, vec!["newer", "older"]);
    }

    /// BL-R4-2 (ported from `tests/test_sec_r4_purge_session_cascade.py::
    /// test_purge_deletes_claude_code_session_rows`): `agent_id` has no
    /// write path via `register_new_session` (matching Python's own
    /// INSERT), so this seeds it directly the same way the Python test's
    /// own fixture does -- proving the delete function works regardless
    /// of how a row's `agent_id` got populated.
    #[tokio::test]
    async fn delete_by_agent_id_removes_every_matching_row() {
        let (_dir, db) = test_conn().await;
        for id in ["s1", "s2"] {
            let am = ActiveModel {
                session_id: Set(id.to_string()),
                pid: Set(1),
                parent_pid: Set(2),
                first_detected: Set("2026-01-01T00:00:00Z".to_string()),
                last_activity: Set("2026-01-01T00:00:00Z".to_string()),
                working_directory: Set(None),
                agent_id: Set(Some("alice".to_string())),
                status: Set(Some("detected".to_string())),
                git_commits: Set(None),
                metadata: Set(None),
            };
            Entity::insert(am).exec(&db).await.unwrap();
        }

        let deleted = delete_by_agent_id(&db, "alice").await.unwrap();

        assert_eq!(deleted, 2);
        assert!(get_by_id(&db, "s1").await.unwrap().is_none());
        assert!(get_by_id(&db, "s2").await.unwrap().is_none());
    }

    #[tokio::test]
    async fn delete_by_agent_id_leaves_a_bystanders_session_intact() {
        let (_dir, db) = test_conn().await;
        for (id, agent) in [("s1", "alice"), ("s2", "bob")] {
            let am = ActiveModel {
                session_id: Set(id.to_string()),
                pid: Set(1),
                parent_pid: Set(2),
                first_detected: Set("2026-01-01T00:00:00Z".to_string()),
                last_activity: Set("2026-01-01T00:00:00Z".to_string()),
                working_directory: Set(None),
                agent_id: Set(Some(agent.to_string())),
                status: Set(Some("detected".to_string())),
                git_commits: Set(None),
                metadata: Set(None),
            };
            Entity::insert(am).exec(&db).await.unwrap();
        }

        assert_eq!(delete_by_agent_id(&db, "alice").await.unwrap(), 1);

        assert!(get_by_id(&db, "s1").await.unwrap().is_none());
        assert!(get_by_id(&db, "s2").await.unwrap().is_some());
    }

    #[tokio::test]
    async fn delete_by_agent_id_on_no_matching_rows_is_a_clean_zero() {
        let (_dir, db) = test_conn().await;
        assert_eq!(delete_by_agent_id(&db, "ghost").await.unwrap(), 0);
    }
}
