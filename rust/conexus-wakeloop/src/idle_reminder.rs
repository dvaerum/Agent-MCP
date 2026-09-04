//! Idle backlog reminder for the `wait_for_events` wake loop. Port of
//! `agent_mcp/core/idle_reminder.py`.
//!
//! When an agent is sitting idle in the event loop but still has
//! unaddressed work -- unread messages and/or OPEN tasks assigned to it
//! (status not completed/cancelled/failed) -- periodically wake it with
//! a `reminder` event that lists exactly what's outstanding and tells it
//! to go handle it. No backlog -> no reminder, so a genuinely-idle agent
//! stays parked for free.
//!
//! Cadence is a per-agent timer (in-memory; a restart just restarts the
//! timer, which for an hour-scale interval is harmless). It's seeded on
//! first sight so a freshly-connected agent isn't reminded immediately,
//! and advanced every time the reminder check runs -- whether or not
//! there was a backlog to report -- so the check runs at most once per
//! interval.
//!
//! Same GIL-vs-real-threads note as [`crate::hold_ladder`]: Python backs
//! `_last_check` with an unlocked module-global dict, safe there only
//! because of CPython's single-threaded event loop; this port wraps it
//! in a real `Mutex`.
//!
//! ## R13-F4 body-content-leak note (re-derived, not ported at face
//! value)
//!
//! Python's `_subject_of` guards against `message_repo`'s DISPLAY
//! projection (`message_subject_view`) synthesizing a 50-char BODY
//! PREVIEW into a message's `subject` field when the stored subject is
//! NULL -- flagged via a `subject_is_placeholder` bool the reminder must
//! check before copying `subject` into this contractually body-free
//! event. `conexus_db::message_repository::MessageRow::subject` is the
//! RAW column value read directly off `agent_messages` (Phase B ported
//! the repository layer only -- the display-projection step lives in
//! Python's serializer, one layer up, and has no Rust port yet). Since
//! the raw column is NEVER a synthesized body preview, there is no
//! placeholder case to guard against here at all -- using the raw
//! `subject` directly is not just simpler, it structurally cannot leak
//! body content the way a literal port of the placeholder check would
//! need to. A future Rust port of a message-serializing tool (e.g.
//! `get_agent_messages`) would need its own port of
//! `message_subject_view` for ITS OWN display purposes; this module
//! doesn't need one.

use std::collections::HashMap;
use std::sync::Mutex;

use conexus_db::task_repository::{self, TaskRow};
use conexus_db::{MessageQueryFilters, MessageRepository, MessageRow};
use rusqlite::Connection;
use serde_json::Value;

/// Task statuses that are still "open" -- a reminder nudges these.
/// Anything else (completed/cancelled/failed) is terminal and left
/// alone. Matches Python's `_TERMINAL_TASK_STATUSES` exactly, including
/// both English spellings of "cancelled".
const TERMINAL_TASK_STATUSES: &[&str] = &["completed", "cancelled", "canceled", "failed"];

/// Cap how many items the reminder itemizes (the true totals are still
/// reported); keeps a large backlog from ballooning the event.
const LIST_CAP: i64 = 15;

/// `agent_id -> monotonic timestamp (seconds) of the last reminder
/// check`. A real `Mutex`, not Python's unlocked dict -- see module doc.
static LAST_CHECK: Mutex<Option<HashMap<String, f64>>> = Mutex::new(None);

fn with_last_check<T>(f: impl FnOnce(&mut HashMap<String, f64>) -> T) -> T {
    let mut guard = LAST_CHECK.lock().unwrap();
    f(guard.get_or_insert_with(HashMap::new))
}

/// Seconds until this agent's next reminder check. Seeds to `now_mono`
/// on first sight (so a fresh connection waits a full interval, not
/// fires immediately). `now_mono` is an explicit monotonic-clock reading
/// in seconds (the caller's own `Instant`-derived value) -- this crate's
/// established "explicit input over hidden state" convention, not a
/// hidden internal clock read.
pub fn seconds_until_due(agent_id: &str, interval: f64, now_mono: f64) -> f64 {
    with_last_check(|last_check| match last_check.get(agent_id) {
        None => {
            last_check.insert(agent_id.to_string(), now_mono);
            interval
        }
        Some(&last) => (last + interval - now_mono).max(0.0),
    })
}

/// Advance the timer (called every time the interval elapses, whether or
/// not a reminder was actually sent).
pub fn mark_checked(agent_id: &str, now_mono: f64) {
    with_last_check(|last_check| {
        last_check.insert(agent_id.to_string(), now_mono);
    });
}

/// Drop all timers (test isolation helper).
pub fn clear() {
    with_last_check(|last_check| last_check.clear());
}

#[derive(Debug, Clone, PartialEq)]
pub struct BacklogTask {
    pub task_id: String,
    pub title: String,
    pub status: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct BacklogMessage {
    pub message_id: String,
    pub sender_id: String,
    pub subject: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Backlog {
    pub unread_count: i64,
    pub task_count: usize,
    pub unread_messages: Vec<BacklogMessage>,
    pub open_tasks: Vec<BacklogTask>,
}

/// A real, sender-set subject -- or a fixed body-free placeholder. See
/// the module doc's R13-F4 note for why this needs no placeholder-flag
/// check, unlike Python's `_subject_of`.
fn subject_of(row: &MessageRow) -> String {
    match row.subject.as_deref().map(str::trim) {
        Some(s) if !s.is_empty() => s.to_string(),
        _ => "(no subject)".to_string(),
    }
}

/// Return the agent's outstanding work, or `None` when there is none.
/// Fully defensive -- any DB error yields `None` (no reminder) rather
/// than breaking the wait loop, matching Python's own `except Exception`
/// wrapper here.
pub fn collect_backlog(conn: &Connection, agent_id: &str) -> Option<Backlog> {
    let unread_rows: Vec<MessageRow> = MessageRepository::new()
        .query(
            conn,
            &MessageQueryFilters {
                to: Some(agent_id),
                read: Some(false),
                limit: LIST_CAP,
                ..Default::default()
            },
            true, // oldest_first
        )
        .ok()?;
    let unread_count = conexus_db::message_repository::count_unread(conn, agent_id).ok()?;

    let open_tasks: Vec<TaskRow> = task_repository::list_by_agent(conn, agent_id, None, Some(200))
        .ok()?
        .into_iter()
        .filter(|t| !TERMINAL_TASK_STATUSES.contains(&t.status.to_lowercase().as_str()))
        .collect();

    if unread_count == 0 && open_tasks.is_empty() {
        return None;
    }

    Some(Backlog {
        unread_count,
        task_count: open_tasks.len(),
        unread_messages: unread_rows
            .iter()
            .map(|r| BacklogMessage {
                message_id: r.message_id.clone(),
                sender_id: r.sender_id.clone(),
                subject: subject_of(r),
            })
            .collect(),
        open_tasks: open_tasks
            .iter()
            .take(LIST_CAP as usize)
            .map(|t| BacklogTask {
                task_id: t.task_id.clone(),
                title: if t.title.is_empty() {
                    "(untitled)".to_string()
                } else {
                    t.title.clone()
                },
                status: t.status.clone(),
            })
            .collect(),
    })
}

fn format_message(backlog: &Backlog) -> String {
    let mut lines = vec![
        "⏰ Reminder — you have unaddressed work sitting in your queue. Please handle it now."
            .to_string(),
    ];

    if backlog.unread_count > 0 {
        lines.push(format!("\nUnread messages ({}):", backlog.unread_count));
        for m in &backlog.unread_messages {
            lines.push(format!("  • from {}: {}", m.sender_id, m.subject));
        }
        let shown = backlog.unread_messages.len() as i64;
        if backlog.unread_count > shown {
            lines.push(format!("  … and {} more", backlog.unread_count - shown));
        }
    }

    if backlog.task_count > 0 {
        lines.push(format!("\nOpen tasks ({}):", backlog.task_count));
        for t in &backlog.open_tasks {
            lines.push(format!("  • [{}] {} ({})", t.status, t.title, t.task_id));
        }
        let shown = backlog.open_tasks.len();
        if backlog.task_count > shown {
            lines.push(format!("  … and {} more", backlog.task_count - shown));
        }
    }

    lines.push(
        "\nGo address these now: call get_agent_messages to read the messages, and \
         view_tasks / update_task_status to progress the tasks."
            .to_string(),
    );
    lines.join("\n")
}

/// Build the `reminder` event (same envelope shape as every other
/// `wait_for_events` event) carrying the count AND the itemized list.
/// `now` is an explicit ISO-8601 timestamp -- see [`crate::hold_ladder::advisory_event`]'s
/// doc for why this crate takes it explicitly rather than reading the
/// wall clock internally.
pub fn reminder_event(backlog: &Backlog, now: &str) -> Value {
    serde_json::json!({
        "type": "reminder",
        "ref_id": null,
        "timestamp": now,
        "payload": {
            "message": format_message(backlog),
            "unread_count": backlog.unread_count,
            "task_count": backlog.task_count,
            "unread_messages": backlog.unread_messages.iter().map(|m| serde_json::json!({
                "message_id": m.message_id,
                "sender_id": m.sender_id,
                "subject": m.subject,
            })).collect::<Vec<_>>(),
            "open_tasks": backlog.open_tasks.iter().map(|t| serde_json::json!({
                "task_id": t.task_id,
                "title": t.title,
                "status": t.status,
            })).collect::<Vec<_>>(),
        },
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_db::schema::init_schema;
    use std::sync::Mutex as StdMutex;

    // `LAST_CHECK` is process-wide static state; serialize the timer
    // tests against it (same discipline as `hold_ladder`'s `TEST_LOCK`).
    static TEST_LOCK: StdMutex<()> = StdMutex::new(());

    fn with_clean_state<T>(f: impl FnOnce() -> T) -> T {
        let _guard = TEST_LOCK.lock().unwrap();
        clear();
        let result = f();
        clear();
        result
    }

    fn test_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        conn
    }

    // ── timer ─────────────────────────────────────────────────────────

    #[test]
    fn seeds_on_first_sight_and_returns_the_full_interval() {
        with_clean_state(|| {
            assert_eq!(seconds_until_due("a", 60.0, 1000.0), 60.0);
        });
    }

    #[test]
    fn counts_down_after_seeding() {
        with_clean_state(|| {
            seconds_until_due("a", 60.0, 1000.0);
            assert_eq!(seconds_until_due("a", 60.0, 1030.0), 30.0);
        });
    }

    #[test]
    fn never_goes_negative_past_due() {
        with_clean_state(|| {
            seconds_until_due("a", 60.0, 1000.0);
            assert_eq!(seconds_until_due("a", 60.0, 5000.0), 0.0);
        });
    }

    #[test]
    fn mark_checked_advances_the_timer_even_without_a_reminder() {
        with_clean_state(|| {
            seconds_until_due("a", 60.0, 1000.0);
            mark_checked("a", 1060.0);
            assert_eq!(seconds_until_due("a", 60.0, 1060.0), 60.0);
        });
    }

    #[test]
    fn timers_are_per_agent() {
        with_clean_state(|| {
            seconds_until_due("a", 60.0, 1000.0);
            assert_eq!(seconds_until_due("b", 60.0, 1000.0), 60.0);
        });
    }

    // ── collect_backlog ──────────────────────────────────────────────

    fn insert_task(
        conn: &Connection,
        task_id: &str,
        title: &str,
        status: &str,
        assigned_to: &str,
        parent_task: Option<&str>,
    ) {
        // Only one root task (parent_task IS NULL) is allowed per DB
        // (idx_tasks_single_root) -- callers inserting more than one
        // task must give every task after the first a `parent_task`.
        conn.execute(
            "INSERT INTO tasks (task_id, title, description, status, priority, assigned_to, \
             created_by, parent_task, created_at, updated_at) \
             VALUES (?1, ?2, 'desc', ?3, 'medium', ?4, ?4, ?5, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            rusqlite::params![task_id, title, status, assigned_to, parent_task],
        )
        .unwrap();
    }

    fn insert_message(conn: &Connection, message_id: &str, from: &str, to: &str, read: bool) {
        conn.execute(
            "INSERT INTO agent_messages (message_id, sender_id, recipient_id, message_content, \
             message_type, priority, timestamp, delivered, read) \
             VALUES (?1, ?2, ?3, 'body', 'direct', 'normal', '2026-01-01T00:00:00Z', 1, ?4)",
            rusqlite::params![message_id, from, to, read],
        )
        .unwrap();
    }

    #[test]
    fn no_backlog_is_none() {
        let conn = test_conn();
        assert_eq!(collect_backlog(&conn, "a1"), None);
    }

    #[test]
    fn unread_messages_are_collected() {
        let conn = test_conn();
        insert_message(&conn, "m1", "op1", "a1", false);
        let backlog = collect_backlog(&conn, "a1").unwrap();
        assert_eq!(backlog.unread_count, 1);
        assert_eq!(backlog.unread_messages.len(), 1);
        assert_eq!(backlog.unread_messages[0].sender_id, "op1");
        assert_eq!(backlog.unread_messages[0].subject, "(no subject)");
    }

    #[test]
    fn read_messages_are_not_backlog() {
        let conn = test_conn();
        insert_message(&conn, "m1", "op1", "a1", true);
        assert_eq!(collect_backlog(&conn, "a1"), None);
    }

    #[test]
    fn open_tasks_are_collected_and_terminal_ones_excluded() {
        let conn = test_conn();
        insert_task(&conn, "t1", "Fix the bug", "in_progress", "a1", None);
        insert_task(&conn, "t2", "Done already", "completed", "a1", Some("t1"));
        insert_task(&conn, "t3", "Cancelled one", "cancelled", "a1", Some("t1"));
        let backlog = collect_backlog(&conn, "a1").unwrap();
        assert_eq!(backlog.task_count, 1);
        assert_eq!(backlog.open_tasks[0].task_id, "t1");
    }

    #[test]
    fn untitled_task_gets_a_placeholder_title() {
        let conn = test_conn();
        insert_task(&conn, "t1", "", "in_progress", "a1", None);
        let backlog = collect_backlog(&conn, "a1").unwrap();
        assert_eq!(backlog.open_tasks[0].title, "(untitled)");
    }

    #[test]
    fn reminder_event_shape() {
        let conn = test_conn();
        insert_message(&conn, "m1", "op1", "a1", false);
        let backlog = collect_backlog(&conn, "a1").unwrap();
        let ev = reminder_event(&backlog, "2026-01-01T00:00:00Z");
        assert_eq!(ev["type"], "reminder");
        assert_eq!(ev["timestamp"], "2026-01-01T00:00:00Z");
        assert_eq!(ev["payload"]["unread_count"], 1);
        assert!(ev["payload"]["message"]
            .as_str()
            .unwrap()
            .contains("Unread messages"));
    }
}
