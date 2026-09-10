//! Port of the `agent_actions` insert path in
//! `agent_mcp/db/actions/agent_actions_db.py::log_agent_action_to_db`.
//!
//! Scope note: only the legacy `agent_id=`-kwarg path is ported —
//! every current tool-layer call site this crate's callers need
//! (`conexus-tools`'s `project_settings_tools`) passes `agent_id`
//! directly, never `principal=`. Python's `principal=` kwarg (which
//! merges an attribution envelope into `details` and derives
//! `agent_id` from `principal.actor_label()`) has no Rust caller yet;
//! port it when a real call site needs it rather than speculatively
//! now. Likewise `_push_dashboard_data_changed` (a live-dashboard SSE
//! hint fired alongside the insert) has no Rust dashboard-push
//! mechanism to call yet — deferred to whichever phase wires the
//! `conexus` binary's own push path, same "defer to the phase that
//! owns the mechanism" call already made for `emit_context_write_wakes`
//! (see `conexus_tools::wake_notify`).
//!
//! Unlike Python's `log_agent_action_to_db` (which catches its own
//! `sqlite3.Error` and only logs it — audit failure must never break
//! the primary write), this repository function propagates `Err`
//! like every other repository in this crate; the caller decides
//! whether to swallow it. Keeping the "audit is best-effort" policy
//! at the tool-call-site (an explicit `if let Err(e) = ...`) rather
//! than hidden inside the repository matches this crate's convention
//! of never silently swallowing an error two layers away from the
//! decision that makes it safe to ignore.
//!
//! Phase G (sea-orm migration): converted onto the [`entity::agent_action`]
//! `Entity`, which already existed (defined ahead of this repository's
//! own rewrite). [`log_agent_action`] is a plain `ActiveModel::insert`
//! — a pure append against an autoincrement PK needs no
//! `.on_conflict()`. [`list_recent`]'s "last N, oldest-of-the-batch
//! first" shape doesn't fit sea-orm's query builder directly (it can't
//! express an inner `ORDER BY ... LIMIT` re-sorted by an outer
//! `ORDER BY` in one chain), so it uses this crate's established raw-
//! SQL escape hatch (`sea_orm::Statement` + `query_all_raw`, the same
//! idiom `task_comments_repository::task_status` and
//! `rag_repository`/`group_membership_repository` already use) rather
//! than bend the builder to fit.

use sea_orm::{
    ActiveModelTrait, ActiveValue::Set, ConnectionTrait, DatabaseConnection, DbErr, Statement,
};
use serde_json::Value;

use crate::entity::agent_action::ActiveModel;

/// One `agent_actions` row, as read back by [`list_recent`].
#[derive(Debug, Clone, PartialEq)]
pub struct AgentActionRow {
    pub action_id: i64,
    pub agent_id: String,
    pub action_type: String,
    pub task_id: Option<String>,
    pub timestamp: String,
    pub details: Option<Value>,
}

/// Insert one `agent_actions` row. `details`, when `Some`, is stored
/// as its JSON-serialized text (mirrors Python's `json.dumps`); `None`
/// stores a SQL NULL, matching an action with no extra detail.
/// `now` is an explicit ISO-8601 timestamp — this crate's "never read
/// a hidden wall clock" convention.
pub async fn log_agent_action(
    db: &DatabaseConnection,
    agent_id: &str,
    action_type: &str,
    task_id: Option<&str>,
    details: Option<&Value>,
    now: &str,
) -> Result<(), DbErr> {
    let details_json = details.map(|d| d.to_string());
    let am = ActiveModel {
        agent_id: Set(agent_id.to_string()),
        action_type: Set(action_type.to_string()),
        task_id: Set(task_id.map(str::to_string)),
        timestamp: Set(now.to_string()),
        details: Set(details_json),
        ..Default::default()
    };
    am.insert(db).await?;
    Ok(())
}

/// The `limit` most recent rows (optionally filtered by `agent_id`/
/// `action_type`), in ascending `action_id` order -- Python's
/// `view_audit_log`'s `filtered_log_entries[-limit:]` takes the last
/// `limit` entries of an append-ordered list WITHOUT reversing them,
/// so "most recent N, oldest-of-the-batch first" is the exact
/// behavior to preserve, not "newest first" (a plausible but wrong
/// re-derivation). Implemented as an inner DESC-ordered LIMIT
/// (cheapest way to pick "the last N") wrapped in an outer ASC
/// re-sort, via this crate's raw-SQL escape hatch (see this module's
/// own doc comment for why sea-orm's query builder can't express this
/// shape directly).
pub async fn list_recent(
    db: &DatabaseConnection,
    agent_id_filter: Option<&str>,
    action_type_filter: Option<&str>,
    limit: i64,
) -> Result<Vec<AgentActionRow>, DbErr> {
    let mut clauses = Vec::new();
    let mut params: Vec<sea_orm::Value> = Vec::new();
    if let Some(agent_id) = agent_id_filter {
        clauses.push("agent_id = ?");
        params.push(agent_id.into());
    }
    if let Some(action_type) = action_type_filter {
        clauses.push("action_type = ?");
        params.push(action_type.into());
    }
    let where_clause = if clauses.is_empty() {
        String::new()
    } else {
        format!("WHERE {}", clauses.join(" AND "))
    };
    params.push(limit.into());
    let sql = format!(
        "SELECT action_id, agent_id, action_type, task_id, timestamp, details FROM ( \
             SELECT action_id, agent_id, action_type, task_id, timestamp, details \
             FROM agent_actions {where_clause} ORDER BY action_id DESC LIMIT ? \
         ) ORDER BY action_id ASC"
    );
    let stmt = Statement::from_sql_and_values(sea_orm::DatabaseBackend::Sqlite, &sql, params);
    let rows = db.query_all_raw(stmt).await?;
    rows.into_iter()
        .map(|row| {
            let details_raw: Option<String> = row.try_get("", "details")?;
            Ok(AgentActionRow {
                action_id: row.try_get("", "action_id")?,
                agent_id: row.try_get("", "agent_id")?,
                action_type: row.try_get("", "action_type")?,
                task_id: row.try_get("", "task_id")?,
                timestamp: row.try_get("", "timestamp")?,
                details: details_raw.and_then(|s| serde_json::from_str(&s).ok()),
            })
        })
        .collect()
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
    async fn logs_a_row_with_no_details() {
        let (_dir, db) = test_conn().await;
        log_agent_action(
            &db,
            "alice",
            "updated_setting",
            None,
            None,
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();

        let rows = list_recent(&db, None, None, 50).await.unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].agent_id, "alice");
        assert_eq!(rows[0].action_type, "updated_setting");
        assert_eq!(rows[0].task_id, None);
        assert_eq!(rows[0].details, None);
    }

    #[tokio::test]
    async fn logs_a_row_with_json_serialized_details() {
        let (_dir, db) = test_conn().await;
        let details = serde_json::json!({"context_key": "config_x", "created": true});
        log_agent_action(
            &db,
            "alice",
            "updated_setting",
            None,
            Some(&details),
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();

        let rows = list_recent(&db, None, None, 50).await.unwrap();
        assert_eq!(rows[0].details, Some(details));
    }

    #[tokio::test]
    async fn multiple_actions_accumulate_distinct_autoincrement_ids() {
        let (_dir, db) = test_conn().await;
        for i in 0..3 {
            log_agent_action(
                &db,
                "alice",
                "updated_setting",
                None,
                None,
                &format!("2026-01-01T00:00:0{i}Z"),
            )
            .await
            .unwrap();
        }
        let rows = list_recent(&db, None, None, 50).await.unwrap();
        assert_eq!(rows.len(), 3);
    }

    async fn seed(db: &DatabaseConnection, agent_id: &str, action_type: &str, ts: &str) {
        log_agent_action(db, agent_id, action_type, None, None, ts)
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn list_recent_returns_the_last_n_in_ascending_order_not_reversed() {
        // Matches Python's `filtered_log_entries[-limit:]` -- the last
        // N entries, still oldest-of-the-batch first (NOT re-sorted
        // newest-first, a plausible but wrong re-derivation).
        let (_dir, db) = test_conn().await;
        for i in 0..5 {
            seed(
                &db,
                "alice",
                "did_thing",
                &format!("2026-01-01T00:00:0{i}Z"),
            )
            .await;
        }
        let rows = list_recent(&db, None, None, 3).await.unwrap();
        let timestamps: Vec<&str> = rows.iter().map(|r| r.timestamp.as_str()).collect();
        assert_eq!(
            timestamps,
            vec![
                "2026-01-01T00:00:02Z",
                "2026-01-01T00:00:03Z",
                "2026-01-01T00:00:04Z"
            ]
        );
    }

    #[tokio::test]
    async fn list_recent_filters_by_agent_id() {
        let (_dir, db) = test_conn().await;
        seed(&db, "alice", "did_thing", "2026-01-01T00:00:00Z").await;
        seed(&db, "bob", "did_thing", "2026-01-01T00:00:01Z").await;
        let rows = list_recent(&db, Some("bob"), None, 50).await.unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].agent_id, "bob");
    }

    #[tokio::test]
    async fn list_recent_filters_by_action_type() {
        let (_dir, db) = test_conn().await;
        seed(&db, "alice", "created_task", "2026-01-01T00:00:00Z").await;
        seed(&db, "alice", "deleted_task", "2026-01-01T00:00:01Z").await;
        let rows = list_recent(&db, None, Some("deleted_task"), 50)
            .await
            .unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].action_type, "deleted_task");
    }

    #[tokio::test]
    async fn list_recent_combines_both_filters() {
        let (_dir, db) = test_conn().await;
        seed(&db, "alice", "created_task", "2026-01-01T00:00:00Z").await;
        seed(&db, "alice", "deleted_task", "2026-01-01T00:00:01Z").await;
        seed(&db, "bob", "deleted_task", "2026-01-01T00:00:02Z").await;
        let rows = list_recent(&db, Some("alice"), Some("deleted_task"), 50)
            .await
            .unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].agent_id, "alice");
        assert_eq!(rows[0].action_type, "deleted_task");
    }

    #[tokio::test]
    async fn list_recent_on_an_empty_table_is_empty() {
        let (_dir, db) = test_conn().await;
        assert_eq!(list_recent(&db, None, None, 50).await.unwrap(), vec![]);
    }

    #[tokio::test]
    async fn list_recent_parses_details_json() {
        let (_dir, db) = test_conn().await;
        let details = serde_json::json!({"key": "value"});
        log_agent_action(
            &db,
            "alice",
            "did_thing",
            None,
            Some(&details),
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();
        let rows = list_recent(&db, None, None, 50).await.unwrap();
        assert_eq!(rows[0].details, Some(details));
    }
}
