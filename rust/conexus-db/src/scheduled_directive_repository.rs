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
//!
//! Phase G (sea-orm migration): the seventh repository converted.
//! Every function that touches the DB takes the `&DatabaseConnection`
//! it should run against — this crate has no separate "opens its own
//! connection" path, matching every other repository here.
//! [`parse_flexible`] stays a pure, connection-less function
//! throughout — it never touches the DB, and is reused as a plain
//! date-parsing utility elsewhere in the workspace
//! (`conexus-wakeloop`, `conexus-router`).

use crate::pending_directive_repository::{DirectiveEvent, DirectiveEventData};
use chrono::{DateTime, NaiveDateTime, Utc};
use sea_orm::{
    ActiveValue::Set, ColumnTrait, Condition, DatabaseConnection, DbErr, EntityTrait,
    PaginatorTrait, QueryFilter, QueryOrder,
};

pub use crate::entity::scheduled_directive::Model as ScheduledDirectiveRow;
use crate::entity::scheduled_directive::{ActiveModel, Column, Entity};

pub async fn get(
    db: &DatabaseConnection,
    directive_id: &str,
) -> Result<Option<ScheduledDirectiveRow>, DbErr> {
    Entity::find_by_id(directive_id.to_string()).one(db).await
}

/// Soonest-due first for one agent.
pub async fn list_for_agent(
    db: &DatabaseConnection,
    agent_id: &str,
) -> Result<Vec<ScheduledDirectiveRow>, DbErr> {
    Entity::find()
        .filter(Column::AgentId.eq(agent_id))
        .order_by_asc(Column::NextDueAt)
        .all(db)
        .await
}

/// Project-wide, grouped by agent then soonest-due.
pub async fn list_all(db: &DatabaseConnection) -> Result<Vec<ScheduledDirectiveRow>, DbErr> {
    Entity::find()
        .order_by_asc(Column::AgentId)
        .order_by_asc(Column::NextDueAt)
        .all(db)
        .await
}

/// The guardrail count backing `config_max_schedules_per_agent`.
pub async fn count_active_for_agent(db: &DatabaseConnection, agent_id: &str) -> Result<i64, DbErr> {
    let count = Entity::find()
        .filter(Column::AgentId.eq(agent_id))
        .filter(Column::Enabled.eq(true))
        .filter(Column::Status.eq("active"))
        .count(db)
        .await?;
    Ok(count as i64)
}

/// INSERT a fresh `active`/`enabled` schedule with `run_count = 0`.
/// Returns the row built from its own INSERT parameters, not a
/// re-`SELECT` — matches the `pending_directive_repository::
/// create_poke` pattern. A duplicate `directive_id` surfaces as a
/// real `DbErr` (PK violation); Python has no pre-check here either.
#[allow(clippy::too_many_arguments)]
pub async fn create(
    db: &DatabaseConnection,
    directive_id: &str,
    agent_id: &str,
    prompt: &str,
    interval_seconds: i64,
    next_due_at: &str,
    until_at: Option<&str>,
    max_runs: Option<i64>,
    created_by: Option<&str>,
    now_iso: &str,
) -> Result<ScheduledDirectiveRow, DbErr> {
    let am = ActiveModel {
        directive_id: Set(directive_id.to_string()),
        agent_id: Set(agent_id.to_string()),
        prompt: Set(prompt.to_string()),
        interval_seconds: Set(interval_seconds),
        next_due_at: Set(next_due_at.to_string()),
        enabled: Set(true),
        status: Set("active".to_string()),
        until_at: Set(until_at.map(String::from)),
        max_runs: Set(max_runs),
        run_count: Set(0),
        created_at: Set(now_iso.to_string()),
        created_by: Set(created_by.map(String::from)),
        updated_at: Set(Some(now_iso.to_string())),
        updated_by: Set(created_by.map(String::from)),
    };
    Entity::insert(am).exec(db).await?;
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
/// confusing), so this is a real 3-state enum instead. Maps onto
/// sea-orm's own `ActiveValue` 2-state (`Set`/`NotSet`) exactly:
/// `Unchanged` leaves the `ActiveModel` field `NotSet` (untouched by
/// the generated `UPDATE`), `Clear` becomes `Set(None)`, and `Set(v)`
/// becomes `Set(Some(v))`.
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
///
/// Built as an `ActiveModel` with the primary key `Set` and every
/// other field either `Set` (a real change) or left `NotSet` (an
/// `Unchanged` field never appears in the generated `UPDATE ... SET`
/// list at all) — `Entity::update(am).exec(db)` both performs the
/// partial write and returns the refreshed row in one round trip, so
/// there is no separate re-`SELECT` the way the old raw-SQL version
/// needed.
pub async fn update_fields(
    db: &DatabaseConnection,
    directive_id: &str,
    fields: &ScheduledDirectiveFields,
    updated_by: &str,
    now_iso: &str,
) -> Result<Option<ScheduledDirectiveRow>, DbErr> {
    if get(db, directive_id).await?.is_none() {
        return Ok(None);
    }

    let mut am = ActiveModel {
        directive_id: Set(directive_id.to_string()),
        ..Default::default()
    };

    if let Some(v) = &fields.prompt {
        am.prompt = Set(v.clone());
    }
    if let Some(v) = fields.interval_seconds {
        am.interval_seconds = Set(v);
    }
    if let Some(v) = &fields.next_due_at {
        am.next_due_at = Set(v.clone());
    }
    if let Some(v) = fields.enabled {
        am.enabled = Set(v);
    }
    if let Some(v) = &fields.status {
        am.status = Set(v.clone());
    }
    match &fields.until_at {
        NullableUpdate::Unchanged => {}
        NullableUpdate::Clear => am.until_at = Set(None),
        NullableUpdate::Set(v) => am.until_at = Set(Some(v.clone())),
    }
    match &fields.max_runs {
        NullableUpdate::Unchanged => {}
        NullableUpdate::Clear => am.max_runs = Set(None),
        NullableUpdate::Set(v) => am.max_runs = Set(Some(*v)),
    }
    if let Some(v) = fields.run_count {
        am.run_count = Set(v);
    }

    am.updated_at = Set(Some(now_iso.to_string()));
    am.updated_by = Set(Some(updated_by.to_string()));

    let row = Entity::update(am).exec(db).await?;
    Ok(Some(row))
}

/// `true` iff a row existed and was removed.
pub async fn delete(db: &DatabaseConnection, directive_id: &str) -> Result<bool, DbErr> {
    let result = Entity::delete_by_id(directive_id.to_string())
        .exec(db)
        .await?;
    Ok(result.rows_affected > 0)
}

/// Soonest `next_due_at` among the agent's still-fireable schedules —
/// `None` means no wake condition (nothing enabled/active, or every
/// active schedule's window has closed). Backs the idle-stop
/// suppression gate ([`has_active`]).
///
/// Ordering by `next_due_at ASC` and taking the first row is
/// equivalent to a SQL `MIN(next_due_at)` over the same filter (both
/// return the smallest value among the matching rows, or nothing when
/// none match) — this reads more naturally through sea-orm's query
/// builder than a raw scalar aggregate would, with no behavioral
/// difference.
pub async fn soonest_due_at(
    db: &DatabaseConnection,
    agent_id: &str,
    now_iso: &str,
) -> Result<Option<String>, DbErr> {
    let row = Entity::find()
        .filter(Column::AgentId.eq(agent_id))
        .filter(Column::Enabled.eq(true))
        .filter(Column::Status.eq("active"))
        .filter(
            Condition::any()
                .add(Column::UntilAt.is_null())
                .add(Column::UntilAt.gt(now_iso)),
        )
        .order_by_asc(Column::NextDueAt)
        .one(db)
        .await?;
    Ok(row.map(|r| r.next_due_at))
}

/// `true` iff the agent has at least one fireable schedule — the
/// idle-stop suppression gate.
pub async fn has_active(
    db: &DatabaseConnection,
    agent_id: &str,
    now_iso: &str,
) -> Result<bool, DbErr> {
    Ok(soonest_due_at(db, agent_id, now_iso).await?.is_some())
}

/// Failure modes of [`collect_due_and_fire`]: a real DB error, or a
/// stored timestamp this crate's flexible ISO-8601 parser couldn't
/// make sense of (a data-integrity condition, not a normal runtime
/// one — every timestamp this crate itself writes is one of the two
/// formats the parser accepts).
#[derive(Debug)]
pub enum CollectDueError {
    Db(DbErr),
    InvalidTimestamp(String),
}

impl From<DbErr> for CollectDueError {
    fn from(e: DbErr) -> Self {
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
///
/// `pub`, not module-private: `conexus-wakeloop`'s event-feed
/// collectors (`idle_stop_seconds_remaining`) need the exact same
/// flexible-ISO-8601 parsing this repository already solved — a
/// second copy of this dual-format fallback would be the kind of
/// drift-prone duplication this crate's own `sql_util`/
/// `pagination_cache` consolidation already avoided elsewhere. Pure
/// (no `db` parameter) and untouched by the Phase G sea-orm
/// migration — it never had any DB involvement to begin with.
pub fn parse_flexible(timestamp: &str) -> Result<DateTime<Utc>, CollectDueError> {
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
pub async fn collect_due_and_fire(
    db: &DatabaseConnection,
    agent_id: &str,
    now_iso: &str,
) -> Result<Vec<DirectiveEvent>, CollectDueError> {
    let candidates = Entity::find()
        .filter(Column::AgentId.eq(agent_id))
        .filter(Column::Enabled.eq(true))
        .filter(Column::Status.eq("active"))
        .filter(
            Condition::any().add(Column::NextDueAt.lte(now_iso)).add(
                Condition::all()
                    .add(Column::UntilAt.is_not_null())
                    .add(Column::UntilAt.lte(now_iso)),
            ),
        )
        .order_by_asc(Column::NextDueAt)
        .all(db)
        .await?;

    let mut events = Vec::new();
    for c in candidates {
        // Mirrors the SQL filter's own `until_at <= now_iso` predicate
        // exactly (plain string compare) — this decides whether the
        // row was pulled in because it's window-closed, so it must
        // use the identical comparison the query used to select it.
        if c.until_at.as_deref().is_some_and(|u| u <= now_iso) {
            let am = ActiveModel {
                directive_id: Set(c.directive_id.clone()),
                status: Set("completed".to_string()),
                enabled: Set(false),
                updated_at: Set(Some(now_iso.to_string())),
                updated_by: Set(Some("system".to_string())),
                ..Default::default()
            };
            Entity::update(am).exec(db).await?;
            continue; // reaped -- no event
        }

        let fire = compute_next_and_terminal(
            c.run_count,
            c.interval_seconds,
            c.max_runs,
            c.until_at.as_deref(),
            now_iso,
        )?;

        let mut am = ActiveModel {
            directive_id: Set(c.directive_id.clone()),
            run_count: Set(fire.new_run_count),
            next_due_at: Set(fire.new_next_due_at.clone()),
            updated_at: Set(Some(now_iso.to_string())),
            updated_by: Set(Some("system".to_string())),
            ..Default::default()
        };
        if fire.completed {
            am.status = Set("completed".to_string());
            am.enabled = Set(false);
        }
        Entity::update(am).exec(db).await?;

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

    #[allow(clippy::too_many_arguments)]
    async fn seed(
        db: &DatabaseConnection,
        directive_id: &str,
        agent_id: &str,
        interval_seconds: i64,
        next_due_at: &str,
        until_at: Option<&str>,
        max_runs: Option<i64>,
    ) -> ScheduledDirectiveRow {
        create(
            db,
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
        .await
        .unwrap()
    }

    #[tokio::test]
    async fn create_returns_the_row_it_just_inserted_with_zeroed_run_count() {
        let (_dir, db) = test_conn().await;
        let row = seed(&db, "s1", "alice", 3600, "2026-01-01T01:00:00Z", None, None).await;
        assert_eq!(row.status, "active");
        assert!(row.enabled);
        assert_eq!(row.run_count, 0);
        assert_eq!(row.until_at, None);
        assert_eq!(row.max_runs, None);
    }

    #[tokio::test]
    async fn get_returns_none_for_unknown_directive() {
        let (_dir, db) = test_conn().await;
        assert_eq!(get(&db, "nope").await.unwrap(), None);
    }

    #[tokio::test]
    async fn list_for_agent_orders_by_next_due_at_ascending() {
        let (_dir, db) = test_conn().await;
        seed(
            &db,
            "s-late",
            "alice",
            3600,
            "2026-01-02T00:00:00Z",
            None,
            None,
        )
        .await;
        seed(
            &db,
            "s-early",
            "alice",
            3600,
            "2026-01-01T00:00:00Z",
            None,
            None,
        )
        .await;

        let rows = list_for_agent(&db, "alice").await.unwrap();
        let ids: Vec<&str> = rows.iter().map(|r| r.directive_id.as_str()).collect();
        assert_eq!(ids, vec!["s-early", "s-late"]);
    }

    #[tokio::test]
    async fn list_all_groups_by_agent_then_due_time() {
        let (_dir, db) = test_conn().await;
        seed(&db, "b1", "bob", 3600, "2026-01-01T00:00:00Z", None, None).await;
        seed(&db, "a2", "alice", 3600, "2026-01-02T00:00:00Z", None, None).await;
        seed(&db, "a1", "alice", 3600, "2026-01-01T00:00:00Z", None, None).await;

        let rows = list_all(&db).await.unwrap();
        let ids: Vec<&str> = rows.iter().map(|r| r.directive_id.as_str()).collect();
        assert_eq!(ids, vec!["a1", "a2", "b1"]);
    }

    #[tokio::test]
    async fn count_active_for_agent_excludes_disabled_and_completed() {
        let (_dir, db) = test_conn().await;
        seed(&db, "s1", "alice", 3600, "2026-01-01T00:00:00Z", None, None).await;
        seed(&db, "s2", "alice", 3600, "2026-01-01T00:00:00Z", None, None).await;
        update_fields(
            &db,
            "s2",
            &ScheduledDirectiveFields {
                enabled: Some(false),
                ..Default::default()
            },
            "admin",
            "2026-01-01T00:00:01Z",
        )
        .await
        .unwrap();

        assert_eq!(count_active_for_agent(&db, "alice").await.unwrap(), 1);
    }

    #[tokio::test]
    async fn update_fields_unknown_directive_returns_none() {
        let (_dir, db) = test_conn().await;
        let result = update_fields(
            &db,
            "nope",
            &ScheduledDirectiveFields::default(),
            "admin",
            "2026-01-01T00:00:00Z",
        )
        .await;
        assert_eq!(result.unwrap(), None);
    }

    #[tokio::test]
    async fn update_fields_always_bumps_updated_at_even_with_no_field_changes() {
        let (_dir, db) = test_conn().await;
        seed(&db, "s1", "alice", 3600, "2026-01-01T00:00:00Z", None, None).await;

        let row = update_fields(
            &db,
            "s1",
            &ScheduledDirectiveFields::default(),
            "bob",
            "2026-01-02T00:00:00Z",
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(row.updated_at.as_deref(), Some("2026-01-02T00:00:00Z"));
        assert_eq!(row.updated_by.as_deref(), Some("bob"));
    }

    #[tokio::test]
    async fn update_fields_can_pause_a_schedule() {
        let (_dir, db) = test_conn().await;
        seed(&db, "s1", "alice", 3600, "2026-01-01T00:00:00Z", None, None).await;

        let row = update_fields(
            &db,
            "s1",
            &ScheduledDirectiveFields {
                enabled: Some(false),
                status: Some("paused".to_string()),
                ..Default::default()
            },
            "admin",
            "2026-01-01T00:00:01Z",
        )
        .await
        .unwrap()
        .unwrap();
        assert!(!row.enabled);
        assert_eq!(row.status, "paused");
    }

    #[tokio::test]
    async fn update_fields_nullable_update_can_clear_and_set_until_at() {
        let (_dir, db) = test_conn().await;
        seed(
            &db,
            "s1",
            "alice",
            3600,
            "2026-01-01T00:00:00Z",
            Some("2026-06-01T00:00:00Z"),
            None,
        )
        .await;

        let cleared = update_fields(
            &db,
            "s1",
            &ScheduledDirectiveFields {
                until_at: NullableUpdate::Clear,
                ..Default::default()
            },
            "admin",
            "2026-01-01T00:00:01Z",
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(cleared.until_at, None);

        let set_again = update_fields(
            &db,
            "s1",
            &ScheduledDirectiveFields {
                until_at: NullableUpdate::Set("2026-07-01T00:00:00Z".to_string()),
                ..Default::default()
            },
            "admin",
            "2026-01-01T00:00:02Z",
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(set_again.until_at.as_deref(), Some("2026-07-01T00:00:00Z"));
    }

    #[tokio::test]
    async fn delete_removes_row_and_returns_true() {
        let (_dir, db) = test_conn().await;
        seed(&db, "s1", "alice", 3600, "2026-01-01T00:00:00Z", None, None).await;
        assert!(delete(&db, "s1").await.unwrap());
        assert_eq!(get(&db, "s1").await.unwrap(), None);
    }

    #[tokio::test]
    async fn delete_missing_directive_returns_false() {
        let (_dir, db) = test_conn().await;
        assert!(!delete(&db, "nope").await.unwrap());
    }

    #[tokio::test]
    async fn soonest_due_at_none_when_nothing_fireable() {
        let (_dir, db) = test_conn().await;
        assert_eq!(
            soonest_due_at(&db, "alice", "2026-01-01T00:00:00Z")
                .await
                .unwrap(),
            None
        );
    }

    #[tokio::test]
    async fn soonest_due_at_excludes_windows_already_closed() {
        let (_dir, db) = test_conn().await;
        seed(
            &db,
            "s1",
            "alice",
            3600,
            "2026-06-01T00:00:00Z",
            Some("2026-01-01T00:00:00Z"),
            None,
        )
        .await;
        // until_at already passed relative to "now" -> not fireable.
        assert_eq!(
            soonest_due_at(&db, "alice", "2026-01-02T00:00:00Z")
                .await
                .unwrap(),
            None
        );
    }

    #[tokio::test]
    async fn has_active_reflects_soonest_due_at() {
        let (_dir, db) = test_conn().await;
        assert!(!has_active(&db, "alice", "2026-01-01T00:00:00Z")
            .await
            .unwrap());
        seed(&db, "s1", "alice", 3600, "2026-06-01T00:00:00Z", None, None).await;
        assert!(has_active(&db, "alice", "2026-01-01T00:00:00Z")
            .await
            .unwrap());
    }

    #[tokio::test]
    async fn fire_resets_next_due_from_delivery_time_not_the_old_grid() {
        let (_dir, db) = test_conn().await;
        // Overdue by 5 minutes, 60s interval.
        seed(&db, "s1", "alice", 60, "2026-01-01T00:00:00Z", None, None).await;

        let now = "2026-01-01T00:05:00Z";
        let events = collect_due_and_fire(&db, "alice", now).await.unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].event_type, "directive");
        assert_eq!(events[0].ref_id, "s1");
        assert_eq!(events[0].priority, "urgent");
        assert_eq!(events[0].data.source, "schedule");
        assert_eq!(events[0].data.schedule_id.as_deref(), Some("s1"));

        let row = get(&db, "s1").await.unwrap().unwrap();
        assert_eq!(row.run_count, 1);
        assert_eq!(row.status, "active");
        assert!(row.enabled);
        // next_due_at must be `now + 60s`, NOT the old grid position
        // (`2026-01-01T00:01:00Z`, i.e. old next_due_at + interval).
        assert_eq!(row.next_due_at, "2026-01-01T00:06:00.000000Z");
    }

    #[tokio::test]
    async fn offline_across_many_intervals_fires_exactly_once() {
        let (_dir, db) = test_conn().await;
        // 15-minute interval, overdue by 3 days (288 missed slots).
        seed(&db, "s1", "alice", 900, "2025-12-29T00:00:00Z", None, None).await;

        let now = "2026-01-01T00:00:00Z";
        let events = collect_due_and_fire(&db, "alice", now).await.unwrap();
        assert_eq!(
            events.len(),
            1,
            "must fire exactly once regardless of how many intervals were missed"
        );

        let row = get(&db, "s1").await.unwrap().unwrap();
        assert_eq!(row.run_count, 1);
        assert_eq!(row.next_due_at, "2026-01-01T00:15:00.000000Z");
    }

    #[tokio::test]
    async fn max_runs_end_condition_completes_but_still_fires_the_last_event() {
        let (_dir, db) = test_conn().await;
        seed(
            &db,
            "s1",
            "alice",
            60,
            "2026-01-01T00:00:00Z",
            None,
            Some(1),
        )
        .await;

        let events = collect_due_and_fire(&db, "alice", "2026-01-01T00:00:00Z")
            .await
            .unwrap();
        assert_eq!(events.len(), 1, "the terminal fire still emits an event");

        let row = get(&db, "s1").await.unwrap().unwrap();
        assert_eq!(row.run_count, 1);
        assert_eq!(row.status, "completed");
        assert!(!row.enabled);
        assert!(!has_active(&db, "alice", "2026-01-01T00:00:01Z")
            .await
            .unwrap());
    }

    #[tokio::test]
    async fn until_window_next_fire_beyond_completes() {
        let (_dir, db) = test_conn().await;
        // until_at 30s out, interval 60s -> the computed next-due
        // (now+60) exceeds until_at, so THIS fire is the last one.
        seed(
            &db,
            "s1",
            "alice",
            60,
            "2026-01-01T00:00:00Z",
            Some("2026-01-01T00:00:30Z"),
            None,
        )
        .await;

        let events = collect_due_and_fire(&db, "alice", "2026-01-01T00:00:00Z")
            .await
            .unwrap();
        assert_eq!(events.len(), 1);

        let row = get(&db, "s1").await.unwrap().unwrap();
        assert_eq!(row.status, "completed");
        assert!(!row.enabled);
        assert_eq!(row.run_count, 1);
    }

    #[tokio::test]
    async fn until_already_passed_reaps_without_firing() {
        let (_dir, db) = test_conn().await;
        seed(
            &db,
            "s1",
            "alice",
            60,
            "2025-12-01T00:00:00Z",
            Some("2025-12-31T00:00:00Z"),
            None,
        )
        .await;

        let events = collect_due_and_fire(&db, "alice", "2026-01-01T00:00:00Z")
            .await
            .unwrap();
        assert_eq!(
            events,
            Vec::new(),
            "a closed window must be reaped, not fired"
        );

        let row = get(&db, "s1").await.unwrap().unwrap();
        assert_eq!(row.status, "completed");
        assert!(!row.enabled);
        assert_eq!(
            row.run_count, 0,
            "run_count must stay untouched -- it never actually fired"
        );
    }

    #[tokio::test]
    async fn not_yet_due_does_not_fire() {
        let (_dir, db) = test_conn().await;
        seed(&db, "s1", "alice", 60, "2026-06-01T00:00:00Z", None, None).await;

        let events = collect_due_and_fire(&db, "alice", "2026-01-01T00:00:00Z")
            .await
            .unwrap();
        assert_eq!(events, Vec::new());
        assert!(has_active(&db, "alice", "2026-01-01T00:00:00Z")
            .await
            .unwrap());
    }

    #[tokio::test]
    async fn disabled_schedule_never_fires_or_counts() {
        let (_dir, db) = test_conn().await;
        seed(&db, "s1", "alice", 60, "2026-01-01T00:00:00Z", None, None).await;
        update_fields(
            &db,
            "s1",
            &ScheduledDirectiveFields {
                enabled: Some(false),
                status: Some("paused".to_string()),
                ..Default::default()
            },
            "admin",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();

        let events = collect_due_and_fire(&db, "alice", "2026-01-01T00:00:01Z")
            .await
            .unwrap();
        assert_eq!(events, Vec::new());
        assert_eq!(count_active_for_agent(&db, "alice").await.unwrap(), 0);
        assert!(!has_active(&db, "alice", "2026-01-01T00:00:01Z")
            .await
            .unwrap());
    }

    #[tokio::test]
    async fn collect_due_and_fire_is_scoped_per_agent() {
        let (_dir, db) = test_conn().await;
        seed(&db, "s1", "alice", 60, "2026-01-01T00:00:00Z", None, None).await;
        seed(&db, "s2", "bob", 60, "2026-01-01T00:00:00Z", None, None).await;

        let events = collect_due_and_fire(&db, "alice", "2026-01-01T00:00:01Z")
            .await
            .unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].ref_id, "s1");
        // bob's schedule must still be pending, untouched.
        assert_eq!(get(&db, "s2").await.unwrap().unwrap().run_count, 0);
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
