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

use rusqlite::{Connection, Result};
use serde_json::Value;

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
}
