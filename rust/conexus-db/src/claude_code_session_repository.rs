//! `claude_code_sessions` table -- port of the DB half of
//! `agent_mcp/features/claude_session_monitor.py`. Tracks Claude Code
//! sessions discovered via `.agent/registry.json` (the git-agentmcp
//! hook's own coordination file), independent of this crate's
//! `agents`/MCP-session concepts entirely -- a session here is a
//! detected Claude Code *process*, not an MCP bearer.

use rusqlite::{Connection, OptionalExtension, Result};

/// One `claude_code_sessions` row.
#[derive(Debug, Clone, PartialEq)]
pub struct ClaudeCodeSessionRow {
    pub session_id: String,
    pub pid: i64,
    pub parent_pid: i64,
    pub first_detected: String,
    pub last_activity: String,
    pub working_directory: Option<String>,
    pub agent_id: Option<String>,
    pub status: Option<String>,
    pub git_commits: Option<String>,
    pub metadata: Option<String>,
}

const COLUMNS: &str = "session_id, pid, parent_pid, first_detected, last_activity, \
     working_directory, agent_id, status, git_commits, metadata";

fn row_to_session(row: &rusqlite::Row) -> rusqlite::Result<ClaudeCodeSessionRow> {
    Ok(ClaudeCodeSessionRow {
        session_id: row.get(0)?,
        pid: row.get(1)?,
        parent_pid: row.get(2)?,
        first_detected: row.get(3)?,
        last_activity: row.get(4)?,
        working_directory: row.get(5)?,
        agent_id: row.get(6)?,
        status: row.get(7)?,
        git_commits: row.get(8)?,
        metadata: row.get(9)?,
    })
}

pub fn get_by_id(conn: &Connection, session_id: &str) -> Result<Option<ClaudeCodeSessionRow>> {
    conn.query_row(
        &format!("SELECT {COLUMNS} FROM claude_code_sessions WHERE session_id = ?1"),
        [session_id],
        row_to_session,
    )
    .optional()
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
pub fn register_new_session(
    conn: &Connection,
    session: &NewSession,
    last_activity: &str,
    now: &str,
) -> Result<()> {
    conn.execute(
        "INSERT OR REPLACE INTO claude_code_sessions \
         (session_id, pid, parent_pid, first_detected, last_activity, working_directory, \
          status, metadata) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, 'detected', ?7)",
        (
            session.session_id,
            session.pid,
            session.parent_pid,
            now,
            last_activity,
            session.working_directory,
            session.metadata,
        ),
    )?;
    Ok(())
}

/// Refresh an existing session's activity/metadata, flipping it back
/// to `'active'` (a session that drifted to some other status while
/// still present in the registry is corrected here, matching Python's
/// unconditional `SET ... status = 'active'`).
pub fn update_activity(
    conn: &Connection,
    session_id: &str,
    last_activity: &str,
    metadata: &str,
) -> Result<bool> {
    let changed = conn.execute(
        "UPDATE claude_code_sessions SET last_activity = ?1, metadata = ?2, status = 'active' \
         WHERE session_id = ?3",
        (last_activity, metadata, session_id),
    )?;
    Ok(changed > 0)
}

/// A session that dropped out of the registry (process exited, or the
/// hook stopped reporting it) -- marks it `'inactive'` without
/// deleting the row (history preserved, matching Python).
pub fn mark_inactive(conn: &Connection, session_id: &str, now: &str) -> Result<bool> {
    let changed = conn.execute(
        "UPDATE claude_code_sessions SET status = 'inactive', last_activity = ?1 \
         WHERE session_id = ?2",
        (now, session_id),
    )?;
    Ok(changed > 0)
}

/// Every session currently `'detected'` or `'active'`, newest-activity
/// first.
pub fn list_active(conn: &Connection) -> Result<Vec<ClaudeCodeSessionRow>> {
    let mut stmt = conn.prepare(&format!(
        "SELECT {COLUMNS} FROM claude_code_sessions \
         WHERE status IN ('detected', 'active') ORDER BY last_activity DESC"
    ))?;
    let rows = stmt.query_map([], row_to_session)?;
    rows.collect()
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
    fn register_new_session_creates_a_detected_row() {
        let conn = test_conn();
        register_new_session(
            &conn,
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
        .unwrap();

        let row = get_by_id(&conn, "s1").unwrap().unwrap();
        assert_eq!(row.pid, 111);
        assert_eq!(row.parent_pid, 222);
        assert_eq!(row.status.as_deref(), Some("detected"));
        assert_eq!(row.working_directory.as_deref(), Some("/repo"));
    }

    #[test]
    fn register_new_session_is_insert_or_replace_on_a_re_detected_id() {
        let conn = test_conn();
        let seed = NewSession {
            session_id: "s1",
            pid: 111,
            parent_pid: 222,
            working_directory: Some("/repo"),
            metadata: "{}",
        };
        register_new_session(&conn, &seed, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z").unwrap();
        // Re-detected with a different pid (process restarted under
        // the same session_id) -- must overwrite, not conflict.
        let seed2 = NewSession { pid: 999, ..seed };
        register_new_session(
            &conn,
            &seed2,
            "2026-01-02T00:00:00Z",
            "2026-01-02T00:00:00Z",
        )
        .unwrap();

        let row = get_by_id(&conn, "s1").unwrap().unwrap();
        assert_eq!(row.pid, 999);
    }

    #[test]
    fn update_activity_refreshes_and_reactivates() {
        let conn = test_conn();
        register_new_session(
            &conn,
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
        .unwrap();
        mark_inactive(&conn, "s1", "2026-01-02T00:00:00Z").unwrap();

        let updated = update_activity(&conn, "s1", "2026-01-03T00:00:00Z", "{\"k\":1}").unwrap();
        assert!(updated);

        let row = get_by_id(&conn, "s1").unwrap().unwrap();
        assert_eq!(row.status.as_deref(), Some("active"));
        assert_eq!(row.last_activity, "2026-01-03T00:00:00Z");
        assert_eq!(row.metadata.as_deref(), Some("{\"k\":1}"));
    }

    #[test]
    fn update_activity_on_an_unknown_id_is_a_clean_false() {
        let conn = test_conn();
        assert!(!update_activity(&conn, "ghost", "2026-01-01T00:00:00Z", "{}").unwrap());
    }

    #[test]
    fn mark_inactive_on_an_unknown_id_is_a_clean_false() {
        let conn = test_conn();
        assert!(!mark_inactive(&conn, "ghost", "2026-01-01T00:00:00Z").unwrap());
    }

    #[test]
    fn list_active_excludes_inactive_and_orders_by_activity_desc() {
        let conn = test_conn();
        for (id, activity) in [
            ("older", "2026-01-01T00:00:00Z"),
            ("newer", "2026-01-03T00:00:00Z"),
        ] {
            register_new_session(
                &conn,
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
            .unwrap();
        }
        register_new_session(
            &conn,
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
        .unwrap();
        mark_inactive(&conn, "gone", "2026-01-02T00:00:00Z").unwrap();

        let active = list_active(&conn).unwrap();
        let ids: Vec<&str> = active.iter().map(|r| r.session_id.as_str()).collect();
        assert_eq!(ids, vec!["newer", "older"]);
    }
}
