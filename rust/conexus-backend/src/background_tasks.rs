//! Per-project background maintenance loops (Phase F, prancy-napping-pie
//! -- 1 of the 4 background loops the operator approved porting,
//! 2026-09-07). Every loop here runs for the lifetime of the process;
//! `conexus-backend` has no in-process graceful-shutdown coordination
//! (unlike Python's `g.server_running` flag, needed there because one
//! Python process serves several concerns cooperatively) -- this
//! binary serves exactly one project and exits whole when the OS
//! kills it, so a loop with no explicit stop condition is the correct,
//! simplest port, not a corner cut.
//!
//! Also unlike Python's own `g.startup_complete_event` gate (deferring
//! the first cycle until `MCP_PROJECT_DIR` is set, so the DB engine
//! cache doesn't bind to the wrong path): `main()`'s boot sequence
//! already opens and initializes the DB connection synchronously,
//! fully, BEFORE `SharedState`/these loops are ever constructed --
//! there is no equivalent race to guard against, so no startup gate is
//! needed here either.

use std::sync::Arc;
use std::time::Duration;

use rusqlite::Connection;

use crate::server::SharedState;

/// Port of `agent_mcp/features/message_retention.py`. The
/// `agent_messages` table grows unbounded (rows are only ever flipped
/// to `read=1`, never deleted) -- this prunes read rows older than a
/// per-project `config_message_retention_days` knob (absent/0 =
/// unbounded, upstream behavior unchanged).
mod message_retention {
    use super::*;

    /// A misconfigured `config_message_retention_days` (e.g. an
    /// operator typo like `10**18`) would overflow `chrono::Duration`'s
    /// internal bound if fed through unclamped -- verified directly
    /// this same session (Phase F test_sec_r16 port) that
    /// `chrono::Duration::seconds` panics well before 1e15 seconds;
    /// clamping in DAYS here keeps every downstream computation the
    /// same order of magnitude Python's own `MAX_RETENTION_DAYS` clamp
    /// does, for the identical reason.
    const MAX_RETENTION_DAYS: i64 = 3650;

    pub const DEFAULT_INTERVAL: Duration = Duration::from_secs(24 * 60 * 60);

    fn read_retention_days(conn: &Connection) -> i64 {
        let days = conexus_db::project_settings_repository::get_int(
            conn,
            "config_message_retention_days",
            0,
        );
        if days <= 0 {
            return 0;
        }
        days.min(MAX_RETENTION_DAYS)
    }

    /// Deletes read messages older than the configured retention
    /// window. Returns the number of rows deleted; a no-op (`Ok(0)`,
    /// never touching the table) when retention is disabled -- same
    /// contract as Python's `prune_old_messages()`.
    pub fn prune_old_messages(
        conn: &Connection,
        now: chrono::DateTime<chrono::Utc>,
    ) -> rusqlite::Result<i64> {
        let days = read_retention_days(conn);
        if days <= 0 {
            return Ok(0);
        }
        let cutoff = (now - chrono::Duration::days(days)).to_rfc3339();
        conexus_db::message_repository::prune_read_before(conn, &cutoff)
    }

    pub async fn run_periodically(shared: Arc<SharedState>, interval: Duration) {
        loop {
            let result = {
                let conn = shared.conn.lock().await;
                prune_old_messages(&conn, chrono::Utc::now())
            };
            match result {
                Ok(0) => {}
                Ok(deleted) => {
                    eprintln!(
                        "conexus-backend: message retention deleted {deleted} read message(s)"
                    );
                }
                Err(e) => {
                    eprintln!("conexus-backend: message retention cycle failed: {e}");
                }
            }
            tokio::time::sleep(interval).await;
        }
    }
}

/// Spawns every approved background maintenance loop. Called once from
/// `main()` after `SharedState` is constructed; each loop gets its own
/// detached task (never joined -- see this module's own doc on why no
/// shutdown coordination is needed).
pub fn spawn_all(shared: &Arc<SharedState>) {
    tokio::spawn(message_retention::run_periodically(
        shared.clone(),
        message_retention::DEFAULT_INTERVAL,
    ));
}

#[cfg(test)]
mod tests {
    use super::message_retention::prune_old_messages;
    use conexus_db::message_repository::{self, NewMessage};
    use conexus_db::schema::init_schema;
    use rusqlite::Connection;

    fn test_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        conn
    }

    fn set_retention_days(conn: &Connection, days: i64) {
        conexus_db::project_settings_repository::upsert(
            conn,
            "config_message_retention_days",
            &days.to_string(),
            None,
            false,
            "test",
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
    }

    fn seed_message(conn: &Connection, id: &str, sent_at: &str, read: bool) {
        // "admin" is the one recipient_exists() accepts unconditionally
        // (message_repository.rs), so tests don't need to seed a real
        // agents row just to send a message.
        message_repository::send(
            conn,
            NewMessage {
                message_id: id,
                sender_id: "alice",
                recipient_id: "admin",
                message_content: "hi",
                message_type: "direct",
                priority: "normal",
                timestamp: sent_at,
                delivered: true,
                read,
                subject: None,
                parent_message_id: None,
            },
        )
        .unwrap();
    }

    #[test]
    fn disabled_by_default_prunes_nothing() {
        let conn = test_conn();
        seed_message(&conn, "m1", "2020-01-01T00:00:00Z", true);
        let deleted = prune_old_messages(&conn, chrono::Utc::now()).unwrap();
        assert_eq!(deleted, 0);
        assert!(message_repository::get_by_id(&conn, "m1")
            .unwrap()
            .is_some());
    }

    #[test]
    fn prunes_only_read_messages_past_the_configured_window() {
        let conn = test_conn();
        set_retention_days(&conn, 30);
        let now: chrono::DateTime<chrono::Utc> = "2026-06-01T00:00:00Z".parse().unwrap();
        seed_message(&conn, "old-read", "2026-01-01T00:00:00Z", true);
        seed_message(&conn, "old-unread", "2026-01-01T00:00:00Z", false);
        seed_message(&conn, "recent-read", "2026-05-30T00:00:00Z", true);

        let deleted = prune_old_messages(&conn, now).unwrap();

        assert_eq!(deleted, 1);
        assert!(message_repository::get_by_id(&conn, "old-read")
            .unwrap()
            .is_none());
        assert!(message_repository::get_by_id(&conn, "old-unread")
            .unwrap()
            .is_some());
        assert!(message_repository::get_by_id(&conn, "recent-read")
            .unwrap()
            .is_some());
    }

    #[test]
    fn a_huge_retention_value_is_clamped_not_left_to_overflow() {
        // Verified this session (Phase F test_sec_r16 port) that
        // chrono::Duration panics well before 1e15 seconds -- this
        // proves the clamp actually engages for an operator typo
        // rather than assuming MAX_RETENTION_DAYS is merely decorative.
        // The seeded message must be older than the CLAMPED window
        // (MAX_RETENTION_DAYS = 3650 days, ~10y) or clamping correctly
        // means it's still within the retained window and never
        // pruned -- an explicit fixed `now` keeps this independent of
        // the wall clock, unlike an earlier draft that used
        // `chrono::Utc::now()` and silently stopped pruning once real
        // time passed 10 years past the seeded date.
        let conn = test_conn();
        set_retention_days(&conn, 999_999_999_999);
        let now: chrono::DateTime<chrono::Utc> = "2026-01-01T00:00:00Z".parse().unwrap();
        seed_message(&conn, "m1", "2000-01-01T00:00:00Z", true);
        // Must not panic.
        let deleted = prune_old_messages(&conn, now).unwrap();
        assert_eq!(deleted, 1);
    }
}
