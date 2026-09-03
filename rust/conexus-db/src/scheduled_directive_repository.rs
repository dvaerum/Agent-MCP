//! Port of `agent_mcp/repositories/scheduled_directive_repository.py`.
//!
//! `scheduled_directive` is the recurring, self-scheduling sibling of
//! `pending_directive` (the one-shot "poke"): rows carry `next_due_at`
//! / `run_count` / `interval_seconds` and fire repeatedly on their own
//! cadence, with no new row created per fire. Both converge on the
//! same [`DirectiveEvent`](crate::pending_directive_repository::DirectiveEvent)
//! wire shape — `collect_due_and_fire` here sets `data.source =
//! "schedule"` and `data.schedule_id = Some(directive_id)`, where
//! `pending_directive_repository::collect_undelivered` sets
//! `data.source = "poke"` and `data.schedule_id = None`.
//!
//! Three invariants from the Python module's own docstring must
//! survive here as design contracts, not just incidental behavior:
//!
//! 1. **Interval-reset-from-delivery**: a fire always sets
//!    `next_due_at = <delivery time> + interval`, never a fixed
//!    wall-clock grid — a reconnecting agent overdue by many missed
//!    intervals fires exactly ONCE, not once per missed slot
//!    (`offline_across_many_intervals_fires_once` test below).
//! 2. **End-conditions are terminal but the row is KEPT**:
//!    `run_count >= max_runs`, or the next computed fire would land
//!    past `until_at`, flips the row to `status = "completed",
//!    enabled = 0` — it stays listable, never gets deleted.
//! 3. **A closed `until_at` window is reaped WITHOUT firing**: if
//!    `until_at` has already passed, the row is marked completed with
//!    no event emitted and `run_count` untouched — distinct from case
//!    2, where the LAST fire still emits an event on its way to
//!    terminal.
//!
//! Concurrency note ported from ADR-0026, NOT automatically true in
//! Rust: Python's version is safe to call from two independent
//! trigger paths (the wait-loop collector and the delivery-scheduler
//! tick) because CPython's single-threaded event loop never yields
//! mid-transaction between the SELECT and the UPDATEs here. A Rust
//! caller running this on a multi-threaded runtime or from genuinely
//! concurrent callers MUST NOT assume that for free — either run
//! `collect_due_and_fire` inside one exclusive DB transaction per
//! call (recommended: the whole SELECT+loop+UPDATEs as one unit), or
//! add an explicit claim (`UPDATE ... RETURNING`-style atomic claim)
//! before this crate is composed into `conexus-mcp`'s actor model in
//! a later phase. This module itself does not open a transaction —
//! that stays the caller's responsibility, matching every other
//! repository here, but the caller must not skip it for this
//! function the way it safely could for a single independent UPDATE.

use crate::pending_directive_repository::{DirectiveEvent, DirectiveEventData};
use chrono::{DateTime, NaiveDateTime, Utc};
use rusqlite::{Connection, OptionalExtension, Result, Row, ToSql};

/// One row of the `scheduled_directive` table.
#[derive(Debug, Clone, PartialEq)]
pub struct ScheduledDirectiveRow {
    pub directive_id: String,
    pub agent_id: String,
    pub prompt: String,
    pub interval_seconds: i64,
    pub next_due_at: String,
    pub enabled: bool,
    pub status: String,
    pub until_at: Option<String>,
    pub max_runs: Option<i64>,
    pub run_count: i64,
    pub created_at: String,
    pub created_by: Option<String>,
    pub updated_at: Option<String>,
    pub updated_by: Option<String>,
}

const COLUMNS: &str =
    "directive_id, agent_id, prompt, interval_seconds, next_due_at, enabled, status, \
     until_at, max_runs, run_count, created_at, created_by, updated_at, updated_by";

fn row_to_directive(row: &Row) -> rusqlite::Result<ScheduledDirectiveRow> {
    Ok(ScheduledDirectiveRow {
        directive_id: row.get(0)?,
        agent_id: row.get(1)?,
        prompt: row.get(2)?,
        interval_seconds: row.get(3)?,
        next_due_at: row.get(4)?,
        enabled: row.get(5)?,
        status: row.get(6)?,
        until_at: row.get(7)?,
        max_runs: row.get(8)?,
        run_count: row.get(9)?,
        created_at: row.get(10)?,
        created_by: row.get(11)?,
        updated_at: row.get(12)?,
        updated_by: row.get(13)?,
    })
}

pub fn get(conn: &Connection, directive_id: &str) -> Result<Option<ScheduledDirectiveRow>> {
    conn.query_row(
        &format!("SELECT {COLUMNS} FROM scheduled_directive WHERE directive_id = ?1"),
        [directive_id],
        row_to_directive,
    )
    .optional()
}

/// Soonest-due first for one agent.
pub fn list_for_agent(conn: &Connection, agent_id: &str) -> Result<Vec<ScheduledDirectiveRow>> {
    let mut stmt = conn.prepare(&format!(
        "SELECT {COLUMNS} FROM scheduled_directive WHERE agent_id = ?1 ORDER BY next_due_at ASC"
    ))?;
    let rows = stmt.query_map([agent_id], row_to_directive)?;
    rows.collect()
}

/// Project-wide, grouped by agent then soonest-due.
pub fn list_all(conn: &Connection) -> Result<Vec<ScheduledDirectiveRow>> {
    let mut stmt = conn.prepare(&format!(
        "SELECT {COLUMNS} FROM scheduled_directive ORDER BY agent_id ASC, next_due_at ASC"
    ))?;
    let rows = stmt.query_map([], row_to_directive)?;
    rows.collect()
}

/// The guardrail count backing `config_max_schedules_per_agent`.
pub fn count_active_for_agent(conn: &Connection, agent_id: &str) -> Result<i64> {
    conn.query_row(
        "SELECT COUNT(*) FROM scheduled_directive WHERE agent_id = ?1 AND enabled = 1 AND status = 'active'",
        [agent_id],
        |row| row.get(0),
    )
}

/// INSERT a fresh `active`/`enabled` schedule with `run_count = 0`.
/// Returns the row built from its own INSERT parameters, not a
/// re-`SELECT` — matches the `pending_directive_repository::
/// create_poke` pattern. A duplicate `directive_id` surfaces as a
/// real `rusqlite::Error` (PK violation); Python has no pre-check
/// here either.
#[allow(clippy::too_many_arguments)]
pub fn create(
    conn: &Connection,
    directive_id: &str,
    agent_id: &str,
    prompt: &str,
    interval_seconds: i64,
    next_due_at: &str,
    until_at: Option<&str>,
    max_runs: Option<i64>,
    created_by: Option<&str>,
    now_iso: &str,
) -> Result<ScheduledDirectiveRow> {
    conn.execute(
        "INSERT INTO scheduled_directive (directive_id, agent_id, prompt, interval_seconds, next_due_at, \
         enabled, status, until_at, max_runs, run_count, created_at, created_by, updated_at, updated_by) \
         VALUES (?1, ?2, ?3, ?4, ?5, 1, 'active', ?6, ?7, 0, ?8, ?9, ?8, ?9)",
        (directive_id, agent_id, prompt, interval_seconds, next_due_at, until_at, max_runs, now_iso, created_by),
    )?;
    Ok(ScheduledDirectiveRow {
        directive_id: directive_id.to_string(),
        agent_id: agent_id.to_string(),
        prompt: prompt.to_string(),
        interval_seconds,
        next_due_at: next_due_at.to_string(),
        enabled: true,
        status: "active".to_string(),
        until_at: until_at.map(String::from),
        max_runs,
        run_count: 0,
        created_at: now_iso.to_string(),
        created_by: created_by.map(String::from),
        updated_at: Some(now_iso.to_string()),
        updated_by: created_by.map(String::from),
    })
}

/// A nullable column's update instruction — `Unchanged` (default,
/// matching a key absent from Python's `fields` dict), `Clear` (set
/// `NULL`, matching a key present with a `None` value), or `Set`
/// (matching a key present with a real value). Plain `Option<T>`
/// would collapse the "absent" and "explicitly null" cases together
/// (clippy's `option_option` lint also flags `Option<Option<T>>` as
/// confusing), so this is a real 3-state enum instead.
#[derive(Debug, Clone, Default, PartialEq)]
pub enum NullableUpdate<T> {
    #[default]
    Unchanged,
    Clear,
    Set(T),
}

/// The allowlisted columns `update_fields` may touch — a closed
/// struct (every field optional/`Unchanged` by default), matching
/// Python's dict-of-allowed-keys but making an off-allowlist column
/// a compile error instead of a silently-ignored dict key.
#[derive(Debug, Clone, Default)]
pub struct ScheduledDirectiveFields {
    pub prompt: Option<String>,
    pub interval_seconds: Option<i64>,
    pub next_due_at: Option<String>,
    pub enabled: Option<bool>,
    pub status: Option<String>,
    pub until_at: NullableUpdate<String>,
    pub max_runs: NullableUpdate<i64>,
    pub run_count: Option<i64>,
}

/// Partial UPDATE of the allowed columns; always refreshes
/// `updated_at`/`updated_by` regardless of whether any other field
/// changed (matching Python exactly — even an all-`Unchanged` call
/// still bumps them). `None` if `directive_id` doesn't exist.
pub fn update_fields(
    conn: &Connection,
    directive_id: &str,
    fields: &ScheduledDirectiveFields,
    updated_by: &str,
    now_iso: &str,
) -> Result<Option<ScheduledDirectiveRow>> {
    if get(conn, directive_id)?.is_none() {
        return Ok(None);
    }

    let mut set_clauses: Vec<&str> = Vec::new();
    let mut params: Vec<Box<dyn ToSql>> = Vec::new();

    if let Some(v) = &fields.prompt {
        set_clauses.push("prompt = ?");
        params.push(Box::new(v.clone()));
    }
    if let Some(v) = fields.interval_seconds {
        set_clauses.push("interval_seconds = ?");
        params.push(Box::new(v));
    }
    if let Some(v) = &fields.next_due_at {
        set_clauses.push("next_due_at = ?");
        params.push(Box::new(v.clone()));
    }
    if let Some(v) = fields.enabled {
        set_clauses.push("enabled = ?");
        params.push(Box::new(v));
    }
    if let Some(v) = &fields.status {
        set_clauses.push("status = ?");
        params.push(Box::new(v.clone()));
    }
    match &fields.until_at {
        NullableUpdate::Unchanged => {}
        NullableUpdate::Clear => set_clauses.push("until_at = NULL"),
        NullableUpdate::Set(v) => {
            set_clauses.push("until_at = ?");
            params.push(Box::new(v.clone()));
        }
    }
    match &fields.max_runs {
        NullableUpdate::Unchanged => {}
        NullableUpdate::Clear => set_clauses.push("max_runs = NULL"),
        NullableUpdate::Set(v) => {
            set_clauses.push("max_runs = ?");
            params.push(Box::new(*v));
        }
    }
    if let Some(v) = fields.run_count {
        set_clauses.push("run_count = ?");
        params.push(Box::new(v));
    }

    set_clauses.push("updated_at = ?");
    params.push(Box::new(now_iso.to_string()));
    set_clauses.push("updated_by = ?");
    params.push(Box::new(updated_by.to_string()));
    params.push(Box::new(directive_id.to_string()));

    let sql = format!(
        "UPDATE scheduled_directive SET {} WHERE directive_id = ?",
        set_clauses.join(", ")
    );
    let param_refs: Vec<&dyn ToSql> = params.iter().map(|b| b.as_ref()).collect();
    conn.execute(&sql, param_refs.as_slice())?;

    get(conn, directive_id)
}

/// `true` iff a row existed and was removed.
pub fn delete(conn: &Connection, directive_id: &str) -> Result<bool> {
    let changed = conn.execute(
        "DELETE FROM scheduled_directive WHERE directive_id = ?1",
        [directive_id],
    )?;
    Ok(changed > 0)
}

/// Soonest `next_due_at` among the agent's still-fireable schedules —
/// `None` means no wake condition (nothing enabled/active, or every
/// active schedule's window has closed). Backs the idle-stop
/// suppression gate ([`has_active`]).
pub fn soonest_due_at(conn: &Connection, agent_id: &str, now_iso: &str) -> Result<Option<String>> {
    conn.query_row(
        "SELECT MIN(next_due_at) FROM scheduled_directive \
         WHERE agent_id = ?1 AND enabled = 1 AND status = 'active' AND (until_at IS NULL OR until_at > ?2)",
        (agent_id, now_iso),
        |row| row.get(0),
    )
}

/// `true` iff the agent has at least one fireable schedule — the
/// idle-stop suppression gate.
pub fn has_active(conn: &Connection, agent_id: &str, now_iso: &str) -> Result<bool> {
    Ok(soonest_due_at(conn, agent_id, now_iso)?.is_some())
}

/// Failure modes of [`collect_due_and_fire`]: a real DB error, or a
/// stored timestamp this crate's flexible ISO-8601 parser couldn't
/// make sense of (a data-integrity condition, not a normal runtime
/// one — every timestamp this crate itself writes is one of the two
/// formats the parser accepts).
#[derive(Debug)]
pub enum CollectDueError {
    Db(rusqlite::Error),
    InvalidTimestamp(String),
}

impl From<rusqlite::Error> for CollectDueError {
    fn from(e: rusqlite::Error) -> Self {
        CollectDueError::Db(e)
    }
}

impl std::fmt::Display for CollectDueError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CollectDueError::Db(e) => write!(f, "database error: {e}"),
            CollectDueError::InvalidTimestamp(s) => {
                write!(f, "unrecognized timestamp format: {s:?}")
            }
        }
    }
}

impl std::error::Error for CollectDueError {}

/// Parses either RFC3339 (`...Z` / `...+00:00`) or a tz-less ISO-8601
/// timestamp (this codebase's `created_at`/`updated_at` columns are
/// often naive — Python's tz-less `datetime.now().isoformat()`), both
/// treated as UTC. This crate never reads a wall clock itself (see
/// the module doc on that rule); this only ever operates on caller-
/// or DB-supplied timestamp strings.
fn parse_flexible(timestamp: &str) -> Result<DateTime<Utc>, CollectDueError> {
    if let Ok(dt) = DateTime::parse_from_rfc3339(timestamp) {
        return Ok(dt.with_timezone(&Utc));
    }
    for fmt in ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%dT%H:%M:%S"] {
        if let Ok(naive) = NaiveDateTime::parse_from_str(timestamp, fmt) {
            return Ok(naive.and_utc());
        }
    }
    Err(CollectDueError::InvalidTimestamp(timestamp.to_string()))
}

/// `now + interval_seconds`, normalized to RFC3339 UTC with
/// microsecond precision and a `Z` suffix — this is a fresh,
/// Rust-computed value (unlike a raw DB/caller string, whose original
/// format this crate has no need to preserve), so picking one
/// canonical, unambiguous output format here is a deliberate,
/// harmless choice, not a fidelity gap.
fn add_seconds_iso(now_iso: &str, seconds: i64) -> Result<String, CollectDueError> {
    let base = parse_flexible(now_iso)?;
    let shifted = base + chrono::Duration::seconds(seconds);
    Ok(shifted.to_rfc3339_opts(chrono::SecondsFormat::Micros, true))
}

struct ComputedFire {
    new_next_due_at: String,
    new_run_count: i64,
    completed: bool,
}

/// Pure computation (no SQL) — port of Python's
/// `_compute_next_and_terminal`. `completed` is set by EITHER the
/// `max_runs` ceiling being reached OR the freshly-computed next fire
/// landing past `until_at`; the latter comparison parses both sides
/// via [`parse_flexible`] rather than a raw string compare (unlike
/// the SQL-mirrored window-closed check in [`collect_due_and_fire`]),
/// since `until_at` may be in a caller-supplied format that doesn't
/// happen to share this function's own canonical output format —
/// a real-datetime comparison is strictly more correct here and never
/// changes the intended outcome when formats do agree.
fn compute_next_and_terminal(
    run_count: i64,
    interval_seconds: i64,
    max_runs: Option<i64>,
    until_at: Option<&str>,
    now_iso: &str,
) -> Result<ComputedFire, CollectDueError> {
    let new_run_count = run_count + 1;
    let new_next_due_at = add_seconds_iso(now_iso, interval_seconds)?;

    let mut completed = matches!(max_runs, Some(m) if new_run_count >= m);
    if !completed {
        if let Some(until) = until_at {
            let until_dt = parse_flexible(until)?;
            let next_dt = parse_flexible(&new_next_due_at)?;
            if next_dt > until_dt {
                completed = true;
            }
        }
    }

    Ok(ComputedFire {
        new_next_due_at,
        new_run_count,
        completed,
    })
}

struct Candidate {
    directive_id: String,
    prompt: String,
    interval_seconds: i64,
    run_count: i64,
    max_runs: Option<i64>,
    until_at: Option<String>,
}

/// The firing step. Selects every schedule for `agent_id` that is
/// either genuinely due (`next_due_at <= now_iso`) or whose `until_at`
/// window has already closed, then per row either:
///
/// - **reaps without firing** (window already closed): flips to
///   `status = "completed", enabled = 0`, `run_count` untouched, NO
///   event appended;
/// - **fires** (still fireable): advances `next_due_at`/`run_count`
///   via [`compute_next_and_terminal`], flips to `status =
///   "completed", enabled = 0` too if that computation says this is
///   the last allowed fire — but UNLIKE the reap case, still appends
///   an event even on the terminal fire.
///
/// See the module doc for the concurrency contract this function
/// requires from its caller (it is NOT self-healing/idempotent —
/// a failed downstream push after this commits is a LOST fire, not a
/// retried one).
pub fn collect_due_and_fire(
    conn: &Connection,
    agent_id: &str,
    now_iso: &str,
) -> Result<Vec<DirectiveEvent>, CollectDueError> {
    let mut stmt = conn.prepare(
        "SELECT directive_id, prompt, interval_seconds, run_count, max_runs, until_at FROM scheduled_directive \
         WHERE agent_id = ?1 AND enabled = 1 AND status = 'active' \
         AND (next_due_at <= ?2 OR (until_at IS NOT NULL AND until_at <= ?2)) \
         ORDER BY next_due_at ASC",
    )?;
    let candidates: Vec<Candidate> = stmt
        .query_map((agent_id, now_iso), |row| {
            Ok(Candidate {
                directive_id: row.get(0)?,
                prompt: row.get(1)?,
                interval_seconds: row.get(2)?,
                run_count: row.get(3)?,
                max_runs: row.get(4)?,
                until_at: row.get(5)?,
            })
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    drop(stmt);

    let mut events = Vec::new();
    for c in candidates {
        // Mirrors the SQL's own `until_at <= now_iso` predicate
        // exactly (plain string compare) — this decides whether the
        // row was pulled in because it's window-closed, so it must
        // use the identical comparison the SQL used to select it.
        if c.until_at.as_deref().is_some_and(|u| u <= now_iso) {
            conn.execute(
                "UPDATE scheduled_directive SET status = 'completed', enabled = 0, updated_at = ?1, \
                 updated_by = 'system' WHERE directive_id = ?2",
                (now_iso, &c.directive_id),
            )?;
            continue; // reaped -- no event
        }

        let fire = compute_next_and_terminal(
            c.run_count,
            c.interval_seconds,
            c.max_runs,
            c.until_at.as_deref(),
            now_iso,
        )?;

        if fire.completed {
            conn.execute(
                "UPDATE scheduled_directive SET run_count = ?1, next_due_at = ?2, status = 'completed', \
                 enabled = 0, updated_at = ?3, updated_by = 'system' WHERE directive_id = ?4",
                (fire.new_run_count, &fire.new_next_due_at, now_iso, &c.directive_id),
            )?;
        } else {
            conn.execute(
                "UPDATE scheduled_directive SET run_count = ?1, next_due_at = ?2, updated_at = ?3, \
                 updated_by = 'system' WHERE directive_id = ?4",
                (fire.new_run_count, &fire.new_next_due_at, now_iso, &c.directive_id),
            )?;
        }

        events.push(DirectiveEvent {
            event_type: "directive".to_string(),
            ref_id: c.directive_id.clone(),
            timestamp: now_iso.to_string(),
            priority: "urgent".to_string(),
            data: DirectiveEventData {
                prompt: c.prompt,
                source: "schedule".to_string(),
                schedule_id: Some(c.directive_id),
            },
        });
    }

    Ok(events)
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

    #[allow(clippy::too_many_arguments)]
    fn seed(
        conn: &Connection,
        directive_id: &str,
        agent_id: &str,
        interval_seconds: i64,
        next_due_at: &str,
        until_at: Option<&str>,
        max_runs: Option<i64>,
    ) -> ScheduledDirectiveRow {
        create(
            conn,
            directive_id,
            agent_id,
            "check in",
            interval_seconds,
            next_due_at,
            until_at,
            max_runs,
            Some("admin"),
            "2026-01-01T00:00:00Z",
        )
        .unwrap()
    }

    #[test]
    fn create_returns_the_row_it_just_inserted_with_zeroed_run_count() {
        let conn = test_conn();
        let row = seed(
            &conn,
            "s1",
            "alice",
            3600,
            "2026-01-01T01:00:00Z",
            None,
            None,
        );
        assert_eq!(row.status, "active");
        assert!(row.enabled);
        assert_eq!(row.run_count, 0);
        assert_eq!(row.until_at, None);
        assert_eq!(row.max_runs, None);
    }

    #[test]
    fn get_returns_none_for_unknown_directive() {
        let conn = test_conn();
        assert_eq!(get(&conn, "nope").unwrap(), None);
    }

    #[test]
    fn list_for_agent_orders_by_next_due_at_ascending() {
        let conn = test_conn();
        seed(
            &conn,
            "s-late",
            "alice",
            3600,
            "2026-01-02T00:00:00Z",
            None,
            None,
        );
        seed(
            &conn,
            "s-early",
            "alice",
            3600,
            "2026-01-01T00:00:00Z",
            None,
            None,
        );

        let rows = list_for_agent(&conn, "alice").unwrap();
        let ids: Vec<&str> = rows.iter().map(|r| r.directive_id.as_str()).collect();
        assert_eq!(ids, vec!["s-early", "s-late"]);
    }

    #[test]
    fn list_all_groups_by_agent_then_due_time() {
        let conn = test_conn();
        seed(&conn, "b1", "bob", 3600, "2026-01-01T00:00:00Z", None, None);
        seed(
            &conn,
            "a2",
            "alice",
            3600,
            "2026-01-02T00:00:00Z",
            None,
            None,
        );
        seed(
            &conn,
            "a1",
            "alice",
            3600,
            "2026-01-01T00:00:00Z",
            None,
            None,
        );

        let rows = list_all(&conn).unwrap();
        let ids: Vec<&str> = rows.iter().map(|r| r.directive_id.as_str()).collect();
        assert_eq!(ids, vec!["a1", "a2", "b1"]);
    }

    #[test]
    fn count_active_for_agent_excludes_disabled_and_completed() {
        let conn = test_conn();
        seed(
            &conn,
            "s1",
            "alice",
            3600,
            "2026-01-01T00:00:00Z",
            None,
            None,
        );
        seed(
            &conn,
            "s2",
            "alice",
            3600,
            "2026-01-01T00:00:00Z",
            None,
            None,
        );
        update_fields(
            &conn,
            "s2",
            &ScheduledDirectiveFields {
                enabled: Some(false),
                ..Default::default()
            },
            "admin",
            "2026-01-01T00:00:01Z",
        )
        .unwrap();

        assert_eq!(count_active_for_agent(&conn, "alice").unwrap(), 1);
    }

    #[test]
    fn update_fields_unknown_directive_returns_none() {
        let conn = test_conn();
        let result = update_fields(
            &conn,
            "nope",
            &ScheduledDirectiveFields::default(),
            "admin",
            "2026-01-01T00:00:00Z",
        );
        assert_eq!(result.unwrap(), None);
    }

    #[test]
    fn update_fields_always_bumps_updated_at_even_with_no_field_changes() {
        let conn = test_conn();
        seed(
            &conn,
            "s1",
            "alice",
            3600,
            "2026-01-01T00:00:00Z",
            None,
            None,
        );

        let row = update_fields(
            &conn,
            "s1",
            &ScheduledDirectiveFields::default(),
            "bob",
            "2026-01-02T00:00:00Z",
        )
        .unwrap()
        .unwrap();
        assert_eq!(row.updated_at.as_deref(), Some("2026-01-02T00:00:00Z"));
        assert_eq!(row.updated_by.as_deref(), Some("bob"));
    }

    #[test]
    fn update_fields_can_pause_a_schedule() {
        let conn = test_conn();
        seed(
            &conn,
            "s1",
            "alice",
            3600,
            "2026-01-01T00:00:00Z",
            None,
            None,
        );

        let row = update_fields(
            &conn,
            "s1",
            &ScheduledDirectiveFields {
                enabled: Some(false),
                status: Some("paused".to_string()),
                ..Default::default()
            },
            "admin",
            "2026-01-01T00:00:01Z",
        )
        .unwrap()
        .unwrap();
        assert!(!row.enabled);
        assert_eq!(row.status, "paused");
    }

    #[test]
    fn update_fields_nullable_update_can_clear_and_set_until_at() {
        let conn = test_conn();
        seed(
            &conn,
            "s1",
            "alice",
            3600,
            "2026-01-01T00:00:00Z",
            Some("2026-06-01T00:00:00Z"),
            None,
        );

        let cleared = update_fields(
            &conn,
            "s1",
            &ScheduledDirectiveFields {
                until_at: NullableUpdate::Clear,
                ..Default::default()
            },
            "admin",
            "2026-01-01T00:00:01Z",
        )
        .unwrap()
        .unwrap();
        assert_eq!(cleared.until_at, None);

        let set_again = update_fields(
            &conn,
            "s1",
            &ScheduledDirectiveFields {
                until_at: NullableUpdate::Set("2026-07-01T00:00:00Z".to_string()),
                ..Default::default()
            },
            "admin",
            "2026-01-01T00:00:02Z",
        )
        .unwrap()
        .unwrap();
        assert_eq!(set_again.until_at.as_deref(), Some("2026-07-01T00:00:00Z"));
    }

    #[test]
    fn delete_removes_row_and_returns_true() {
        let conn = test_conn();
        seed(
            &conn,
            "s1",
            "alice",
            3600,
            "2026-01-01T00:00:00Z",
            None,
            None,
        );
        assert!(delete(&conn, "s1").unwrap());
        assert_eq!(get(&conn, "s1").unwrap(), None);
    }

    #[test]
    fn delete_missing_directive_returns_false() {
        let conn = test_conn();
        assert!(!delete(&conn, "nope").unwrap());
    }

    #[test]
    fn soonest_due_at_none_when_nothing_fireable() {
        let conn = test_conn();
        assert_eq!(
            soonest_due_at(&conn, "alice", "2026-01-01T00:00:00Z").unwrap(),
            None
        );
    }

    #[test]
    fn soonest_due_at_excludes_windows_already_closed() {
        let conn = test_conn();
        seed(
            &conn,
            "s1",
            "alice",
            3600,
            "2026-06-01T00:00:00Z",
            Some("2026-01-01T00:00:00Z"),
            None,
        );
        // until_at already passed relative to "now" -> not fireable.
        assert_eq!(
            soonest_due_at(&conn, "alice", "2026-01-02T00:00:00Z").unwrap(),
            None
        );
    }

    #[test]
    fn has_active_reflects_soonest_due_at() {
        let conn = test_conn();
        assert!(!has_active(&conn, "alice", "2026-01-01T00:00:00Z").unwrap());
        seed(
            &conn,
            "s1",
            "alice",
            3600,
            "2026-06-01T00:00:00Z",
            None,
            None,
        );
        assert!(has_active(&conn, "alice", "2026-01-01T00:00:00Z").unwrap());
    }

    #[test]
    fn fire_resets_next_due_from_delivery_time_not_the_old_grid() {
        let conn = test_conn();
        // Overdue by 5 minutes, 60s interval.
        seed(&conn, "s1", "alice", 60, "2026-01-01T00:00:00Z", None, None);

        let now = "2026-01-01T00:05:00Z";
        let events = collect_due_and_fire(&conn, "alice", now).unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].event_type, "directive");
        assert_eq!(events[0].ref_id, "s1");
        assert_eq!(events[0].priority, "urgent");
        assert_eq!(events[0].data.source, "schedule");
        assert_eq!(events[0].data.schedule_id.as_deref(), Some("s1"));

        let row = get(&conn, "s1").unwrap().unwrap();
        assert_eq!(row.run_count, 1);
        assert_eq!(row.status, "active");
        assert!(row.enabled);
        // next_due_at must be `now + 60s`, NOT the old grid position
        // (`2026-01-01T00:01:00Z`, i.e. old next_due_at + interval).
        assert_eq!(row.next_due_at, "2026-01-01T00:06:00.000000Z");
    }

    #[test]
    fn offline_across_many_intervals_fires_exactly_once() {
        let conn = test_conn();
        // 15-minute interval, overdue by 3 days (288 missed slots).
        seed(
            &conn,
            "s1",
            "alice",
            900,
            "2025-12-29T00:00:00Z",
            None,
            None,
        );

        let now = "2026-01-01T00:00:00Z";
        let events = collect_due_and_fire(&conn, "alice", now).unwrap();
        assert_eq!(
            events.len(),
            1,
            "must fire exactly once regardless of how many intervals were missed"
        );

        let row = get(&conn, "s1").unwrap().unwrap();
        assert_eq!(row.run_count, 1);
        assert_eq!(row.next_due_at, "2026-01-01T00:15:00.000000Z");
    }

    #[test]
    fn max_runs_end_condition_completes_but_still_fires_the_last_event() {
        let conn = test_conn();
        seed(
            &conn,
            "s1",
            "alice",
            60,
            "2026-01-01T00:00:00Z",
            None,
            Some(1),
        );

        let events = collect_due_and_fire(&conn, "alice", "2026-01-01T00:00:00Z").unwrap();
        assert_eq!(events.len(), 1, "the terminal fire still emits an event");

        let row = get(&conn, "s1").unwrap().unwrap();
        assert_eq!(row.run_count, 1);
        assert_eq!(row.status, "completed");
        assert!(!row.enabled);
        assert!(!has_active(&conn, "alice", "2026-01-01T00:00:01Z").unwrap());
    }

    #[test]
    fn until_window_next_fire_beyond_completes() {
        let conn = test_conn();
        // until_at 30s out, interval 60s -> the computed next-due
        // (now+60) exceeds until_at, so THIS fire is the last one.
        seed(
            &conn,
            "s1",
            "alice",
            60,
            "2026-01-01T00:00:00Z",
            Some("2026-01-01T00:00:30Z"),
            None,
        );

        let events = collect_due_and_fire(&conn, "alice", "2026-01-01T00:00:00Z").unwrap();
        assert_eq!(events.len(), 1);

        let row = get(&conn, "s1").unwrap().unwrap();
        assert_eq!(row.status, "completed");
        assert!(!row.enabled);
        assert_eq!(row.run_count, 1);
    }

    #[test]
    fn until_already_passed_reaps_without_firing() {
        let conn = test_conn();
        seed(
            &conn,
            "s1",
            "alice",
            60,
            "2025-12-01T00:00:00Z",
            Some("2025-12-31T00:00:00Z"),
            None,
        );

        let events = collect_due_and_fire(&conn, "alice", "2026-01-01T00:00:00Z").unwrap();
        assert_eq!(
            events,
            Vec::new(),
            "a closed window must be reaped, not fired"
        );

        let row = get(&conn, "s1").unwrap().unwrap();
        assert_eq!(row.status, "completed");
        assert!(!row.enabled);
        assert_eq!(
            row.run_count, 0,
            "run_count must stay untouched -- it never actually fired"
        );
    }

    #[test]
    fn not_yet_due_does_not_fire() {
        let conn = test_conn();
        seed(&conn, "s1", "alice", 60, "2026-06-01T00:00:00Z", None, None);

        let events = collect_due_and_fire(&conn, "alice", "2026-01-01T00:00:00Z").unwrap();
        assert_eq!(events, Vec::new());
        assert!(has_active(&conn, "alice", "2026-01-01T00:00:00Z").unwrap());
    }

    #[test]
    fn disabled_schedule_never_fires_or_counts() {
        let conn = test_conn();
        seed(&conn, "s1", "alice", 60, "2026-01-01T00:00:00Z", None, None);
        update_fields(
            &conn,
            "s1",
            &ScheduledDirectiveFields {
                enabled: Some(false),
                status: Some("paused".to_string()),
                ..Default::default()
            },
            "admin",
            "2026-01-01T00:00:00Z",
        )
        .unwrap();

        let events = collect_due_and_fire(&conn, "alice", "2026-01-01T00:00:01Z").unwrap();
        assert_eq!(events, Vec::new());
        assert_eq!(count_active_for_agent(&conn, "alice").unwrap(), 0);
        assert!(!has_active(&conn, "alice", "2026-01-01T00:00:01Z").unwrap());
    }

    #[test]
    fn collect_due_and_fire_is_scoped_per_agent() {
        let conn = test_conn();
        seed(&conn, "s1", "alice", 60, "2026-01-01T00:00:00Z", None, None);
        seed(&conn, "s2", "bob", 60, "2026-01-01T00:00:00Z", None, None);

        let events = collect_due_and_fire(&conn, "alice", "2026-01-01T00:00:01Z").unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].ref_id, "s1");
        // bob's schedule must still be pending, untouched.
        assert_eq!(get(&conn, "s2").unwrap().unwrap().run_count, 0);
    }

    #[test]
    fn parse_flexible_accepts_rfc3339_and_naive_iso8601() {
        assert!(parse_flexible("2026-01-01T00:00:00Z").is_ok());
        assert!(parse_flexible("2026-01-01T00:00:00+00:00").is_ok());
        assert!(parse_flexible("2026-01-01T00:00:00.123456").is_ok());
        assert!(parse_flexible("2026-01-01T00:00:00").is_ok());
        assert!(parse_flexible("not a timestamp").is_err());
    }
}
