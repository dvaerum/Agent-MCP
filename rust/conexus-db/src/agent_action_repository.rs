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

use rusqlite::Result;
use rusqlite::{params_from_iter, Connection};
use serde_json::Value;

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
pub fn log_agent_action(
    conn: &Connection,
    agent_id: &str,
    action_type: &str,
    task_id: Option<&str>,
    details: Option<&Value>,
    now: &str,
) -> Result<()> {
    let details_json = details.map(|d| d.to_string());
    conn.execute(
        "INSERT INTO agent_actions (agent_id, action_type, task_id, timestamp, details) \
         VALUES (?1, ?2, ?3, ?4, ?5)",
        (agent_id, action_type, task_id, now, details_json),
    )?;
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
/// re-sort.
pub fn list_recent(
    conn: &Connection,
    agent_id_filter: Option<&str>,
    action_type_filter: Option<&str>,
    limit: i64,
) -> Result<Vec<AgentActionRow>> {
    let mut clauses = Vec::new();
    let mut params: Vec<&dyn rusqlite::ToSql> = Vec::new();
    if let Some(agent_id) = &agent_id_filter {
        clauses.push("agent_id = ?");
        params.push(agent_id);
    }
    if let Some(action_type) = &action_type_filter {
        clauses.push("action_type = ?");
        params.push(action_type);
    }
    let where_clause = if clauses.is_empty() {
        String::new()
    } else {
        format!("WHERE {}", clauses.join(" AND "))
    };
    params.push(&limit);
    let sql = format!(
        "SELECT action_id, agent_id, action_type, task_id, timestamp, details FROM ( \
             SELECT action_id, agent_id, action_type, task_id, timestamp, details \
             FROM agent_actions {where_clause} ORDER BY action_id DESC LIMIT ? \
         ) ORDER BY action_id ASC"
    );
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt
        .query_map(params_from_iter(params), |row| {
            let details_raw: Option<String> = row.get(5)?;
            Ok(AgentActionRow {
                action_id: row.get(0)?,
                agent_id: row.get(1)?,
                action_type: row.get(2)?,
                task_id: row.get(3)?,
                timestamp: row.get(4)?,
                details: details_raw.and_then(|s| serde_json::from_str(&s).ok()),
            })
        })?
        .collect::<Result<Vec<_>>>()?;
    Ok(rows)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::init_schema;

    fn test_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        conn
    }

    #[test]
    fn logs_a_row_with_no_details() {
        let conn = test_conn();
        log_agent_action(
            &conn,
            "alice",
            "updated_setting",
            None,
            None,
            "2026-01-01T00:00:00Z",
        )
        .unwrap();

        let (agent_id, action_type, task_id, details): (
            String,
            String,
            Option<String>,
            Option<String>,
        ) = conn
            .query_row(
                "SELECT agent_id, action_type, task_id, details FROM agent_actions",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
            )
            .unwrap();
        assert_eq!(agent_id, "alice");
        assert_eq!(action_type, "updated_setting");
        assert_eq!(task_id, None);
        assert_eq!(details, None);
    }

    #[test]
    fn logs_a_row_with_json_serialized_details() {
        let conn = test_conn();
        let details = serde_json::json!({"context_key": "config_x", "created": true});
        log_agent_action(
            &conn,
            "alice",
            "updated_setting",
            None,
            Some(&details),
            "2026-01-01T00:00:00Z",
        )
        .unwrap();

        let stored: String = conn
            .query_row("SELECT details FROM agent_actions", [], |r| r.get(0))
            .unwrap();
        assert_eq!(serde_json::from_str::<Value>(&stored).unwrap(), details);
    }

    #[test]
    fn multiple_actions_accumulate_distinct_autoincrement_ids() {
        let conn = test_conn();
        for i in 0..3 {
            log_agent_action(
                &conn,
                "alice",
                "updated_setting",
                None,
                None,
                &format!("2026-01-01T00:00:0{i}Z"),
            )
            .unwrap();
        }
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM agent_actions", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 3);
    }

    fn seed(conn: &Connection, agent_id: &str, action_type: &str, ts: &str) {
        log_agent_action(conn, agent_id, action_type, None, None, ts).unwrap();
    }

    #[test]
    fn list_recent_returns_the_last_n_in_ascending_order_not_reversed() {
        // Matches Python's `filtered_log_entries[-limit:]` -- the last
        // N entries, still oldest-of-the-batch first (NOT re-sorted
        // newest-first, a plausible but wrong re-derivation).
        let conn = test_conn();
        for i in 0..5 {
            seed(
                &conn,
                "alice",
                "did_thing",
                &format!("2026-01-01T00:00:0{i}Z"),
            );
        }
        let rows = list_recent(&conn, None, None, 3).unwrap();
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

    #[test]
    fn list_recent_filters_by_agent_id() {
        let conn = test_conn();
        seed(&conn, "alice", "did_thing", "2026-01-01T00:00:00Z");
        seed(&conn, "bob", "did_thing", "2026-01-01T00:00:01Z");
        let rows = list_recent(&conn, Some("bob"), None, 50).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].agent_id, "bob");
    }

    #[test]
    fn list_recent_filters_by_action_type() {
        let conn = test_conn();
        seed(&conn, "alice", "created_task", "2026-01-01T00:00:00Z");
        seed(&conn, "alice", "deleted_task", "2026-01-01T00:00:01Z");
        let rows = list_recent(&conn, None, Some("deleted_task"), 50).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].action_type, "deleted_task");
    }

    #[test]
    fn list_recent_combines_both_filters() {
        let conn = test_conn();
        seed(&conn, "alice", "created_task", "2026-01-01T00:00:00Z");
        seed(&conn, "alice", "deleted_task", "2026-01-01T00:00:01Z");
        seed(&conn, "bob", "deleted_task", "2026-01-01T00:00:02Z");
        let rows = list_recent(&conn, Some("alice"), Some("deleted_task"), 50).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].agent_id, "alice");
        assert_eq!(rows[0].action_type, "deleted_task");
    }

    #[test]
    fn list_recent_on_an_empty_table_is_empty() {
        let conn = test_conn();
        assert_eq!(list_recent(&conn, None, None, 50).unwrap(), vec![]);
    }

    #[test]
    fn list_recent_parses_details_json() {
        let conn = test_conn();
        let details = serde_json::json!({"key": "value"});
        log_agent_action(
            &conn,
            "alice",
            "did_thing",
            None,
            Some(&details),
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        let rows = list_recent(&conn, None, None, 50).unwrap();
        assert_eq!(rows[0].details, Some(details));
    }
}
