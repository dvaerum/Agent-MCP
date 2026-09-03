//! Port of `agent_mcp/repositories/pending_directive_repository.py`.
//!
//! `pending_directive` is the one-shot, human-triggered "poke" queue:
//! an operator pushes a single ad-hoc directive to one agent
//! out-of-band. It is NOT recurring — no `next_due_at`/interval —
//! just one row, delivered once (stamped `delivered_at`) or sitting
//! undelivered until the next check-in. Its sibling
//! `scheduled_directive_repository` (not yet ported) is the same
//! delivery concept at a different lifecycle stage: a recurring,
//! self-scheduling directive. Both converge on the identical
//! `directive` event JSON shape ([`DirectiveEvent`]), distinguished
//! only by `data.source` (`"poke"` vs `"schedule"`) and
//! `data.schedule_id` (`None` vs the schedule's id) — per ADR-0026,
//! both feed the same unified delivery-scheduler push mechanism.
//!
//! A module of plain functions, matching Python's own design (no
//! cache). Every function takes the `&Connection` it should run
//! against — this crate has no separate "opens its own connection"
//! path, matching every other repository here. Unlike
//! `group_capability_repository`, this table lives on the per-project
//! AGENT database (confirmed via the ORM model,
//! `agent_mcp/db/models/pending_directive.py`), not the router DB —
//! don't assume from a sibling repository's placement.
//!
//! `agent_id` has NO database-level foreign key to `agents.agent_id`
//! — Python's model/migration comments call it a "logical FK" only,
//! so this port doesn't assume referential-integrity enforcement at
//! the DB layer either.

use rusqlite::{Connection, Result};

/// One row of the `pending_directive` table.
#[derive(Debug, Clone, PartialEq)]
pub struct PendingDirectiveRow {
    pub poke_id: String,
    pub agent_id: String,
    pub prompt: String,
    pub priority: String,
    pub created_at: String,
    pub created_by: Option<String>,
    pub delivered_at: Option<String>,
}

/// The wire shape both poke and scheduled directives converge on.
/// `event_type` serializes as `"type"` to match the JSON key Python's
/// `_poke_event`/`_schedule_event` produce.
#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct DirectiveEvent {
    #[serde(rename = "type")]
    pub event_type: String,
    pub ref_id: String,
    pub timestamp: String,
    pub priority: String,
    pub data: DirectiveEventData,
}

#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct DirectiveEventData {
    pub prompt: String,
    pub source: String,
    pub schedule_id: Option<String>,
}

/// Ported from Python's `_poke_event`: `priority or "urgent"` — a
/// defensive fallback for a stored empty-string priority (the DB
/// column is `NOT NULL DEFAULT 'urgent'`, but this guards a row that
/// somehow got an empty string written directly, bypassing
/// `create_poke`'s own default).
fn poke_event(poke_id: &str, prompt: &str, priority: &str, timestamp: &str) -> DirectiveEvent {
    let priority = if priority.is_empty() {
        "urgent"
    } else {
        priority
    };
    DirectiveEvent {
        event_type: "directive".to_string(),
        ref_id: poke_id.to_string(),
        timestamp: timestamp.to_string(),
        priority: priority.to_string(),
        data: DirectiveEventData {
            prompt: prompt.to_string(),
            source: "poke".to_string(),
            schedule_id: None,
        },
    }
}

/// INSERT an undelivered poke row. Returns the row as constructed
/// from the INSERT's own parameters — matching Python exactly, this
/// does NOT re-`SELECT` afterward. A duplicate `poke_id` surfaces as
/// a real `rusqlite::Error` (PK violation), not a special variant —
/// matching Python, which lets the underlying `sqlite3.IntegrityError`
/// propagate uncaught.
#[allow(clippy::too_many_arguments)]
pub fn create_poke(
    conn: &Connection,
    poke_id: &str,
    agent_id: &str,
    prompt: &str,
    priority: Option<&str>,
    created_by: Option<&str>,
    now_iso: &str,
) -> Result<PendingDirectiveRow> {
    let priority = priority.unwrap_or("urgent");
    conn.execute(
        "INSERT INTO pending_directive (poke_id, agent_id, prompt, priority, created_at, created_by, delivered_at) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, NULL)",
        (poke_id, agent_id, prompt, priority, now_iso, created_by),
    )?;
    Ok(PendingDirectiveRow {
        poke_id: poke_id.to_string(),
        agent_id: agent_id.to_string(),
        prompt: prompt.to_string(),
        priority: priority.to_string(),
        created_at: now_iso.to_string(),
        created_by: created_by.map(String::from),
        delivered_at: None,
    })
}

/// Collect + mark-delivered every undelivered poke for `agent_id`.
/// Each row's `delivered_at` is stamped inside this same pass, so a
/// poke can never be double-delivered even if the caller re-invokes
/// this before committing (the caller owns the transaction/commit,
/// matching Python). Returns events in `created_at ASC` order —
/// "urgent sorts to the front" is a caller-side concern (Python's
/// `_sort_events_priority_then_time`), NOT this repository's job;
/// don't conflate SQL ordering with delivery-order guarantees.
/// Empty (never an error) when nothing is undelivered.
pub fn collect_undelivered(
    conn: &Connection,
    agent_id: &str,
    now_iso: &str,
) -> Result<Vec<DirectiveEvent>> {
    let mut stmt = conn.prepare(
        "SELECT poke_id, prompt, priority FROM pending_directive \
         WHERE agent_id = ?1 AND delivered_at IS NULL ORDER BY created_at ASC",
    )?;
    let rows: Vec<(String, String, String)> = stmt
        .query_map([agent_id], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?))
        })?
        .collect::<Result<Vec<_>>>()?;
    drop(stmt);

    let mut events = Vec::with_capacity(rows.len());
    for (poke_id, prompt, priority) in rows {
        conn.execute(
            "UPDATE pending_directive SET delivered_at = ?1 WHERE poke_id = ?2",
            (now_iso, &poke_id),
        )?;
        events.push(poke_event(&poke_id, &prompt, &priority, now_iso));
    }
    Ok(events)
}

/// Count of undelivered pokes for `agent_id`. `0`, never an error,
/// when there are none (a `COUNT(*)` query always returns exactly one
/// row).
pub fn count_undelivered(conn: &Connection, agent_id: &str) -> Result<i64> {
    conn.query_row(
        "SELECT COUNT(*) FROM pending_directive WHERE agent_id = ?1 AND delivered_at IS NULL",
        [agent_id],
        |row| row.get(0),
    )
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
    fn create_poke_returns_the_row_it_just_inserted() {
        let conn = test_conn();
        let row = create_poke(
            &conn,
            "poke-1",
            "alice",
            "check in",
            None,
            Some("admin"),
            "2026-01-01T00:00:00Z",
        )
        .unwrap();

        assert_eq!(row.poke_id, "poke-1");
        assert_eq!(row.agent_id, "alice");
        assert_eq!(row.prompt, "check in");
        assert_eq!(
            row.priority, "urgent",
            "default priority when None is passed"
        );
        assert_eq!(row.created_by.as_deref(), Some("admin"));
        assert_eq!(row.delivered_at, None);
    }

    #[test]
    fn create_poke_duplicate_poke_id_is_a_real_error() {
        let conn = test_conn();
        create_poke(
            &conn,
            "poke-1",
            "alice",
            "first",
            None,
            None,
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        let err = create_poke(
            &conn,
            "poke-1",
            "alice",
            "second",
            None,
            None,
            "2026-01-01T00:00:01Z",
        );
        assert!(err.is_err());
    }

    #[test]
    fn count_undelivered_reflects_only_undelivered_rows() {
        let conn = test_conn();
        assert_eq!(count_undelivered(&conn, "alice").unwrap(), 0);
        create_poke(
            &conn,
            "poke-1",
            "alice",
            "p1",
            None,
            None,
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        create_poke(
            &conn,
            "poke-2",
            "alice",
            "p2",
            None,
            None,
            "2026-01-01T00:00:01Z",
        )
        .unwrap();
        assert_eq!(count_undelivered(&conn, "alice").unwrap(), 2);
    }

    #[test]
    fn count_undelivered_is_scoped_per_agent() {
        let conn = test_conn();
        create_poke(
            &conn,
            "poke-1",
            "alice",
            "p1",
            None,
            None,
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        create_poke(
            &conn,
            "poke-2",
            "bob",
            "p2",
            None,
            None,
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        assert_eq!(count_undelivered(&conn, "alice").unwrap(), 1);
        assert_eq!(count_undelivered(&conn, "bob").unwrap(), 1);
    }

    #[test]
    fn collect_undelivered_marks_delivered_exactly_once() {
        let conn = test_conn();
        create_poke(
            &conn,
            "poke-1",
            "alice",
            "check in",
            Some("high"),
            None,
            "2026-01-01T00:00:00Z",
        )
        .unwrap();

        let events = collect_undelivered(&conn, "alice", "2026-01-01T00:00:05Z").unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].event_type, "directive");
        assert_eq!(events[0].ref_id, "poke-1");
        assert_eq!(events[0].priority, "high");
        assert_eq!(events[0].data.prompt, "check in");
        assert_eq!(events[0].data.source, "poke");
        assert_eq!(events[0].data.schedule_id, None);
        assert_eq!(count_undelivered(&conn, "alice").unwrap(), 0);

        // Second call: nothing left to collect -- delivered exactly once.
        let second = collect_undelivered(&conn, "alice", "2026-01-01T00:00:06Z").unwrap();
        assert_eq!(second, Vec::new());
    }

    #[test]
    fn collect_undelivered_orders_by_created_at_ascending() {
        let conn = test_conn();
        create_poke(
            &conn,
            "poke-2",
            "alice",
            "second",
            None,
            None,
            "2026-01-01T00:00:02Z",
        )
        .unwrap();
        create_poke(
            &conn,
            "poke-1",
            "alice",
            "first",
            None,
            None,
            "2026-01-01T00:00:01Z",
        )
        .unwrap();

        let events = collect_undelivered(&conn, "alice", "2026-01-01T00:00:05Z").unwrap();
        let ids: Vec<&str> = events.iter().map(|e| e.ref_id.as_str()).collect();
        assert_eq!(ids, vec!["poke-1", "poke-2"]);
    }

    #[test]
    fn collect_undelivered_is_scoped_per_agent() {
        let conn = test_conn();
        create_poke(
            &conn,
            "poke-1",
            "alice",
            "for alice",
            None,
            None,
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        create_poke(
            &conn,
            "poke-2",
            "bob",
            "for bob",
            None,
            None,
            "2026-01-01T00:00:00Z",
        )
        .unwrap();

        let events = collect_undelivered(&conn, "alice", "2026-01-01T00:00:05Z").unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].ref_id, "poke-1");
        // bob's poke must still be sitting undelivered.
        assert_eq!(count_undelivered(&conn, "bob").unwrap(), 1);
    }

    #[test]
    fn collect_undelivered_empty_is_not_an_error() {
        let conn = test_conn();
        assert_eq!(
            collect_undelivered(&conn, "alice", "2026-01-01T00:00:00Z").unwrap(),
            Vec::new()
        );
    }
}
