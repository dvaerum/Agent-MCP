//! `AgentRepository` — port of the pure DB-CRUD surface of
//! `agent_mcp/repositories/agent_repository.py`'s `AgentRepository`
//! class.
//!
//! Scope note: this crate ports the SQL/schema surface only. The
//! Python class also owns an in-process cache (`state.active_agents`,
//! `state.agent_working_dirs`) and publishes domain events
//! (`EventBus`) from the same methods — those are composition-layer
//! concerns (per the target architecture, `conexus-db` sits below
//! `conexus-auth`/`conexus-tools`, which is where the actor/cache/
//! event-bus design lives) and are deliberately deferred to the
//! phase that ports them, not bundled in here. Every method here is
//! a pure `&Connection -> Result` function — no hidden global state.
//!
//! `updated_at`/`created_at` are NOT stamped from a hidden wall-clock
//! read inside this crate: callers pass the timestamp string in.
//! This is a deliberate improvement over the Python source (which
//! calls `datetime.now()` inline) — it keeps every method here a
//! pure function of its arguments, so tests never need to mock a
//! clock, and the actual "what clock, what format" policy is owned
//! by exactly one place upstream (Phase D's app layer) rather than
//! scattered across every write method.
//!
//! Phase G (sea-orm migration): [`AgentRepository::query`] was the
//! first method converted (`async`, takes `&sea_orm::
//! DatabaseConnection`) — PR 1/4 of this repository's own conversion
//! sequence. PR 2/4 converts the CRUD-lifecycle writes: `create`/
//! `seed_manager_profile`/`terminate`/`delete`/`rotate_token`/
//! `insert_tombstone`/`review_profile`. Every other method here,
//! including `get_by_token`/`is_live`/`get_by_id`/`update_field`/
//! `reconcile_current_task_on_reassign`/`clear_current_task_for`/
//! `clear_current_task_for_many`/`list_active`/
//! `list_profile_changes_since`, is DELIBERATELY, PERMANENTLY staying
//! rusqlite-only: they're hot-path (every `/mcp`/`/api` request
//! resolves its bearer through `get_by_token`+`is_live`),
//! transaction-bound, or wake-loop-collector-bound — mirroring
//! `resolve_capabilities`/`group_membership_repository`'s own
//! established "one sync implementation, never duplicated across a
//! sync/async split" precedent (PR #990). See
//! `conexus_backend::principal_resolve`/`conexus_auth::
//! wake_loop_eligibility`/`conexus_wakeloop::event_feed`'s own doc
//! comments for the specific reasoning per call site — those already
//! say "AgentRepository is not a Phase G target," which remains true
//! for the methods they call; it was never true for the whole
//! repository.

use crate::pagination_cache::StableOrderCache;
use regex::Regex;
use rusqlite::{Connection, OptionalExtension, Result, Row};
use sea_orm::{
    ActiveValue::Set, ColumnTrait, ConnectionTrait, DatabaseConnection, DbErr, EntityTrait,
    Order as SeaOrmOrder, PaginatorTrait, QueryFilter, QueryOrder, QuerySelect, Statement,
};
use std::collections::HashMap;
use std::sync::LazyLock;

use crate::entity::agent;

/// The `agent_id` shape Python's `_AGENT_ID_RE` enforces, ported
/// verbatim rather than hand-rolled: `^[a-z][a-z0-9@_-]*[a-z0-9]$|
/// ^[a-z]$` — lowercase, starts with a letter, ends with a
/// letter/digit, `@`/`_`/`-` only in the interior.
static AGENT_ID_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[a-z][a-z0-9@_-]*[a-z0-9]$|^[a-z]$").expect("static regex is valid")
});

fn is_valid_agent_id(agent_id: &str) -> bool {
    AGENT_ID_RE.is_match(agent_id)
}

/// Reserved `agent_id` prefix Python's `create()` rejects
/// synchronously (before any DB write) — `admin*` is reserved for the
/// operator/dashboard identity space.
const RESERVED_AGENT_ID_PREFIX: &str = "admin";

/// One row of the `agents` table, matching the ORM model
/// (`agent_mcp/db/models/agent.py`) column-for-column.
#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct AgentRow {
    pub token: String,
    pub agent_id: String,
    pub created_at: String,
    pub status: String,
    pub current_task: Option<String>,
    pub working_directory: String,
    pub color: Option<String>,
    pub terminated_at: Option<String>,
    pub updated_at: Option<String>,
    pub aoe_session_id: Option<String>,
    pub auto_event_loop: bool,
    pub last_event_seen_at: Option<String>,
    pub last_activity_at: Option<String>,
    pub agent_role: String,
    pub profile: Option<String>,
    pub profile_updated_at: Option<String>,
    pub profile_reviewed_at: Option<String>,
    pub profile_updated_by: Option<String>,
}

fn row_to_agent(row: &Row) -> rusqlite::Result<AgentRow> {
    Ok(AgentRow {
        token: row.get("token")?,
        agent_id: row.get("agent_id")?,
        created_at: row.get("created_at")?,
        status: row.get("status")?,
        current_task: row.get("current_task")?,
        working_directory: row.get("working_directory")?,
        color: row.get("color")?,
        terminated_at: row.get("terminated_at")?,
        updated_at: row.get("updated_at")?,
        aoe_session_id: row.get("aoe_session_id")?,
        auto_event_loop: row.get("auto_event_loop")?,
        last_event_seen_at: row.get("last_event_seen_at")?,
        last_activity_at: row.get("last_activity_at")?,
        agent_role: row.get("agent_role")?,
        profile: row.get("profile")?,
        profile_updated_at: row.get("profile_updated_at")?,
        profile_reviewed_at: row.get("profile_reviewed_at")?,
        profile_updated_by: row.get("profile_updated_by")?,
    })
}

const AGENT_COLUMNS: &str = "token, agent_id, created_at, status, current_task, working_directory, \
     color, terminated_at, updated_at, aoe_session_id, auto_event_loop, last_event_seen_at, \
     last_activity_at, agent_role, profile, profile_updated_at, profile_reviewed_at, profile_updated_by";

/// `agent::Model` -> `AgentRow`, used by [`AgentRepository::query`]'s
/// sea-orm read path. A field-for-field move, not a lossy translation
/// — `entity::agent::Model`'s columns match `AgentRow`'s one-for-one
/// by construction (see that Entity's own module doc).
fn agent_row_from_model(m: agent::Model) -> AgentRow {
    AgentRow {
        token: m.token,
        agent_id: m.agent_id,
        created_at: m.created_at,
        status: m.status,
        current_task: m.current_task,
        working_directory: m.working_directory,
        color: m.color,
        terminated_at: m.terminated_at,
        updated_at: m.updated_at,
        aoe_session_id: m.aoe_session_id,
        auto_event_loop: m.auto_event_loop,
        last_event_seen_at: m.last_event_seen_at,
        last_activity_at: m.last_activity_at,
        agent_role: m.agent_role,
        profile: m.profile,
        profile_updated_at: m.profile_updated_at,
        profile_reviewed_at: m.profile_reviewed_at,
        profile_updated_by: m.profile_updated_by,
    }
}

/// Read one row by `agent_id` through the sea-orm `Entity` — the
/// re-read-after-write primitive [`AgentRepository::create`]/
/// [`AgentRepository::review_profile`] need for their own return
/// value, since [`AgentRepository::get_by_id`] stays rusqlite-only
/// (see this module's own doc). Not `pub`: every async reader outside
/// this file already has [`AgentRepository::query`]; this is purely
/// internal plumbing for the CRUD-lifecycle writes converted in PR
/// 2/4.
async fn find_agent_row(
    db: &DatabaseConnection,
    agent_id: &str,
) -> std::result::Result<Option<AgentRow>, DbErr> {
    Ok(agent::Entity::find()
        .filter(agent::Column::AgentId.eq(agent_id))
        .one(db)
        .await?
        .map(agent_row_from_model))
}

/// One row of the `wait_for_events` peer-profile-change catch-up feed —
/// see [`AgentRepository::list_profile_changes_since`].
#[derive(Debug, Clone, PartialEq)]
pub struct ProfileChangeRow {
    pub agent_id: String,
    pub agent_role: String,
    pub profile: Option<String>,
    pub profile_updated_at: String,
    pub profile_updated_by: Option<String>,
}

/// The `NOT IN (...)` fragment excluding every terminal status from
/// an "active"/"live" agent view. Ported from Python's
/// `TERMINAL_AGENT_STATUSES`/`LIVE_AGENT_SQL` — kept as one constant
/// used everywhere this predicate is needed, for the same reason the
/// Python source gives: a weaker `status != 'terminated'` check
/// drifting from this strict `NOT IN (...)` check once let tombstone
/// rows leak into a listing (the bug this constant exists to make
/// unrepresentable).
const NOT_TERMINAL_SQL: &str = "status NOT IN ('terminated', 'tombstone')";

/// Fields `update_field` is allowed to write. A closed enum — unlike
/// Python's runtime string-allowlist check, an off-allowlist field
/// (`token`, `agent_id`, `created_at`, or a typo) is a compile error
/// here, not a call that silently returns `None`. `token` is
/// deliberately excluded (it has its own `rotate_token` path);
/// `agent_id`/`created_at` are immutable identity/audit fields.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AgentField {
    Status,
    CurrentTask,
    WorkingDirectory,
    Color,
    TerminatedAt,
    AutoEventLoop,
    LastActivityAt,
    LastEventSeenAt,
    AgentRole,
    /// The AoE notification-stream session id. Added for
    /// `edit_agent`/`admin_tools.py`'s `EDITABLE_AGENT_FIELDS` (Phase
    /// D5) -- no prior Rust tool needed to write this column.
    AoeSessionId,
}

impl AgentField {
    fn column(self) -> &'static str {
        match self {
            AgentField::Status => "status",
            AgentField::CurrentTask => "current_task",
            AgentField::WorkingDirectory => "working_directory",
            AgentField::Color => "color",
            AgentField::TerminatedAt => "terminated_at",
            AgentField::AutoEventLoop => "auto_event_loop",
            AgentField::LastActivityAt => "last_activity_at",
            AgentField::LastEventSeenAt => "last_event_seen_at",
            AgentField::AgentRole => "agent_role",
            AgentField::AoeSessionId => "aoe_session_id",
        }
    }
}

/// A value being written via `update_field`. `Text`/`OptionalText`
/// bind directly; `Bool` coerces to SQLite's `0`/`1` the way Python's
/// `_sanitise_field()` coerces `auto_event_loop` explicitly rather
/// than relying on truthy/falsy passthrough.
#[derive(Debug, Clone)]
pub enum FieldValue {
    Text(String),
    OptionalText(Option<String>),
    Bool(bool),
}

/// Parameters for `AgentRepository::create`.
pub struct NewAgent<'a> {
    pub token: &'a str,
    pub agent_id: &'a str,
    pub created_at: &'a str,
    pub status: &'a str,
    pub current_task: Option<&'a str>,
    pub working_directory: &'a str,
    pub color: Option<&'a str>,
    pub agent_role: &'a str,
}

/// Failure modes of `create()`. Mirrors Python's split: identity
/// validation is synchronous and happens before any write
/// (`InvalidAgentId`); a DB-level conflict (duplicate `agent_id` or
/// `token`) is a distinct variant here rather than an opaque
/// propagated error, so callers can map it to a real `Conflict`
/// (matching `conexus-core`'s `ToolResult`) without string-sniffing
/// a SQLite error message.
///
/// Phase G (sea-orm migration): `Conflict`/`Db` carry `DbErr` now
/// that [`AgentRepository::create`] is sea-orm-backed — no rusqlite
/// variant remains, there is no sync twin of `create`.
#[derive(Debug)]
pub enum CreateAgentError {
    InvalidAgentId(String),
    Conflict(DbErr),
    Db(DbErr),
}

impl std::fmt::Display for CreateAgentError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CreateAgentError::InvalidAgentId(id) => {
                write!(f, "invalid agent_id: {id:?}")
            }
            CreateAgentError::Conflict(e) => write!(f, "agent already exists: {e}"),
            CreateAgentError::Db(e) => write!(f, "database error: {e}"),
        }
    }
}

impl std::error::Error for CreateAgentError {}

/// Classifies a failed INSERT as a UNIQUE(`agent_id`)/PRIMARY
/// KEY(`token`) collision — same `DbErr::sql_err()` idiom
/// `group_membership_repository::is_unique_violation` already
/// establishes for sea-orm's own error shape.
fn is_unique_violation(err: &DbErr) -> bool {
    matches!(
        err.sql_err(),
        Some(sea_orm::SqlErr::UniqueConstraintViolation(_))
    )
}

/// Pure DB-CRUD surface for the `agents` table. Every method takes
/// the connection it should run against, matching the Python source's
/// `connection=` seam (this Rust port has ONLY that seam: there is no
/// separate "standalone, opens its own connection" path, since owning
/// a connection pool is an app-layer concern, not a repository
/// concern). The one exception is [`Self::query`]'s pagination
/// anchor: it's real, deliberate cross-call state (see
/// [`pagination_cache`](crate::pagination_cache)'s docs for why it
/// can't be a pure function of its arguments), held as an explicit
/// instance field the caller owns — matching Python's
/// `_pagination_cache` class attribute in spirit (one cache per
/// repository), but never a hidden global static.
#[derive(Default)]
pub struct AgentRepository {
    pagination_cache: StableOrderCache<AgentQueryCacheKey, String>,
}

impl AgentRepository {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn get_by_id(conn: &Connection, agent_id: &str) -> Result<Option<AgentRow>> {
        conn.query_row(
            &format!("SELECT {AGENT_COLUMNS} FROM agents WHERE agent_id = ?1"),
            [agent_id],
            row_to_agent,
        )
        .optional()
    }

    pub fn get_by_token(conn: &Connection, token: &str) -> Result<Option<AgentRow>> {
        conn.query_row(
            &format!("SELECT {AGENT_COLUMNS} FROM agents WHERE token = ?1"),
            [token],
            row_to_agent,
        )
        .optional()
    }

    /// Excludes every [`TERMINAL_AGENT_STATUSES`] row — matches
    /// Python's `list_active()` (BL-R31-3: tombstones must never
    /// leak into an active listing).
    pub fn list_active(conn: &Connection) -> Result<Vec<AgentRow>> {
        let mut stmt = conn.prepare(&format!(
            "SELECT {AGENT_COLUMNS} FROM agents WHERE {NOT_TERMINAL_SQL}"
        ))?;
        let rows = stmt.query_map([], row_to_agent)?;
        rows.collect()
    }

    /// EVERY row (no status filter at all -- including terminated and
    /// tombstone), newest-created first, capped at `limit`. Matches
    /// `GET /api/all-data`'s real query (`SELECT * FROM agents ORDER
    /// BY created_at DESC LIMIT ?`) exactly: that endpoint filters out
    /// the admin/tombstone rows itself, in the caller, not here --
    /// this is a faithful bounded-read primitive, not a second
    /// `list_active`.
    pub fn list_all_bounded(conn: &Connection, limit: i64) -> Result<Vec<AgentRow>> {
        let mut stmt = conn.prepare(&format!(
            "SELECT {AGENT_COLUMNS} FROM agents ORDER BY created_at DESC LIMIT ?1"
        ))?;
        let rows = stmt.query_map([limit], row_to_agent)?;
        rows.collect()
    }

    /// `GET /api/agents`'s real query, faithfully -- every status
    /// EXCEPT `tombstone` (a DB-internal FK artefact, never operator-
    /// queryable), optionally narrowed to one exact `status`, newest-
    /// created first, capped at `limit`. A THIRD status-filter shape
    /// alongside `list_active`/`list_all_bounded`: unlike
    /// `list_active`'s multi-status `NOT_TERMINAL_SQL` exclusion, this
    /// endpoint's dashboard listing deliberately INCLUDES `terminated`
    /// rows (an operator still wants to see/restore them) -- only
    /// `tombstone` is excluded. The tombstone exclusion is applied in
    /// SQL, not filtered client-side after `LIMIT`, so a project with
    /// tombstones interleaved near the top of `created_at DESC` still
    /// returns a full `limit` of real rows, matching Python's real
    /// two-branch WHERE clause exactly.
    pub fn list_for_dashboard(
        conn: &Connection,
        status_filter: Option<&str>,
        limit: i64,
    ) -> Result<Vec<AgentRow>> {
        match status_filter {
            None => {
                let mut stmt = conn.prepare(&format!(
                    "SELECT {AGENT_COLUMNS} FROM agents WHERE status != 'tombstone' \
                     ORDER BY created_at DESC LIMIT ?1"
                ))?;
                let rows = stmt.query_map([limit], row_to_agent)?;
                rows.collect()
            }
            Some(status) => {
                let mut stmt = conn.prepare(&format!(
                    "SELECT {AGENT_COLUMNS} FROM agents WHERE status = ?1 AND status != 'tombstone' \
                     ORDER BY created_at DESC LIMIT ?2"
                ))?;
                let rows = stmt.query_map(rusqlite::params![status, limit], row_to_agent)?;
                rows.collect()
            }
        }
    }

    /// True iff a live (non-terminated, non-tombstone) agent row exists
    /// for `agent_id`. Reuses [`NOT_TERMINAL_SQL`] so this predicate can
    /// never drift from the other converged "live agent" sites
    /// (`list_active`, `count_active_by_status`) -- matches Python's
    /// `agent_repository.is_live_agent`, needed by `task_tools.py`'s
    /// assignment-target validation (a task pinned on a terminated
    /// agent, or a `[deleted-<id>]` tombstone, is unreachable work).
    pub fn is_live(conn: &Connection, agent_id: &str) -> Result<bool> {
        conn.query_row(
            &format!("SELECT 1 FROM agents WHERE agent_id = ?1 AND {NOT_TERMINAL_SQL}"),
            [agent_id],
            |_| Ok(()),
        )
        .optional()
        .map(|row| row.is_some())
    }

    pub fn count_active_by_status(conn: &Connection) -> Result<HashMap<String, i64>> {
        let mut stmt = conn.prepare(&format!(
            "SELECT status, COUNT(token) FROM agents WHERE {NOT_TERMINAL_SQL} GROUP BY status"
        ))?;
        let rows = stmt.query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
        })?;
        rows.collect()
    }

    /// Validates `agent_id` synchronously (matching Python: no write
    /// happens for an invalid/reserved id). A duplicate `agent_id` or
    /// `token` surfaces as [`CreateAgentError::Conflict`].
    ///
    /// Phase G (sea-orm migration): sea-orm-backed (`async`, `db: &
    /// sea_orm::DatabaseConnection`) — see this module's own doc.
    /// Built via `Entity::insert`, same idiom `task_repository::
    /// create`/`group_membership_repository::create_group` already
    /// establish; re-reads the inserted row via [`find_agent_row`]
    /// ("async calls async") rather than [`Self::get_by_id`].
    pub async fn create(
        db: &DatabaseConnection,
        new_agent: NewAgent<'_>,
    ) -> std::result::Result<AgentRow, CreateAgentError> {
        if !is_valid_agent_id(new_agent.agent_id)
            || new_agent.agent_id.starts_with(RESERVED_AGENT_ID_PREFIX)
        {
            return Err(CreateAgentError::InvalidAgentId(
                new_agent.agent_id.to_string(),
            ));
        }

        let am = agent::ActiveModel {
            token: Set(new_agent.token.to_string()),
            agent_id: Set(new_agent.agent_id.to_string()),
            created_at: Set(new_agent.created_at.to_string()),
            status: Set(new_agent.status.to_string()),
            current_task: Set(new_agent.current_task.map(String::from)),
            working_directory: Set(new_agent.working_directory.to_string()),
            color: Set(new_agent.color.map(String::from)),
            agent_role: Set(new_agent.agent_role.to_string()),
            ..Default::default()
        };

        agent::Entity::insert(am).exec(db).await.map_err(|e| {
            if is_unique_violation(&e) {
                CreateAgentError::Conflict(e)
            } else {
                CreateAgentError::Db(e)
            }
        })?;

        find_agent_row(db, new_agent.agent_id)
            .await
            .map_err(CreateAgentError::Db)?
            .ok_or_else(|| {
                CreateAgentError::Db(DbErr::RecordNotFound(
                    "just-inserted agent row not found".to_string(),
                ))
            })
    }

    /// Seeds a freshly-registered `manager`-role agent's profile with
    /// its default charter, stamping `profile_reviewed_at =
    /// profile_updated_at = seed_ts` so a fresh manager isn't
    /// instantly "stale" -- `profile_updated_by` stays NULL (a seed,
    /// not an editor, so it never fires a peer-broadcast). A 3-column
    /// atomic UPDATE, not `update_field` (whose one-column-at-a-time
    /// API can't express this in a single statement, and whose
    /// `AgentField` enum doesn't cover these profile columns at all --
    /// matches Python's own choice of a raw SQL UPDATE here instead of
    /// its usual per-field repo helper).
    ///
    /// Phase G (sea-orm migration): sea-orm-backed (`async`, `db: &
    /// sea_orm::DatabaseConnection`) — see this module's own doc.
    /// Built via `Entity::update_many` + `col_expr` (the same
    /// multi-column typed-UPDATE idiom `claude_code_session_repository::
    /// update_activity` already establishes), not a raw SQL string:
    /// every column touched here is expressible through
    /// `ColumnTrait`/`col_expr`.
    pub async fn seed_manager_profile(
        db: &DatabaseConnection,
        agent_id: &str,
        profile: &str,
        seed_ts: &str,
    ) -> std::result::Result<(), DbErr> {
        agent::Entity::update_many()
            .col_expr(
                agent::Column::Profile,
                sea_orm::sea_query::Expr::value(profile),
            )
            .col_expr(
                agent::Column::ProfileUpdatedAt,
                sea_orm::sea_query::Expr::value(seed_ts),
            )
            .col_expr(
                agent::Column::ProfileReviewedAt,
                sea_orm::sea_query::Expr::value(seed_ts),
            )
            .col_expr(
                agent::Column::ProfileUpdatedBy,
                sea_orm::sea_query::Expr::value(None::<String>),
            )
            .filter(agent::Column::AgentId.eq(agent_id))
            .exec(db)
            .await?;
        Ok(())
    }

    /// `None` means "no agent with that `agent_id`" — the only
    /// failure mode left once [`AgentField`]'s closed enum rules out
    /// an off-allowlist field at compile time. Always bumps
    /// `updated_at` to `now`, matching Python.
    pub fn update_field(
        conn: &Connection,
        agent_id: &str,
        field: AgentField,
        new_value: FieldValue,
        now: &str,
    ) -> Result<Option<AgentRow>> {
        let column = field.column();
        let sql = format!("UPDATE agents SET {column} = ?1, updated_at = ?2 WHERE agent_id = ?3");
        let changed = match new_value {
            FieldValue::Text(v) => conn.execute(&sql, (v, now, agent_id))?,
            FieldValue::OptionalText(v) => conn.execute(&sql, (v, now, agent_id))?,
            FieldValue::Bool(v) => conn.execute(&sql, (v, now, agent_id))?,
        };
        if changed == 0 {
            return Ok(None);
        }
        Self::get_by_id(conn, agent_id)
    }

    /// `false` iff no row matched — either the agent doesn't exist,
    /// or it was already terminal (`terminated`/`tombstone`), which
    /// Python excludes explicitly so a second `terminate()` call is a
    /// no-op rather than re-stamping `terminated_at`.
    ///
    /// Phase G (sea-orm migration): sea-orm-backed (`async`, `db: &
    /// sea_orm::DatabaseConnection`) — see this module's own doc. The
    /// exclusion mirrors [`NOT_TERMINAL_SQL`] (kept as a raw SQL
    /// fragment for the untouched sync methods that still use it) via
    /// `Column::Status.is_not_in([...])` instead.
    pub async fn terminate(
        db: &DatabaseConnection,
        agent_id: &str,
        now: &str,
    ) -> std::result::Result<bool, DbErr> {
        let result = agent::Entity::update_many()
            .col_expr(
                agent::Column::Status,
                sea_orm::sea_query::Expr::value("terminated"),
            )
            .col_expr(
                agent::Column::TerminatedAt,
                sea_orm::sea_query::Expr::value(now),
            )
            .col_expr(
                agent::Column::UpdatedAt,
                sea_orm::sea_query::Expr::value(now),
            )
            .col_expr(
                agent::Column::CurrentTask,
                sea_orm::sea_query::Expr::value(None::<String>),
            )
            .filter(agent::Column::AgentId.eq(agent_id))
            .filter(agent::Column::Status.is_not_in(["terminated", "tombstone"]))
            .exec(db)
            .await?;
        Ok(result.rows_affected > 0)
    }

    /// Hard delete — distinct from `terminate()`'s soft delete.
    ///
    /// Phase G (sea-orm migration): sea-orm-backed (`async`, `db: &
    /// sea_orm::DatabaseConnection`) — see this module's own doc.
    /// Built via `Entity::delete_many` (keyed on `agent_id`, not
    /// `Entity::delete_by_id`, since the Entity's primary key is
    /// `token` — see `entity::agent`'s own doc), same idiom
    /// `claude_code_session_repository::delete_by_agent_id` already
    /// establishes.
    pub async fn delete(
        db: &DatabaseConnection,
        agent_id: &str,
    ) -> std::result::Result<bool, DbErr> {
        let result = agent::Entity::delete_many()
            .filter(agent::Column::AgentId.eq(agent_id))
            .exec(db)
            .await?;
        Ok(result.rows_affected > 0)
    }

    /// The one write path for the auth secret (`token` is off
    /// `update_field`'s allowlist by design). `false` iff no agent
    /// with that `agent_id` exists.
    ///
    /// Phase G (sea-orm migration): sea-orm-backed (`async`, `db: &
    /// sea_orm::DatabaseConnection`) — see this module's own doc.
    /// Writes the primary-key column (`token`) via `col_expr` on an
    /// `update_many` filtered by `agent_id`, not `ActiveModel::
    /// update` (which would require already knowing the row's CURRENT
    /// primary key to address it, the exact value this call is
    /// replacing).
    pub async fn rotate_token(
        db: &DatabaseConnection,
        agent_id: &str,
        new_token: &str,
        now: &str,
    ) -> std::result::Result<bool, DbErr> {
        let result = agent::Entity::update_many()
            .col_expr(
                agent::Column::Token,
                sea_orm::sea_query::Expr::value(new_token),
            )
            .col_expr(
                agent::Column::UpdatedAt,
                sea_orm::sea_query::Expr::value(now),
            )
            .filter(agent::Column::AgentId.eq(agent_id))
            .exec(db)
            .await?;
        Ok(result.rows_affected > 0)
    }

    /// Monotonically advance `last_event_seen_at`; never regresses.
    /// Returns `true` only on a REAL advance (agent exists AND
    /// `cursor_value` sorts after the current value) — the caller
    /// uses this to decide whether a re-wake notification is
    /// warranted, so a no-op write must report `false`, not just "the
    /// UPDATE touched a row". ISO-8601 timestamps sort correctly as
    /// plain strings, matching Python's `MAX(COALESCE(x,''), ?)`
    /// pattern (the `COALESCE` guards the first-ever write, where the
    /// column is still `NULL`; `''` sorts before any real timestamp).
    pub fn advance_event_cursor(
        conn: &Connection,
        agent_id: &str,
        cursor_value: &str,
        now: &str,
    ) -> Result<bool> {
        let current: Option<Option<String>> = conn
            .query_row(
                "SELECT last_event_seen_at FROM agents WHERE agent_id = ?1",
                [agent_id],
                |row| row.get(0),
            )
            .optional()?;
        let Some(current) = current else {
            return Ok(false); // no such agent
        };
        if cursor_value <= current.unwrap_or_default().as_str() {
            return Ok(false); // not an advance
        }
        let changed = conn.execute(
            "UPDATE agents SET last_event_seen_at = ?1, updated_at = ?2 WHERE agent_id = ?3",
            (cursor_value, now, agent_id),
        )?;
        Ok(changed > 0)
    }

    /// Always stamps `profile_reviewed_at`. Only writes `profile`/
    /// `profile_updated_at`/`profile_updated_by` when the content
    /// actually changed (SHA-256 comparison, matching Python) — a
    /// reviewer re-approving an unchanged profile shouldn't churn its
    /// update-audit trail. Returns `None` if the agent doesn't exist.
    ///
    /// Phase G (sea-orm migration): sea-orm-backed (`async`, `db: &
    /// sea_orm::DatabaseConnection`) — see this module's own doc. Both
    /// the existence check and the final re-read go through
    /// [`find_agent_row`] ("async calls async") rather than
    /// [`Self::get_by_id`].
    pub async fn review_profile(
        db: &DatabaseConnection,
        agent_id: &str,
        new_profile: Option<&str>,
        editor_id: Option<&str>,
        now: &str,
    ) -> std::result::Result<Option<ReviewProfileResult>, DbErr> {
        use sha2::{Digest, Sha256};

        let Some(existing) = find_agent_row(db, agent_id).await? else {
            return Ok(None);
        };

        let hash =
            |s: Option<&str>| -> Vec<u8> { Sha256::digest(s.unwrap_or("").as_bytes()).to_vec() };
        let changed = hash(new_profile) != hash(existing.profile.as_deref());

        if changed {
            agent::Entity::update_many()
                .col_expr(
                    agent::Column::Profile,
                    sea_orm::sea_query::Expr::value(new_profile),
                )
                .col_expr(
                    agent::Column::ProfileUpdatedAt,
                    sea_orm::sea_query::Expr::value(now),
                )
                .col_expr(
                    agent::Column::ProfileUpdatedBy,
                    sea_orm::sea_query::Expr::value(editor_id),
                )
                .col_expr(
                    agent::Column::ProfileReviewedAt,
                    sea_orm::sea_query::Expr::value(now),
                )
                .col_expr(
                    agent::Column::UpdatedAt,
                    sea_orm::sea_query::Expr::value(now),
                )
                .filter(agent::Column::AgentId.eq(agent_id))
                .exec(db)
                .await?;
        } else {
            agent::Entity::update_many()
                .col_expr(
                    agent::Column::ProfileReviewedAt,
                    sea_orm::sea_query::Expr::value(now),
                )
                .col_expr(
                    agent::Column::UpdatedAt,
                    sea_orm::sea_query::Expr::value(now),
                )
                .filter(agent::Column::AgentId.eq(agent_id))
                .exec(db)
                .await?;
        }

        let agent = find_agent_row(db, agent_id)
            .await?
            .expect("row existed a moment ago under the same connection");
        Ok(Some(ReviewProfileResult { agent, changed }))
    }

    /// Bulk-clear `current_task` for every agent pointing at a
    /// completed/deleted task. Returns the number of agents cleared.
    pub fn clear_current_task_for(conn: &Connection, task_id: &str, now: &str) -> Result<i64> {
        let changed = conn.execute(
            "UPDATE agents SET current_task = NULL, updated_at = ?1 WHERE current_task = ?2",
            (now, task_id),
        )?;
        Ok(changed as i64)
    }

    /// Set-valued sibling of [`Self::clear_current_task_for`] — one
    /// `IN (...)` UPDATE for cascade deletes instead of N single
    /// UPDATEs. A no-op (returns `Ok(0)`, no query executed) for an
    /// empty slice.
    pub fn clear_current_task_for_many(
        conn: &Connection,
        task_ids: &[&str],
        now: &str,
    ) -> Result<i64> {
        if task_ids.is_empty() {
            return Ok(0);
        }
        let placeholders = std::iter::repeat_n("?", task_ids.len())
            .collect::<Vec<_>>()
            .join(", ");
        let sql = format!("UPDATE agents SET current_task = NULL, updated_at = ? WHERE current_task IN ({placeholders})");
        let mut params: Vec<&dyn rusqlite::ToSql> = vec![&now];
        params.extend(task_ids.iter().map(|id| id as &dyn rusqlite::ToSql));
        let changed = conn.execute(&sql, params.as_slice())?;
        Ok(changed as i64)
    }

    /// Best-effort reconciliation run as a side effect of reassigning
    /// `task_id` from `prior_assignee` to `new_assignee`: clears the
    /// loser's stale pointer, and sets the gainer's pointer only if it
    /// was `NULL` (never clobbers a gainer who's independently mid-way
    /// through some other task). Unlike Python's version, real DB
    /// errors here are NOT swallowed — this crate's whole design is
    /// "no hidden behavior behind a `&Connection -> Result` seam"; a
    /// caller that wants best-effort/log-and-continue semantics can
    /// still choose to ignore the `Err`, but silently eating it here
    /// would hide it from every caller forever, including ones that
    /// legitimately want to know.
    pub fn reconcile_current_task_on_reassign(
        conn: &Connection,
        task_id: &str,
        prior_assignee: Option<&str>,
        new_assignee: Option<&str>,
        now: &str,
    ) -> Result<()> {
        if let Some(prior) = prior_assignee {
            conn.execute(
                "UPDATE agents SET current_task = NULL, updated_at = ?1 WHERE agent_id = ?2 AND current_task = ?3",
                (now, prior, task_id),
            )?;
        }
        if let Some(new) = new_assignee {
            conn.execute(
                "UPDATE agents SET current_task = ?1, updated_at = ?2 WHERE agent_id = ?3 AND current_task IS NULL",
                (task_id, now, new),
            )?;
        }
        Ok(())
    }

    /// `INSERT OR IGNORE` a synthetic tombstone row so a purged
    /// agent's `token`/`agent_id` still satisfies the FK from
    /// `agent_messages`. Idempotent by construction — re-purging the
    /// same id is a no-op, not a conflict.
    ///
    /// Phase G (sea-orm migration): sea-orm-backed (`async`, `db: &
    /// sea_orm::DatabaseConnection`) — see this module's own doc.
    /// SQLite's `INSERT OR IGNORE` swallows a violation of ANY unique
    /// constraint on the row (both the `token` primary key and the
    /// `agent_id` unique index), which sea-orm's typed
    /// `.on_conflict()` builder can't express (it targets exactly one
    /// named conflict column/index) — this crate's established raw-
    /// SQL escape hatch (`Statement::from_sql_and_values`, same idiom
    /// `agent_action_repository::list_recent`/
    /// `group_membership_repository::ensure_group` already use) is
    /// the correct tool here, not a bent typed-builder call.
    pub async fn insert_tombstone(
        db: &DatabaseConnection,
        token: &str,
        tombstone_agent_id: &str,
        now: &str,
    ) -> std::result::Result<(), DbErr> {
        let backend = db.get_database_backend();
        let stmt = Statement::from_sql_and_values(
            backend,
            "INSERT OR IGNORE INTO agents (token, agent_id, created_at, status, working_directory, color, updated_at) \
             VALUES (?, ?, ?, 'tombstone', '', '#000000', ?)",
            [token.into(), tombstone_agent_id.into(), now.into(), now.into()],
        );
        db.execute_raw(stmt).await?;
        Ok(())
    }

    /// Filtered, sorted, paginated agent listing backing `view_agents`.
    /// `tombstone` rows are excluded UNCONDITIONALLY, before any
    /// caller-supplied filter (BL-R31-3) — including a caller-supplied
    /// `status: "tombstone"` filter, which becomes self-contradictory
    /// against that unconditional exclusion and so always yields
    /// `(vec![], 0)`. This is deliberately not special-cased: it's the
    /// same emergent behavior Python gets from ANDing both `status`
    /// predicates, preserved by construction rather than by an
    /// explicit early return.
    ///
    /// Pagination is "stable": the ordering for `offset == 0` is
    /// anchored via [`Self::pagination_cache`], and every later
    /// `offset > 0` call in the same sweep replays that SAME ordering
    /// rather than re-deriving it — so a status change or an
    /// insertion elsewhere in the table between page requests can't
    /// shift a still-matching row out of the sweep. `total` is NOT a
    /// fresh `COUNT(*)`: it's the anchored id list reconciled against
    /// rows that still exist right now, so a HARD DELETE of an
    /// already-anchored (but not yet delivered) row is reflected in
    /// `total` on every subsequent page, while the deleted row's
    /// "slot" in the window is simply dropped, never backfilled by
    /// promoting a later-ranked row (that would require re-deriving
    /// the order, which anchoring exists specifically to avoid).
    ///
    /// Diverges from Python in one place: real DB errors propagate as
    /// `Err`, they are not swallowed into `(vec![], 0)` — consistent
    /// with every other method in this crate.
    ///
    /// Phase G: sea-orm-backed (`async`, `db: &sea_orm::
    /// DatabaseConnection`) — see this module's own doc for why this
    /// is the one method of `AgentRepository` that converts. The
    /// filter/sort/pagination semantics above are unchanged; only the
    /// query-building mechanism is (sea-orm's typed builder, not raw
    /// SQL strings).
    pub async fn query(
        &self,
        db: &DatabaseConnection,
        filters: AgentQueryFilters<'_>,
    ) -> std::result::Result<(Vec<AgentRow>, i64), DbErr> {
        // Never "0 rows" or "before the start" — matches Python's
        // clamp exactly (a limit of 0 is not "everything", it's 1).
        let limit = filters.limit.max(1);
        let offset = filters.offset.max(0);

        let status = filters.status.map(String::from);
        let pattern = filters.agent_id_pattern.map(String::from);
        let include_terminated = filters.include_terminated;
        let created_after = filters.created_after.map(String::from);
        let created_before = filters.created_before.map(String::from);
        let sort_by = filters.sort_by;
        let sort_order = filters.sort_order;

        let cache_key = AgentQueryCacheKey {
            status: status.clone(),
            agent_id_pattern: pattern.clone(),
            include_terminated,
            created_after: created_after.clone(),
            created_before: created_before.clone(),
            sort_by,
            sort_order,
        };

        let ordered_ids: Vec<String> = self
            .pagination_cache
            .get_or_anchor_async(cache_key, offset, || {
                Self::compute_ordered_ids(
                    db,
                    status.as_deref(),
                    pattern.as_deref(),
                    include_terminated,
                    created_after.as_deref(),
                    created_before.as_deref(),
                    sort_by,
                    sort_order,
                )
            })
            .await?;

        if ordered_ids.is_empty() {
            return Ok((Vec::new(), 0));
        }

        // total = the anchored ids, reconciled against rows that
        // still exist right now (NOT a fresh unconditional COUNT).
        let total: i64 = agent::Entity::find()
            .filter(agent::Column::AgentId.is_in(ordered_ids.iter().cloned()))
            .count(db)
            .await? as i64;

        let offset_usize = offset as usize;
        let window_ids: Vec<String> = if offset_usize < ordered_ids.len() {
            ordered_ids[offset_usize..]
                .iter()
                .take(limit as usize)
                .cloned()
                .collect()
        } else {
            Vec::new()
        };

        if window_ids.is_empty() {
            return Ok((Vec::new(), total));
        }

        let rows_by_id: HashMap<String, AgentRow> = agent::Entity::find()
            .filter(agent::Column::AgentId.is_in(window_ids.iter().cloned()))
            .all(db)
            .await?
            .into_iter()
            .map(agent_row_from_model)
            .map(|row| (row.agent_id.clone(), row))
            .collect();

        // Reassemble in window_ids (anchored) order, silently
        // dropping any id that no longer resolves — matches Python's
        // `if aid in rows_by_id` guard exactly.
        let ordered_rows = window_ids
            .into_iter()
            .filter_map(|id| rows_by_id.get(&id).cloned())
            .collect();

        Ok((ordered_rows, total))
    }

    /// Every row, unconditionally — no status filtering at all, unlike
    /// every product-facing listing (which all exclude at least
    /// tombstones). For backup/differential-testing tooling only.
    pub fn dump_all(conn: &Connection) -> Result<Vec<AgentRow>> {
        let mut stmt = conn.prepare(&format!(
            "SELECT {AGENT_COLUMNS} FROM agents ORDER BY agent_id"
        ))?;
        let rows = stmt.query_map([], row_to_agent)?;
        rows.collect()
    }

    /// Peer profile changes newer than `since`, excluding `self_id`'s
    /// OWN edits and its own NULL-editor seed row. Feeds
    /// `conexus-wakeloop::event_feed`'s `agent_profile_updated`
    /// catch-up stream (`_collect_agent_profile_events_for`) — the
    /// `agents` table itself IS the log, so a peer offline across an
    /// edit replays it on reconnect via `profile_updated_at > cursor`.
    ///
    /// Two exclusions baked into SQL (kept in sync with the in-memory
    /// live-push path Python calls `notify_agent_profile_updated`, not
    /// yet ported):
    /// - `profile_updated_by != self_id` — the EDITOR is excluded, not
    ///   the subject, so a manager editing a worker reaches the worker
    ///   but not the manager.
    /// - `NOT (agent_id = self_id AND profile_updated_by IS NULL)` — a
    ///   recipient never gets its own NULL-editor seed (its initial
    ///   charter) echoed back to itself, while another agent's seed
    ///   (a new manager's charter) still surfaces as a roster change.
    ///
    /// Tombstone/terminated/system rows are never a profile source —
    /// note this 3-way exclusion is intentionally WIDER than
    /// `NOT_TERMINAL_SQL` (2-way, no `system`); Python's own live-push
    /// path uses the narrower 2-way set, a real asymmetry in the source
    /// this port preserves rather than reconciles (see the Phase D3
    /// research notes in the migration plan).
    pub fn list_profile_changes_since(
        conn: &Connection,
        since: &str,
        self_id: &str,
    ) -> Result<Vec<ProfileChangeRow>> {
        let mut stmt = conn.prepare(
            "SELECT agent_id, agent_role, profile, profile_updated_at, profile_updated_by \
             FROM agents \
             WHERE profile_updated_at IS NOT NULL \
               AND profile_updated_at > ?1 \
               AND (profile_updated_by IS NULL OR profile_updated_by != ?2) \
               AND NOT (agent_id = ?2 AND profile_updated_by IS NULL) \
               AND status NOT IN ('tombstone', 'terminated', 'system') \
             ORDER BY profile_updated_at ASC",
        )?;
        let rows = stmt.query_map((since, self_id), |row| {
            Ok(ProfileChangeRow {
                agent_id: row.get(0)?,
                agent_role: row.get(1)?,
                profile: row.get(2)?,
                profile_updated_at: row.get(3)?,
                profile_updated_by: row.get(4)?,
            })
        })?;
        rows.collect()
    }

    /// The `agent_id`-only ordered id list [`Self::query`] anchors via
    /// [`StableOrderCache::get_or_anchor_async`]. `tombstone` rows are
    /// excluded unconditionally, before any caller filter — same
    /// BL-R31-3 rule [`Self::query`]'s own doc describes. Built via
    /// sea-orm's typed query builder (`select_only` + `column` +
    /// `order_by`, the same idiom `task_repository::count_by_status`/
    /// `conexus_router::identity::users_table_is_empty_impl` already
    /// establish for a single-column projection) rather than a raw
    /// SQL string — every filter/sort clause here is expressible
    /// through `ColumnTrait`/`QueryOrder`, so there's no need for this
    /// crate's `Statement::from_sql_and_values` raw-SQL escape hatch.
    #[allow(clippy::too_many_arguments)]
    async fn compute_ordered_ids(
        db: &DatabaseConnection,
        status: Option<&str>,
        pattern: Option<&str>,
        include_terminated: bool,
        created_after: Option<&str>,
        created_before: Option<&str>,
        sort_by: AgentSortBy,
        sort_order: SortOrder,
    ) -> std::result::Result<Vec<String>, DbErr> {
        let mut query = agent::Entity::find().filter(agent::Column::Status.ne("tombstone"));

        if let Some(s) = status {
            query = query.filter(agent::Column::Status.eq(s));
        }
        if let Some(p) = pattern {
            query = query.filter(agent::Column::AgentId.like(p));
        }
        if !include_terminated {
            query = query.filter(agent::Column::Status.ne("terminated"));
        }
        if let Some(a) = created_after {
            query = query.filter(agent::Column::CreatedAt.gte(a));
        }
        if let Some(b) = created_before {
            query = query.filter(agent::Column::CreatedAt.lte(b));
        }

        let sort_column = match sort_by {
            AgentSortBy::AgentId => agent::Column::AgentId,
            AgentSortBy::Status => agent::Column::Status,
            AgentSortBy::CreatedAt => agent::Column::CreatedAt,
            AgentSortBy::TerminatedAt => agent::Column::TerminatedAt,
        };
        let order = match sort_order {
            SortOrder::Asc => SeaOrmOrder::Asc,
            SortOrder::Desc => SeaOrmOrder::Desc,
        };

        // Fixed `agent_id ASC` tiebreaker guarantees a fully
        // deterministic total order even when the sort column has
        // duplicate values — essential for offset pagination
        // correctness (two agents created in the same second must
        // still sort identically on every page).
        query
            .select_only()
            .column(agent::Column::AgentId)
            .order_by(sort_column, order)
            .order_by(agent::Column::AgentId, SeaOrmOrder::Asc)
            .into_tuple()
            .all(db)
            .await
    }
}

/// Allowlisted `query()` sort columns. A closed enum — unlike
/// Python's runtime allowlist check (an invalid `sort_by` silently
/// falls back to `created_at`), an unsupported value can't reach this
/// type at all. [`parse_agent_sort_by`] provides Python's exact
/// fallback-on-invalid behavior for callers translating a raw string
/// at the API boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum AgentSortBy {
    AgentId,
    Status,
    CreatedAt,
    TerminatedAt,
}

/// Matches Python's `sort_by` allowlist-with-fallback exactly: any
/// value outside `{agent_id, status, created_at, terminated_at}`
/// (including an empty/garbage string) silently becomes `CreatedAt`,
/// the same default Python falls back to. No error is raised here —
/// deliberately, to stay a faithful boundary-translation helper; a
/// caller wanting to REJECT an invalid value should validate before
/// calling this.
pub fn parse_agent_sort_by(s: &str) -> AgentSortBy {
    match s {
        "agent_id" => AgentSortBy::AgentId,
        "status" => AgentSortBy::Status,
        "terminated_at" => AgentSortBy::TerminatedAt,
        _ => AgentSortBy::CreatedAt,
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum SortOrder {
    Asc,
    Desc,
}

/// Matches Python's `sort_order` allowlist-with-fallback: only an
/// exact (case-insensitive) `"ASC"` becomes [`SortOrder::Asc`];
/// everything else — including `"DESC"` and any garbage value —
/// becomes [`SortOrder::Desc`], the same default Python falls back to.
pub fn parse_sort_order(s: &str) -> SortOrder {
    if s.eq_ignore_ascii_case("ASC") {
        SortOrder::Asc
    } else {
        SortOrder::Desc
    }
}

/// Parameters for [`AgentRepository::query`]. `Default` mirrors
/// Python's own defaults (`include_terminated=True`, `sort_by=
/// created_at`, `sort_order=DESC`, `limit=50`, `offset=0`).
pub struct AgentQueryFilters<'a> {
    pub status: Option<&'a str>,
    pub agent_id_pattern: Option<&'a str>,
    pub include_terminated: bool,
    pub created_after: Option<&'a str>,
    pub created_before: Option<&'a str>,
    pub sort_by: AgentSortBy,
    pub sort_order: SortOrder,
    pub limit: i64,
    pub offset: i64,
}

impl Default for AgentQueryFilters<'_> {
    fn default() -> Self {
        Self {
            status: None,
            agent_id_pattern: None,
            include_terminated: true,
            created_after: None,
            created_before: None,
            sort_by: AgentSortBy::CreatedAt,
            sort_order: SortOrder::Desc,
            limit: 50,
            offset: 0,
        }
    }
}

/// The `StableOrderCache` key: every filter/sort knob that affects
/// the WHERE/ORDER BY — deliberately EXCLUDING `limit`/`offset`, so
/// every page of one sweep shares the same anchor.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct AgentQueryCacheKey {
    status: Option<String>,
    agent_id_pattern: Option<String>,
    include_terminated: bool,
    created_after: Option<String>,
    created_before: Option<String>,
    sort_by: AgentSortBy,
    sort_order: SortOrder,
}

/// Result of [`AgentRepository::review_profile`] — the refreshed row
/// plus whether the profile content actually changed (vs. just being
/// re-stamped as reviewed).
#[derive(Debug, Clone, PartialEq)]
pub struct ReviewProfileResult {
    pub agent: AgentRow,
    pub changed: bool,
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

    /// A file-backed DB opened as BOTH a `rusqlite::Connection` (for
    /// every still-sync method under test) and a sea-orm
    /// `DatabaseConnection` (for `query` from PR 1/4 and the
    /// CRUD-lifecycle writes converted in PR 2/4) -- an in-memory
    /// `:memory:` DB can't be shared across two separate connection
    /// handles the way a real file can.
    async fn test_conn_with_sea_orm() -> (tempfile::TempDir, Connection, DatabaseConnection) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("agent_repository_test.db");
        let conn = Connection::open(&path).unwrap();
        init_schema(&conn).unwrap();
        let db = sea_orm::Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        (dir, conn, db)
    }

    async fn seed(db: &DatabaseConnection, agent_id: &str, token: &str, status: &str) {
        AgentRepository::create(
            db,
            NewAgent {
                token,
                agent_id,
                created_at: "2026-01-01T00:00:00Z",
                status,
                current_task: None,
                working_directory: "/tmp",
                color: None,
                agent_role: "worker",
            },
        )
        .await
        .unwrap();
    }

    #[test]
    fn get_by_id_returns_none_for_unknown_agent() {
        let conn = test_conn();
        assert_eq!(AgentRepository::get_by_id(&conn, "nope").unwrap(), None);
    }

    #[tokio::test]
    async fn create_then_get_by_id_and_get_by_token_round_trip() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "alice", "tok-alice", "active").await;

        let by_id = AgentRepository::get_by_id(&conn, "alice").unwrap().unwrap();
        assert_eq!(by_id.agent_id, "alice");
        assert_eq!(by_id.token, "tok-alice");
        assert_eq!(by_id.status, "active");
        assert!(by_id.auto_event_loop, "DB default must be true");
        assert_eq!(by_id.agent_role, "worker");

        let by_token = AgentRepository::get_by_token(&conn, "tok-alice")
            .unwrap()
            .unwrap();
        assert_eq!(by_token, by_id);
    }

    #[tokio::test]
    async fn create_rejects_invalid_agent_id_before_any_write() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        let err = AgentRepository::create(
            &db,
            NewAgent {
                token: "t1",
                agent_id: "Bad-ID", // uppercase, leading letter but rule violated
                created_at: "2026-01-01T00:00:00Z",
                status: "active",
                current_task: None,
                working_directory: "/tmp",
                color: None,
                agent_role: "worker",
            },
        )
        .await
        .unwrap_err();
        assert!(matches!(err, CreateAgentError::InvalidAgentId(_)));
        assert_eq!(AgentRepository::get_by_token(&conn, "t1").unwrap(), None);
    }

    #[tokio::test]
    async fn create_rejects_reserved_admin_prefix() {
        let (_dir, _conn, db) = test_conn_with_sea_orm().await;
        let err = AgentRepository::create(
            &db,
            NewAgent {
                token: "t1",
                agent_id: "admin-bob",
                created_at: "2026-01-01T00:00:00Z",
                status: "active",
                current_task: None,
                working_directory: "/tmp",
                color: None,
                agent_role: "worker",
            },
        )
        .await
        .unwrap_err();
        assert!(matches!(err, CreateAgentError::InvalidAgentId(_)));
    }

    #[tokio::test]
    async fn seed_manager_profile_sets_all_three_columns_and_leaves_updated_by_null() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "manager-1", "tok-m1", "active").await;
        AgentRepository::seed_manager_profile(
            &db,
            "manager-1",
            "You are a manager.",
            "2026-06-01T00:00:00Z",
        )
        .await
        .unwrap();
        let row = AgentRepository::get_by_id(&conn, "manager-1")
            .unwrap()
            .unwrap();
        assert_eq!(row.profile.as_deref(), Some("You are a manager."));
        assert_eq!(
            row.profile_updated_at.as_deref(),
            Some("2026-06-01T00:00:00Z")
        );
        assert_eq!(
            row.profile_reviewed_at.as_deref(),
            Some("2026-06-01T00:00:00Z")
        );
        assert_eq!(row.profile_updated_by, None);
    }

    #[tokio::test]
    async fn create_accepts_single_character_agent_id() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "a", "tok-a", "active").await;
        assert!(AgentRepository::get_by_id(&conn, "a").unwrap().is_some());
    }

    #[tokio::test]
    async fn create_duplicate_agent_id_is_a_conflict_not_a_generic_db_error() {
        let (_dir, _conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "alice", "tok-alice", "active").await;
        let err = AgentRepository::create(
            &db,
            NewAgent {
                token: "tok-other",
                agent_id: "alice",
                created_at: "2026-01-01T00:00:00Z",
                status: "active",
                current_task: None,
                working_directory: "/tmp",
                color: None,
                agent_role: "worker",
            },
        )
        .await
        .unwrap_err();
        assert!(matches!(err, CreateAgentError::Conflict(_)));
    }

    #[tokio::test]
    async fn list_active_excludes_terminated_and_tombstone() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "live1", "t1", "active").await;
        seed(&db, "dead1", "t2", "terminated").await;
        seed(&db, "tomb1", "t3", "tombstone").await;
        seed(&db, "live2", "t4", "created").await;

        let mut ids: Vec<_> = AgentRepository::list_active(&conn)
            .unwrap()
            .into_iter()
            .map(|a| a.agent_id)
            .collect();
        ids.sort();
        assert_eq!(ids, vec!["live1", "live2"]);
    }

    #[tokio::test]
    async fn list_all_bounded_includes_every_status_and_respects_the_limit() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "live1", "t1", "active").await;
        seed(&db, "dead1", "t2", "terminated").await;
        seed(&db, "tomb1", "t3", "tombstone").await;

        let all = AgentRepository::list_all_bounded(&conn, 10).unwrap();
        let mut ids: Vec<_> = all.iter().map(|a| a.agent_id.as_str()).collect();
        ids.sort();
        assert_eq!(
            ids,
            vec!["dead1", "live1", "tomb1"],
            "unlike list_active, every status must be included"
        );

        let capped = AgentRepository::list_all_bounded(&conn, 2).unwrap();
        assert_eq!(capped.len(), 2);
    }

    #[tokio::test]
    async fn list_for_dashboard_excludes_only_tombstone_and_the_limit_applies_after_that_filter() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "live1", "t1", "active").await;
        seed(&db, "dead1", "t2", "terminated").await;
        seed(&db, "tomb1", "t3", "tombstone").await;

        let unfiltered = AgentRepository::list_for_dashboard(&conn, None, 10).unwrap();
        let mut ids: Vec<_> = unfiltered.iter().map(|a| a.agent_id.as_str()).collect();
        ids.sort();
        assert_eq!(
            ids,
            vec!["dead1", "live1"],
            "terminated stays visible (an operator can still restore it); only tombstone drops"
        );

        // The LIMIT must apply AFTER the tombstone exclusion, in SQL --
        // not a client-side filter over an already-capped read, which
        // could silently return fewer than `limit` real rows if a
        // tombstone sorts ahead of them.
        let capped = AgentRepository::list_for_dashboard(&conn, None, 1).unwrap();
        assert_eq!(capped.len(), 1);

        let status_scoped =
            AgentRepository::list_for_dashboard(&conn, Some("terminated"), 10).unwrap();
        assert_eq!(status_scoped.len(), 1);
        assert_eq!(status_scoped[0].agent_id, "dead1");

        let tombstone_scoped =
            AgentRepository::list_for_dashboard(&conn, Some("tombstone"), 10).unwrap();
        assert!(
            tombstone_scoped.is_empty(),
            "an explicit status=tombstone query must still return nothing"
        );
    }

    #[tokio::test]
    async fn count_active_by_status_excludes_terminal_and_groups_correctly() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "a1", "t1", "active").await;
        seed(&db, "a2", "t2", "active").await;
        seed(&db, "c1", "t3", "created").await;
        seed(&db, "d1", "t4", "terminated").await;

        let counts = AgentRepository::count_active_by_status(&conn).unwrap();
        assert_eq!(counts.get("active"), Some(&2));
        assert_eq!(counts.get("created"), Some(&1));
        assert_eq!(counts.get("terminated"), None);
    }

    #[test]
    fn update_field_unknown_agent_returns_none() {
        let conn = test_conn();
        let result = AgentRepository::update_field(
            &conn,
            "nope",
            AgentField::Status,
            FieldValue::Text("active".into()),
            "2026-01-02T00:00:00Z",
        )
        .unwrap();
        assert_eq!(result, None);
    }

    #[tokio::test]
    async fn update_field_writes_value_and_bumps_updated_at() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "alice", "tok-alice", "created").await;

        let updated = AgentRepository::update_field(
            &conn,
            "alice",
            AgentField::Status,
            FieldValue::Text("active".into()),
            "2026-01-02T00:00:00Z",
        )
        .unwrap()
        .unwrap();
        assert_eq!(updated.status, "active");
        assert_eq!(updated.updated_at.as_deref(), Some("2026-01-02T00:00:00Z"));
    }

    #[tokio::test]
    async fn update_field_auto_event_loop_coerces_bool_to_integer_column() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "alice", "tok-alice", "active").await;

        let updated = AgentRepository::update_field(
            &conn,
            "alice",
            AgentField::AutoEventLoop,
            FieldValue::Bool(false),
            "2026-01-02T00:00:00Z",
        )
        .unwrap()
        .unwrap();
        assert!(!updated.auto_event_loop);
    }

    #[tokio::test]
    async fn terminate_sets_status_and_clears_current_task() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "alice", "tok-alice", "active").await;
        AgentRepository::update_field(
            &conn,
            "alice",
            AgentField::CurrentTask,
            FieldValue::OptionalText(Some("task-1".into())),
            "2026-01-01T00:00:00Z",
        )
        .unwrap();

        assert!(
            AgentRepository::terminate(&db, "alice", "2026-01-03T00:00:00Z")
                .await
                .unwrap()
        );

        let row = AgentRepository::get_by_id(&conn, "alice").unwrap().unwrap();
        assert_eq!(row.status, "terminated");
        assert_eq!(row.terminated_at.as_deref(), Some("2026-01-03T00:00:00Z"));
        assert_eq!(row.current_task, None);
    }

    #[tokio::test]
    async fn terminate_missing_agent_returns_false() {
        let (_dir, _conn, db) = test_conn_with_sea_orm().await;
        assert!(
            !AgentRepository::terminate(&db, "nope", "2026-01-01T00:00:00Z")
                .await
                .unwrap()
        );
    }

    #[tokio::test]
    async fn terminate_refuses_a_tombstone_row() {
        // BL-R31-3b: a tombstone is already a purge artefact; flipping
        // its status to 'terminated' would leak `[deleted-<id>]` into
        // the terminated-agents listing. NOT_TERMINAL_SQL already
        // excludes 'tombstone' from terminate()'s WHERE clause -- this
        // pins that specific case (untested until now).
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        AgentRepository::insert_tombstone(&db, "t1", "[deleted-ghost]", "2026-01-01T00:00:00Z")
            .await
            .unwrap();
        assert!(
            !AgentRepository::terminate(&db, "[deleted-ghost]", "2026-01-02T00:00:00Z")
                .await
                .unwrap()
        );
        let row = AgentRepository::get_by_id(&conn, "[deleted-ghost]")
            .unwrap()
            .unwrap();
        assert_eq!(
            row.status, "tombstone",
            "terminate() mutated a tombstone row's status"
        );
    }

    #[tokio::test]
    async fn terminate_already_terminal_is_a_noop_not_a_re_stamp() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "alice", "tok-alice", "active").await;
        assert!(
            AgentRepository::terminate(&db, "alice", "2026-01-01T00:00:00Z")
                .await
                .unwrap()
        );
        // Second terminate on an already-terminal row must report
        // "no row matched" — matches Python excluding terminal rows
        // from the UPDATE's WHERE clause explicitly.
        assert!(
            !AgentRepository::terminate(&db, "alice", "2026-01-02T00:00:00Z")
                .await
                .unwrap()
        );
        let row = AgentRepository::get_by_id(&conn, "alice").unwrap().unwrap();
        assert_eq!(row.terminated_at.as_deref(), Some("2026-01-01T00:00:00Z"));
    }

    #[tokio::test]
    async fn delete_removes_row_and_returns_true() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "alice", "tok-alice", "active").await;
        assert!(AgentRepository::delete(&db, "alice").await.unwrap());
        assert_eq!(AgentRepository::get_by_id(&conn, "alice").unwrap(), None);
    }

    #[tokio::test]
    async fn delete_missing_agent_returns_false() {
        let (_dir, _conn, db) = test_conn_with_sea_orm().await;
        assert!(!AgentRepository::delete(&db, "nope").await.unwrap());
    }

    #[tokio::test]
    async fn rotate_token_writes_new_token_and_old_token_no_longer_resolves() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "alice", "tok-old", "active").await;
        assert!(
            AgentRepository::rotate_token(&db, "alice", "tok-new", "2026-01-02T00:00:00Z")
                .await
                .unwrap()
        );
        assert_eq!(
            AgentRepository::get_by_token(&conn, "tok-old").unwrap(),
            None
        );
        assert_eq!(
            AgentRepository::get_by_token(&conn, "tok-new")
                .unwrap()
                .unwrap()
                .agent_id,
            "alice"
        );
    }

    #[tokio::test]
    async fn rotate_token_missing_agent_returns_false() {
        let (_dir, _conn, db) = test_conn_with_sea_orm().await;
        assert!(
            !AgentRepository::rotate_token(&db, "nope", "tok-new", "2026-01-01T00:00:00Z")
                .await
                .unwrap()
        );
    }

    #[test]
    fn advance_event_cursor_missing_agent_returns_false() {
        let conn = test_conn();
        assert!(!AgentRepository::advance_event_cursor(
            &conn,
            "nope",
            "cursor-1",
            "2026-01-01T00:00:00Z"
        )
        .unwrap());
    }

    #[tokio::test]
    async fn advance_event_cursor_first_write_advances_from_null() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "alice", "tok-alice", "active").await;
        assert!(AgentRepository::advance_event_cursor(
            &conn,
            "alice",
            "2026-01-01T00:00:01Z",
            "2026-01-01T00:00:01Z"
        )
        .unwrap());
        let row = AgentRepository::get_by_id(&conn, "alice").unwrap().unwrap();
        assert_eq!(
            row.last_event_seen_at.as_deref(),
            Some("2026-01-01T00:00:01Z")
        );
    }

    #[tokio::test]
    async fn advance_event_cursor_never_regresses() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "alice", "tok-alice", "active").await;
        assert!(AgentRepository::advance_event_cursor(
            &conn,
            "alice",
            "2026-01-01T00:00:05Z",
            "2026-01-01T00:00:05Z"
        )
        .unwrap());

        // An older cursor value must not overwrite the newer one, and
        // must report "no advance" so the caller doesn't publish a
        // spurious wake.
        assert!(!AgentRepository::advance_event_cursor(
            &conn,
            "alice",
            "2026-01-01T00:00:02Z",
            "2026-01-01T00:00:06Z"
        )
        .unwrap());
        let row = AgentRepository::get_by_id(&conn, "alice").unwrap().unwrap();
        assert_eq!(
            row.last_event_seen_at.as_deref(),
            Some("2026-01-01T00:00:05Z")
        );
    }

    #[tokio::test]
    async fn review_profile_missing_agent_returns_none() {
        let (_dir, _conn, db) = test_conn_with_sea_orm().await;
        assert_eq!(
            AgentRepository::review_profile(
                &db,
                "nope",
                Some("hi"),
                Some("editor"),
                "2026-01-01T00:00:00Z"
            )
            .await
            .unwrap(),
            None
        );
    }

    #[tokio::test]
    async fn review_profile_always_stamps_reviewed_at() {
        let (_dir, _conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "alice", "tok-alice", "active").await;
        let result =
            AgentRepository::review_profile(&db, "alice", None, None, "2026-01-01T00:00:00Z")
                .await
                .unwrap()
                .unwrap();
        assert_eq!(
            result.agent.profile_reviewed_at.as_deref(),
            Some("2026-01-01T00:00:00Z")
        );
    }

    #[tokio::test]
    async fn review_profile_unchanged_content_does_not_touch_profile_updated_fields() {
        let (_dir, _conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "alice", "tok-alice", "active").await;
        let first = AgentRepository::review_profile(
            &db,
            "alice",
            Some("v1"),
            Some("bob"),
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap()
        .unwrap();
        assert!(first.changed);

        // Re-reviewing the SAME content must not re-stamp
        // profile_updated_at/profile_updated_by, only
        // profile_reviewed_at.
        let second = AgentRepository::review_profile(
            &db,
            "alice",
            Some("v1"),
            Some("carol"),
            "2026-01-02T00:00:00Z",
        )
        .await
        .unwrap()
        .unwrap();
        assert!(!second.changed);
        assert_eq!(second.agent.profile.as_deref(), Some("v1"));
        assert_eq!(
            second.agent.profile_updated_at.as_deref(),
            Some("2026-01-01T00:00:00Z")
        );
        assert_eq!(second.agent.profile_updated_by.as_deref(), Some("bob"));
        assert_eq!(
            second.agent.profile_reviewed_at.as_deref(),
            Some("2026-01-02T00:00:00Z")
        );
    }

    #[tokio::test]
    async fn review_profile_changed_content_updates_profile_and_attribution() {
        let (_dir, _conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "alice", "tok-alice", "active").await;
        AgentRepository::review_profile(
            &db,
            "alice",
            Some("v1"),
            Some("bob"),
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();

        let second = AgentRepository::review_profile(
            &db,
            "alice",
            Some("v2"),
            Some("carol"),
            "2026-01-02T00:00:00Z",
        )
        .await
        .unwrap()
        .unwrap();
        assert!(second.changed);
        assert_eq!(second.agent.profile.as_deref(), Some("v2"));
        assert_eq!(second.agent.profile_updated_by.as_deref(), Some("carol"));
        assert_eq!(
            second.agent.profile_updated_at.as_deref(),
            Some("2026-01-02T00:00:00Z")
        );
    }

    #[tokio::test]
    async fn clear_current_task_for_clears_every_matching_agent_and_returns_count() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "a1", "t1", "active").await;
        seed(&db, "a2", "t2", "active").await;
        seed(&db, "a3", "t3", "active").await;
        AgentRepository::update_field(
            &conn,
            "a1",
            AgentField::CurrentTask,
            FieldValue::OptionalText(Some("task-x".into())),
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        AgentRepository::update_field(
            &conn,
            "a2",
            AgentField::CurrentTask,
            FieldValue::OptionalText(Some("task-x".into())),
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        AgentRepository::update_field(
            &conn,
            "a3",
            AgentField::CurrentTask,
            FieldValue::OptionalText(Some("task-y".into())),
            "2026-01-01T00:00:00Z",
        )
        .unwrap();

        let count =
            AgentRepository::clear_current_task_for(&conn, "task-x", "2026-01-02T00:00:00Z")
                .unwrap();
        assert_eq!(count, 2);
        assert_eq!(
            AgentRepository::get_by_id(&conn, "a1")
                .unwrap()
                .unwrap()
                .current_task,
            None
        );
        assert_eq!(
            AgentRepository::get_by_id(&conn, "a3")
                .unwrap()
                .unwrap()
                .current_task,
            Some("task-y".into())
        );
    }

    #[test]
    fn clear_current_task_for_many_empty_slice_is_a_noop() {
        let conn = test_conn();
        assert_eq!(
            AgentRepository::clear_current_task_for_many(&conn, &[], "2026-01-01T00:00:00Z")
                .unwrap(),
            0
        );
    }

    #[tokio::test]
    async fn clear_current_task_for_many_clears_across_the_whole_set() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "a1", "t1", "active").await;
        seed(&db, "a2", "t2", "active").await;
        seed(&db, "a3", "t3", "active").await;
        AgentRepository::update_field(
            &conn,
            "a1",
            AgentField::CurrentTask,
            FieldValue::OptionalText(Some("task-x".into())),
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        AgentRepository::update_field(
            &conn,
            "a2",
            AgentField::CurrentTask,
            FieldValue::OptionalText(Some("task-y".into())),
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        AgentRepository::update_field(
            &conn,
            "a3",
            AgentField::CurrentTask,
            FieldValue::OptionalText(Some("task-z".into())),
            "2026-01-01T00:00:00Z",
        )
        .unwrap();

        let count = AgentRepository::clear_current_task_for_many(
            &conn,
            &["task-x", "task-y"],
            "2026-01-02T00:00:00Z",
        )
        .unwrap();
        assert_eq!(count, 2);
        assert_eq!(
            AgentRepository::get_by_id(&conn, "a3")
                .unwrap()
                .unwrap()
                .current_task,
            Some("task-z".into())
        );
    }

    #[tokio::test]
    async fn reconcile_current_task_on_reassign_clears_loser_and_sets_gainer_if_free() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "loser", "t1", "active").await;
        seed(&db, "gainer", "t2", "active").await;
        AgentRepository::update_field(
            &conn,
            "loser",
            AgentField::CurrentTask,
            FieldValue::OptionalText(Some("task-1".into())),
            "2026-01-01T00:00:00Z",
        )
        .unwrap();

        AgentRepository::reconcile_current_task_on_reassign(
            &conn,
            "task-1",
            Some("loser"),
            Some("gainer"),
            "2026-01-02T00:00:00Z",
        )
        .unwrap();

        assert_eq!(
            AgentRepository::get_by_id(&conn, "loser")
                .unwrap()
                .unwrap()
                .current_task,
            None
        );
        assert_eq!(
            AgentRepository::get_by_id(&conn, "gainer")
                .unwrap()
                .unwrap()
                .current_task,
            Some("task-1".into())
        );
    }

    #[tokio::test]
    async fn reconcile_current_task_on_reassign_never_clobbers_a_busy_gainer() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "gainer", "t1", "active").await;
        AgentRepository::update_field(
            &conn,
            "gainer",
            AgentField::CurrentTask,
            FieldValue::OptionalText(Some("other-task".into())),
            "2026-01-01T00:00:00Z",
        )
        .unwrap();

        AgentRepository::reconcile_current_task_on_reassign(
            &conn,
            "task-1",
            None,
            Some("gainer"),
            "2026-01-02T00:00:00Z",
        )
        .unwrap();

        // gainer was already busy with a different task; must be untouched.
        assert_eq!(
            AgentRepository::get_by_id(&conn, "gainer")
                .unwrap()
                .unwrap()
                .current_task,
            Some("other-task".into())
        );
    }

    #[tokio::test]
    async fn insert_tombstone_creates_placeholder_row() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        AgentRepository::insert_tombstone(
            &db,
            "purged-token",
            "purged-agent",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();
        let row = AgentRepository::get_by_id(&conn, "purged-agent")
            .unwrap()
            .unwrap();
        assert_eq!(row.status, "tombstone");
        assert_eq!(row.token, "purged-token");
        assert_eq!(row.working_directory, "");
        assert_eq!(row.color.as_deref(), Some("#000000"));
    }

    #[tokio::test]
    async fn insert_tombstone_is_idempotent_via_insert_or_ignore() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        AgentRepository::insert_tombstone(
            &db,
            "purged-token",
            "purged-agent",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();
        // Re-purging the same id must not error (INSERT OR IGNORE).
        AgentRepository::insert_tombstone(
            &db,
            "purged-token",
            "purged-agent",
            "2026-01-02T00:00:00Z",
        )
        .await
        .unwrap();
        let row = AgentRepository::get_by_id(&conn, "purged-agent")
            .unwrap()
            .unwrap();
        // Second call was ignored -- original timestamp survives.
        assert_eq!(row.created_at, "2026-01-01T00:00:00Z");
    }

    fn seed_with_timestamp(
        conn: &Connection,
        agent_id: &str,
        token: &str,
        status: &str,
        created_at: &str,
    ) {
        conn.execute(
            "INSERT INTO agents (token, agent_id, created_at, status, working_directory, agent_role) \
             VALUES (?1, ?2, ?3, ?4, '/tmp', 'worker')",
            (token, agent_id, created_at, status),
        )
        .unwrap();
    }

    fn ids(rows: &[AgentRow]) -> Vec<&str> {
        rows.iter().map(|r| r.agent_id.as_str()).collect()
    }

    #[tokio::test]
    async fn query_default_sort_is_created_at_desc_with_agent_id_tiebreaker() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed_with_timestamp(&conn, "z", "t1", "active", "2026-01-01T00:00:00Z");
        seed_with_timestamp(&conn, "a", "t2", "active", "2026-01-01T00:00:00Z"); // same timestamp
        seed_with_timestamp(&conn, "m", "t3", "active", "2026-01-02T00:00:00Z");

        let repo = AgentRepository::new();
        let (rows, total) = repo.query(&db, AgentQueryFilters::default()).await.unwrap();
        assert_eq!(total, 3);
        // "m" is newest -> first. "z"/"a" tie on created_at -> broken by agent_id ASC.
        assert_eq!(ids(&rows), vec!["m", "a", "z"]);
    }

    #[tokio::test]
    async fn query_excludes_tombstones_unconditionally() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed_with_timestamp(&conn, "live", "t1", "active", "2026-01-01T00:00:00Z");
        AgentRepository::insert_tombstone(&db, "t2", "tomb", "2026-01-01T00:00:00Z")
            .await
            .unwrap();

        let repo = AgentRepository::new();
        let (rows, total) = repo.query(&db, AgentQueryFilters::default()).await.unwrap();
        assert_eq!(total, 1);
        assert_eq!(ids(&rows), vec!["live"]);
    }

    #[tokio::test]
    async fn query_explicit_tombstone_status_filter_is_self_contradictory_and_returns_empty() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed_with_timestamp(&conn, "live", "t1", "active", "2026-01-01T00:00:00Z");
        AgentRepository::insert_tombstone(&db, "t2", "tomb", "2026-01-01T00:00:00Z")
            .await
            .unwrap();

        let repo = AgentRepository::new();
        let (rows, total) = repo
            .query(
                &db,
                AgentQueryFilters {
                    status: Some("tombstone"),
                    ..Default::default()
                },
            )
            .await
            .unwrap();
        assert_eq!((rows.len(), total), (0, 0));
    }

    #[tokio::test]
    async fn query_offset_beyond_total_returns_empty_rows_but_real_total() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed_with_timestamp(&conn, "a1", "t1", "active", "2026-01-01T00:00:00Z");
        seed_with_timestamp(&conn, "a2", "t2", "active", "2026-01-02T00:00:00Z");

        let repo = AgentRepository::new();
        let (rows, total) = repo
            .query(
                &db,
                AgentQueryFilters {
                    offset: 100,
                    ..Default::default()
                },
            )
            .await
            .unwrap();
        assert_eq!(rows.len(), 0);
        assert_eq!(
            total, 2,
            "total must still reflect real matching rows, not 0"
        );
    }

    #[tokio::test]
    async fn query_limit_and_offset_are_clamped_like_python() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed_with_timestamp(&conn, "a1", "t1", "active", "2026-01-01T00:00:00Z");

        let repo = AgentRepository::new();
        // limit=0 clamps to 1, not "everything"; offset=-5 clamps to 0.
        let (rows, _) = repo
            .query(
                &db,
                AgentQueryFilters {
                    limit: 0,
                    offset: -5,
                    ..Default::default()
                },
            )
            .await
            .unwrap();
        assert_eq!(rows.len(), 1);
    }

    #[test]
    fn parse_agent_sort_by_falls_back_to_created_at_on_invalid_input() {
        assert_eq!(parse_agent_sort_by("agent_id"), AgentSortBy::AgentId);
        assert_eq!(parse_agent_sort_by("bogus"), AgentSortBy::CreatedAt);
        assert_eq!(parse_agent_sort_by(""), AgentSortBy::CreatedAt);
    }

    #[test]
    fn parse_sort_order_falls_back_to_desc_on_anything_but_asc() {
        assert_eq!(parse_sort_order("ASC"), SortOrder::Asc);
        assert_eq!(parse_sort_order("asc"), SortOrder::Asc);
        assert_eq!(parse_sort_order("DESC"), SortOrder::Desc);
        assert_eq!(parse_sort_order("garbage"), SortOrder::Desc);
    }

    /// Port of Python's
    /// `test_query_offset_pagination_survives_concurrent_status_change`.
    #[tokio::test]
    async fn query_offset_pagination_survives_concurrent_status_change() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        for i in 1..=5 {
            seed_with_timestamp(
                &conn,
                &format!("pg-a{i}"),
                &format!("t{i}"),
                "active",
                &format!("2026-01-01T00:0{i}:00Z"),
            );
        }
        let repo = AgentRepository::new();
        let filters = || AgentQueryFilters {
            agent_id_pattern: Some("pg-a%"),
            include_terminated: false,
            limit: 2,
            ..Default::default()
        };

        // Newest-first: pg-a5, pg-a4, pg-a3, pg-a2, pg-a1. Anchors
        // that full ordering under this filter shape.
        let (page1, _) = repo
            .query(
                &db,
                AgentQueryFilters {
                    offset: 0,
                    ..filters()
                },
            )
            .await
            .unwrap();
        assert_eq!(ids(&page1), vec!["pg-a5", "pg-a4"]);

        // Concurrent mutation OUTSIDE the paginated API: pg-a5 (rank
        // #1) flips to terminated, which would normally drop it from
        // this include_terminated=false filter and shift every
        // later-ranked agent up by one.
        AgentRepository::terminate(&db, "pg-a5", "2026-01-01T00:10:00Z")
            .await
            .unwrap();

        // offset=2 replays the ANCHOR from page1, not a re-filtered
        // live query -- so the window is still ordered_ids[2:4] from
        // the ORIGINAL 5-element ordering.
        let (page2, _) = repo
            .query(
                &db,
                AgentQueryFilters {
                    offset: 2,
                    ..filters()
                },
            )
            .await
            .unwrap();
        assert_eq!(ids(&page2), vec!["pg-a3", "pg-a2"]);

        // pg-a3 was in-filter for the entire sweep and must never be
        // silently skipped despite pg-a5's status flip mid-sweep.
        let seen: Vec<&str> = ids(&page1).into_iter().chain(ids(&page2)).collect();
        assert!(seen.contains(&"pg-a3"));
    }

    /// Port of Python's `test_query_total_excludes_agent_deleted_mid_sweep`.
    #[tokio::test]
    async fn query_total_excludes_agent_deleted_mid_sweep() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        for i in 1..=7 {
            seed_with_timestamp(
                &conn,
                &format!("tc-a{i}"),
                &format!("t{i}"),
                "active",
                &format!("2026-01-01T00:0{i}:00Z"),
            );
        }
        let repo = AgentRepository::new();
        let filters = |offset| AgentQueryFilters {
            agent_id_pattern: Some("tc-a%"),
            include_terminated: false,
            limit: 2,
            offset,
            ..Default::default()
        };

        // Newest-first: tc-a7..tc-a1. Anchors the 7-element ordering.
        let (page1, total1) = repo.query(&db, filters(0)).await.unwrap();
        assert_eq!(total1, 7);
        let mut delivered = page1.len();

        // Hard-delete the rank-3 agent (tc-a5) -- not yet delivered
        // by any page.
        assert!(AgentRepository::delete(&db, "tc-a5").await.unwrap());

        for offset in [2, 4, 6] {
            let (page, total) = repo.query(&db, filters(offset)).await.unwrap();
            assert_eq!(
                total, 6,
                "total must reconcile the anchor against currently-existing rows"
            );
            delivered += page.len();
        }

        // 2 (page1) + 1 (offset=2, tc-a5's slot dropped, not
        // backfilled) + 2 (offset=4) + 1 (offset=6) == 6.
        assert_eq!(delivered, 6);
    }

    #[tokio::test]
    async fn query_pagination_cache_is_per_repository_instance_not_global() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed_with_timestamp(&conn, "a1", "t1", "active", "2026-01-01T00:00:00Z");
        seed_with_timestamp(&conn, "a2", "t2", "active", "2026-01-02T00:00:00Z");

        let repo_a = AgentRepository::new();
        let repo_b = AgentRepository::new();
        repo_a
            .query(
                &db,
                AgentQueryFilters {
                    offset: 0,
                    limit: 1,
                    ..Default::default()
                },
            )
            .await
            .unwrap();

        // A fresh repository instance has no anchor for this shape,
        // so its own offset>0 call must compute fresh rather than
        // panicking or seeing repo_a's private cache state.
        let (page, _) = repo_b
            .query(
                &db,
                AgentQueryFilters {
                    offset: 1,
                    limit: 1,
                    ..Default::default()
                },
            )
            .await
            .unwrap();
        assert_eq!(page.len(), 1);
    }

    #[tokio::test]
    async fn dump_all_includes_terminal_statuses_unlike_every_other_listing() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "live", "t1", "active").await;
        seed(&db, "dead", "t2", "terminated").await;
        AgentRepository::insert_tombstone(&db, "t3", "tomb", "2026-01-01T00:00:00Z")
            .await
            .unwrap();

        let mut ids: Vec<_> = AgentRepository::dump_all(&conn)
            .unwrap()
            .into_iter()
            .map(|a| a.agent_id)
            .collect();
        ids.sort();
        assert_eq!(ids, vec!["dead", "live", "tomb"]);
    }

    // -- list_profile_changes_since --------------------------------------

    #[tokio::test]
    async fn list_profile_changes_since_excludes_the_editors_own_edit() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "manager", "t1", "active").await;
        seed(&db, "worker", "t2", "active").await;
        AgentRepository::review_profile(
            &db,
            "worker",
            Some("curated by manager"),
            Some("manager"),
            "2026-01-01T00:00:01Z",
        )
        .await
        .unwrap();

        // The editor (manager) never sees its own edit.
        let for_manager =
            AgentRepository::list_profile_changes_since(&conn, "2025-01-01T00:00:00Z", "manager")
                .unwrap();
        assert!(for_manager.is_empty());

        // The subject (worker) is NOT excluded — a manager's curation of
        // a DIFFERENT agent reaches that agent.
        let for_worker =
            AgentRepository::list_profile_changes_since(&conn, "2025-01-01T00:00:00Z", "worker")
                .unwrap();
        assert_eq!(for_worker.len(), 1);
        assert_eq!(for_worker[0].agent_id, "worker");
        assert_eq!(for_worker[0].profile_updated_by.as_deref(), Some("manager"));
    }

    #[tokio::test]
    async fn list_profile_changes_since_excludes_own_null_editor_seed_but_not_a_peers() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "manager", "t1", "active").await;
        seed(&db, "peer", "t2", "active").await;
        // A NULL-editor seed (e.g. an initial charter) on "manager".
        AgentRepository::review_profile(
            &db,
            "manager",
            Some("initial charter"),
            None,
            "2026-01-01T00:00:01Z",
        )
        .await
        .unwrap();

        // "manager" itself never sees its own NULL-editor seed echoed back.
        let for_manager =
            AgentRepository::list_profile_changes_since(&conn, "2025-01-01T00:00:00Z", "manager")
                .unwrap();
        assert!(for_manager.is_empty());

        // A DIFFERENT agent still sees it — a new manager's charter is a
        // roster change worth learning about.
        let for_peer =
            AgentRepository::list_profile_changes_since(&conn, "2025-01-01T00:00:00Z", "peer")
                .unwrap();
        assert_eq!(for_peer.len(), 1);
        assert_eq!(for_peer[0].agent_id, "manager");
    }

    #[tokio::test]
    async fn list_profile_changes_since_excludes_rows_at_or_before_the_cursor() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "manager", "t1", "active").await;
        seed(&db, "worker", "t2", "active").await;
        AgentRepository::review_profile(
            &db,
            "worker",
            Some("v1"),
            Some("manager"),
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();

        assert!(AgentRepository::list_profile_changes_since(
            &conn,
            "2026-01-01T00:00:00Z",
            "someone-else",
        )
        .unwrap()
        .is_empty());
    }

    #[tokio::test]
    async fn list_profile_changes_since_excludes_tombstone_terminated_and_system_rows() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "alice", "t1", "active").await;
        AgentRepository::review_profile(
            &db,
            "alice",
            Some("v1"),
            Some("bob"),
            "2026-01-01T00:00:01Z",
        )
        .await
        .unwrap();
        AgentRepository::update_field(
            &conn,
            "alice",
            AgentField::Status,
            FieldValue::Text("terminated".to_string()),
            "2026-01-01T00:00:02Z",
        )
        .unwrap();

        assert!(AgentRepository::list_profile_changes_since(
            &conn,
            "2025-01-01T00:00:00Z",
            "someone-else",
        )
        .unwrap()
        .is_empty());
    }

    // -- is_live -----------------------------------------------------------

    #[tokio::test]
    async fn is_live_true_for_an_active_agent() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "alice", "t1", "active").await;
        assert!(AgentRepository::is_live(&conn, "alice").unwrap());
    }

    #[tokio::test]
    async fn is_live_false_for_a_terminated_agent() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        seed(&db, "alice", "t1", "terminated").await;
        assert!(!AgentRepository::is_live(&conn, "alice").unwrap());
    }

    #[tokio::test]
    async fn is_live_false_for_a_tombstone() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        AgentRepository::insert_tombstone(&db, "t1", "[deleted-alice]", "2026-01-01T00:00:00Z")
            .await
            .unwrap();
        assert!(!AgentRepository::is_live(&conn, "[deleted-alice]").unwrap());
    }

    #[test]
    fn is_live_false_for_an_unknown_agent() {
        let conn = test_conn();
        assert!(!AgentRepository::is_live(&conn, "nobody").unwrap());
    }
}
