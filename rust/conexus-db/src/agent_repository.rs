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

use crate::pagination_cache::StableOrderCache;
use crate::sql_util::{in_placeholders, to_sql_refs};
use regex::Regex;
use rusqlite::{Connection, OptionalExtension, Result, Row};
use std::collections::HashMap;
use std::sync::LazyLock;

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
#[derive(Debug)]
pub enum CreateAgentError {
    InvalidAgentId(String),
    Conflict(rusqlite::Error),
    Db(rusqlite::Error),
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

fn is_unique_violation(err: &rusqlite::Error) -> bool {
    matches!(
        err,
        rusqlite::Error::SqliteFailure(e, _) if e.code == rusqlite::ErrorCode::ConstraintViolation
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
    pub fn create(
        conn: &Connection,
        new_agent: NewAgent,
    ) -> std::result::Result<AgentRow, CreateAgentError> {
        if !is_valid_agent_id(new_agent.agent_id)
            || new_agent.agent_id.starts_with(RESERVED_AGENT_ID_PREFIX)
        {
            return Err(CreateAgentError::InvalidAgentId(
                new_agent.agent_id.to_string(),
            ));
        }

        conn.execute(
            "INSERT INTO agents (token, agent_id, created_at, status, current_task, \
             working_directory, color, agent_role) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            (
                new_agent.token,
                new_agent.agent_id,
                new_agent.created_at,
                new_agent.status,
                new_agent.current_task,
                new_agent.working_directory,
                new_agent.color,
                new_agent.agent_role,
            ),
        )
        .map_err(|e| {
            if is_unique_violation(&e) {
                CreateAgentError::Conflict(e)
            } else {
                CreateAgentError::Db(e)
            }
        })?;

        Self::get_by_id(conn, new_agent.agent_id)
            .map_err(CreateAgentError::Db)?
            .ok_or_else(|| CreateAgentError::Db(rusqlite::Error::QueryReturnedNoRows))
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
    pub fn seed_manager_profile(
        conn: &Connection,
        agent_id: &str,
        profile: &str,
        seed_ts: &str,
    ) -> Result<()> {
        conn.execute(
            "UPDATE agents SET profile = ?1, profile_updated_at = ?2, \
             profile_reviewed_at = ?3, profile_updated_by = NULL WHERE agent_id = ?4",
            (profile, seed_ts, seed_ts, agent_id),
        )?;
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
    pub fn terminate(conn: &Connection, agent_id: &str, now: &str) -> Result<bool> {
        let changed = conn.execute(
            &format!(
                "UPDATE agents SET status = 'terminated', terminated_at = ?1, updated_at = ?1, \
                 current_task = NULL WHERE agent_id = ?2 AND {NOT_TERMINAL_SQL}"
            ),
            (now, agent_id),
        )?;
        Ok(changed > 0)
    }

    /// Hard delete — distinct from `terminate()`'s soft delete.
    pub fn delete(conn: &Connection, agent_id: &str) -> Result<bool> {
        let changed = conn.execute("DELETE FROM agents WHERE agent_id = ?1", [agent_id])?;
        Ok(changed > 0)
    }

    /// The one write path for the auth secret (`token` is off
    /// `update_field`'s allowlist by design). `false` iff no agent
    /// with that `agent_id` exists.
    pub fn rotate_token(
        conn: &Connection,
        agent_id: &str,
        new_token: &str,
        now: &str,
    ) -> Result<bool> {
        let changed = conn.execute(
            "UPDATE agents SET token = ?1, updated_at = ?2 WHERE agent_id = ?3",
            (new_token, now, agent_id),
        )?;
        Ok(changed > 0)
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
    pub fn review_profile(
        conn: &Connection,
        agent_id: &str,
        new_profile: Option<&str>,
        editor_id: Option<&str>,
        now: &str,
    ) -> Result<Option<ReviewProfileResult>> {
        use sha2::{Digest, Sha256};

        let Some(existing) = Self::get_by_id(conn, agent_id)? else {
            return Ok(None);
        };

        let hash =
            |s: Option<&str>| -> Vec<u8> { Sha256::digest(s.unwrap_or("").as_bytes()).to_vec() };
        let changed = hash(new_profile) != hash(existing.profile.as_deref());

        if changed {
            conn.execute(
                "UPDATE agents SET profile = ?1, profile_updated_at = ?2, profile_updated_by = ?3, \
                 profile_reviewed_at = ?2, updated_at = ?2 WHERE agent_id = ?4",
                (new_profile, now, editor_id, agent_id),
            )?;
        } else {
            conn.execute(
                "UPDATE agents SET profile_reviewed_at = ?1, updated_at = ?1 WHERE agent_id = ?2",
                (now, agent_id),
            )?;
        }

        let agent = Self::get_by_id(conn, agent_id)?
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
    pub fn insert_tombstone(
        conn: &Connection,
        token: &str,
        tombstone_agent_id: &str,
        now: &str,
    ) -> Result<()> {
        conn.execute(
            "INSERT OR IGNORE INTO agents (token, agent_id, created_at, status, working_directory, color, updated_at) \
             VALUES (?1, ?2, ?3, 'tombstone', '', '#000000', ?3)",
            (token, tombstone_agent_id, now),
        )?;
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
    pub fn query(
        &self,
        conn: &Connection,
        filters: AgentQueryFilters,
    ) -> Result<(Vec<AgentRow>, i64)> {
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

        let ordered_ids: Vec<String> =
            self.pagination_cache.get_or_anchor(cache_key, offset, || {
                Self::compute_ordered_ids(
                    conn,
                    status.as_deref(),
                    pattern.as_deref(),
                    include_terminated,
                    created_after.as_deref(),
                    created_before.as_deref(),
                    sort_by,
                    sort_order,
                )
            })?;

        if ordered_ids.is_empty() {
            return Ok((Vec::new(), 0));
        }

        // total = the anchored ids, reconciled against rows that
        // still exist right now (NOT a fresh unconditional COUNT).
        let total: i64 = {
            let sql = format!(
                "SELECT COUNT(*) FROM agents WHERE agent_id IN ({})",
                in_placeholders(ordered_ids.len())
            );
            let params = to_sql_refs(&ordered_ids);
            conn.query_row(&sql, params.as_slice(), |row| row.get(0))?
        };

        let offset_usize = offset as usize;
        let window_ids: Vec<&String> = if offset_usize < ordered_ids.len() {
            ordered_ids[offset_usize..]
                .iter()
                .take(limit as usize)
                .collect()
        } else {
            Vec::new()
        };

        if window_ids.is_empty() {
            return Ok((Vec::new(), total));
        }

        let sql = format!(
            "SELECT {AGENT_COLUMNS} FROM agents WHERE agent_id IN ({})",
            in_placeholders(window_ids.len())
        );
        let params = to_sql_refs(&window_ids);
        let mut stmt = conn.prepare(&sql)?;
        let rows_by_id: HashMap<String, AgentRow> = stmt
            .query_map(params.as_slice(), row_to_agent)?
            .collect::<Result<Vec<_>>>()?
            .into_iter()
            .map(|row| (row.agent_id.clone(), row))
            .collect();

        // Reassemble in window_ids (anchored) order, silently
        // dropping any id that no longer resolves — matches Python's
        // `if aid in rows_by_id` guard exactly.
        let ordered_rows = window_ids
            .into_iter()
            .filter_map(|id| rows_by_id.get(id).cloned())
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

    #[allow(clippy::too_many_arguments)]
    fn compute_ordered_ids(
        conn: &Connection,
        status: Option<&str>,
        pattern: Option<&str>,
        include_terminated: bool,
        created_after: Option<&str>,
        created_before: Option<&str>,
        sort_by: AgentSortBy,
        sort_order: SortOrder,
    ) -> Result<Vec<String>> {
        let mut sql = String::from("SELECT agent_id FROM agents WHERE status != 'tombstone'");
        let mut owned_params: Vec<String> = Vec::new();

        if let Some(s) = status {
            sql.push_str(" AND status = ?");
            owned_params.push(s.to_string());
        }
        if let Some(p) = pattern {
            sql.push_str(" AND agent_id LIKE ?");
            owned_params.push(p.to_string());
        }
        if !include_terminated {
            sql.push_str(" AND status != 'terminated'");
        }
        if let Some(a) = created_after {
            sql.push_str(" AND created_at >= ?");
            owned_params.push(a.to_string());
        }
        if let Some(b) = created_before {
            sql.push_str(" AND created_at <= ?");
            owned_params.push(b.to_string());
        }
        // Fixed `agent_id ASC` tiebreaker guarantees a fully
        // deterministic total order even when the sort column has
        // duplicate values — essential for offset pagination
        // correctness (two agents created in the same second must
        // still sort identically on every page).
        sql.push_str(&format!(
            " ORDER BY {} {}, agent_id ASC",
            sort_by.column(),
            sort_order.sql()
        ));

        let mut stmt = conn.prepare(&sql)?;
        let params = to_sql_refs(&owned_params);
        let rows = stmt.query_map(params.as_slice(), |row| row.get::<_, String>(0))?;
        rows.collect()
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

impl AgentSortBy {
    fn column(self) -> &'static str {
        match self {
            AgentSortBy::AgentId => "agent_id",
            AgentSortBy::Status => "status",
            AgentSortBy::CreatedAt => "created_at",
            AgentSortBy::TerminatedAt => "terminated_at",
        }
    }
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

impl SortOrder {
    fn sql(self) -> &'static str {
        match self {
            SortOrder::Asc => "ASC",
            SortOrder::Desc => "DESC",
        }
    }
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

    fn seed(conn: &Connection, agent_id: &str, token: &str, status: &str) {
        AgentRepository::create(
            conn,
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
        .unwrap();
    }

    #[test]
    fn get_by_id_returns_none_for_unknown_agent() {
        let conn = test_conn();
        assert_eq!(AgentRepository::get_by_id(&conn, "nope").unwrap(), None);
    }

    #[test]
    fn create_then_get_by_id_and_get_by_token_round_trip() {
        let conn = test_conn();
        seed(&conn, "alice", "tok-alice", "active");

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

    #[test]
    fn create_rejects_invalid_agent_id_before_any_write() {
        let conn = test_conn();
        let err = AgentRepository::create(
            &conn,
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
        .unwrap_err();
        assert!(matches!(err, CreateAgentError::InvalidAgentId(_)));
        assert_eq!(AgentRepository::get_by_token(&conn, "t1").unwrap(), None);
    }

    #[test]
    fn create_rejects_reserved_admin_prefix() {
        let conn = test_conn();
        let err = AgentRepository::create(
            &conn,
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
        .unwrap_err();
        assert!(matches!(err, CreateAgentError::InvalidAgentId(_)));
    }

    #[test]
    fn seed_manager_profile_sets_all_three_columns_and_leaves_updated_by_null() {
        let conn = test_conn();
        seed(&conn, "manager-1", "tok-m1", "active");
        AgentRepository::seed_manager_profile(
            &conn,
            "manager-1",
            "You are a manager.",
            "2026-06-01T00:00:00Z",
        )
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

    #[test]
    fn create_accepts_single_character_agent_id() {
        let conn = test_conn();
        seed(&conn, "a", "tok-a", "active");
        assert!(AgentRepository::get_by_id(&conn, "a").unwrap().is_some());
    }

    #[test]
    fn create_duplicate_agent_id_is_a_conflict_not_a_generic_db_error() {
        let conn = test_conn();
        seed(&conn, "alice", "tok-alice", "active");
        let err = AgentRepository::create(
            &conn,
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
        .unwrap_err();
        assert!(matches!(err, CreateAgentError::Conflict(_)));
    }

    #[test]
    fn list_active_excludes_terminated_and_tombstone() {
        let conn = test_conn();
        seed(&conn, "live1", "t1", "active");
        seed(&conn, "dead1", "t2", "terminated");
        seed(&conn, "tomb1", "t3", "tombstone");
        seed(&conn, "live2", "t4", "created");

        let mut ids: Vec<_> = AgentRepository::list_active(&conn)
            .unwrap()
            .into_iter()
            .map(|a| a.agent_id)
            .collect();
        ids.sort();
        assert_eq!(ids, vec!["live1", "live2"]);
    }

    #[test]
    fn list_all_bounded_includes_every_status_and_respects_the_limit() {
        let conn = test_conn();
        seed(&conn, "live1", "t1", "active");
        seed(&conn, "dead1", "t2", "terminated");
        seed(&conn, "tomb1", "t3", "tombstone");

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

    #[test]
    fn count_active_by_status_excludes_terminal_and_groups_correctly() {
        let conn = test_conn();
        seed(&conn, "a1", "t1", "active");
        seed(&conn, "a2", "t2", "active");
        seed(&conn, "c1", "t3", "created");
        seed(&conn, "d1", "t4", "terminated");

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

    #[test]
    fn update_field_writes_value_and_bumps_updated_at() {
        let conn = test_conn();
        seed(&conn, "alice", "tok-alice", "created");

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

    #[test]
    fn update_field_auto_event_loop_coerces_bool_to_integer_column() {
        let conn = test_conn();
        seed(&conn, "alice", "tok-alice", "active");

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

    #[test]
    fn terminate_sets_status_and_clears_current_task() {
        let conn = test_conn();
        seed(&conn, "alice", "tok-alice", "active");
        AgentRepository::update_field(
            &conn,
            "alice",
            AgentField::CurrentTask,
            FieldValue::OptionalText(Some("task-1".into())),
            "2026-01-01T00:00:00Z",
        )
        .unwrap();

        assert!(AgentRepository::terminate(&conn, "alice", "2026-01-03T00:00:00Z").unwrap());

        let row = AgentRepository::get_by_id(&conn, "alice").unwrap().unwrap();
        assert_eq!(row.status, "terminated");
        assert_eq!(row.terminated_at.as_deref(), Some("2026-01-03T00:00:00Z"));
        assert_eq!(row.current_task, None);
    }

    #[test]
    fn terminate_missing_agent_returns_false() {
        let conn = test_conn();
        assert!(!AgentRepository::terminate(&conn, "nope", "2026-01-01T00:00:00Z").unwrap());
    }

    #[test]
    fn terminate_already_terminal_is_a_noop_not_a_re_stamp() {
        let conn = test_conn();
        seed(&conn, "alice", "tok-alice", "active");
        assert!(AgentRepository::terminate(&conn, "alice", "2026-01-01T00:00:00Z").unwrap());
        // Second terminate on an already-terminal row must report
        // "no row matched" — matches Python excluding terminal rows
        // from the UPDATE's WHERE clause explicitly.
        assert!(!AgentRepository::terminate(&conn, "alice", "2026-01-02T00:00:00Z").unwrap());
        let row = AgentRepository::get_by_id(&conn, "alice").unwrap().unwrap();
        assert_eq!(row.terminated_at.as_deref(), Some("2026-01-01T00:00:00Z"));
    }

    #[test]
    fn delete_removes_row_and_returns_true() {
        let conn = test_conn();
        seed(&conn, "alice", "tok-alice", "active");
        assert!(AgentRepository::delete(&conn, "alice").unwrap());
        assert_eq!(AgentRepository::get_by_id(&conn, "alice").unwrap(), None);
    }

    #[test]
    fn delete_missing_agent_returns_false() {
        let conn = test_conn();
        assert!(!AgentRepository::delete(&conn, "nope").unwrap());
    }

    #[test]
    fn rotate_token_writes_new_token_and_old_token_no_longer_resolves() {
        let conn = test_conn();
        seed(&conn, "alice", "tok-old", "active");
        assert!(
            AgentRepository::rotate_token(&conn, "alice", "tok-new", "2026-01-02T00:00:00Z")
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

    #[test]
    fn rotate_token_missing_agent_returns_false() {
        let conn = test_conn();
        assert!(
            !AgentRepository::rotate_token(&conn, "nope", "tok-new", "2026-01-01T00:00:00Z")
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

    #[test]
    fn advance_event_cursor_first_write_advances_from_null() {
        let conn = test_conn();
        seed(&conn, "alice", "tok-alice", "active");
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

    #[test]
    fn advance_event_cursor_never_regresses() {
        let conn = test_conn();
        seed(&conn, "alice", "tok-alice", "active");
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

    #[test]
    fn review_profile_missing_agent_returns_none() {
        let conn = test_conn();
        assert_eq!(
            AgentRepository::review_profile(
                &conn,
                "nope",
                Some("hi"),
                Some("editor"),
                "2026-01-01T00:00:00Z"
            )
            .unwrap(),
            None
        );
    }

    #[test]
    fn review_profile_always_stamps_reviewed_at() {
        let conn = test_conn();
        seed(&conn, "alice", "tok-alice", "active");
        let result =
            AgentRepository::review_profile(&conn, "alice", None, None, "2026-01-01T00:00:00Z")
                .unwrap()
                .unwrap();
        assert_eq!(
            result.agent.profile_reviewed_at.as_deref(),
            Some("2026-01-01T00:00:00Z")
        );
    }

    #[test]
    fn review_profile_unchanged_content_does_not_touch_profile_updated_fields() {
        let conn = test_conn();
        seed(&conn, "alice", "tok-alice", "active");
        let first = AgentRepository::review_profile(
            &conn,
            "alice",
            Some("v1"),
            Some("bob"),
            "2026-01-01T00:00:00Z",
        )
        .unwrap()
        .unwrap();
        assert!(first.changed);

        // Re-reviewing the SAME content must not re-stamp
        // profile_updated_at/profile_updated_by, only
        // profile_reviewed_at.
        let second = AgentRepository::review_profile(
            &conn,
            "alice",
            Some("v1"),
            Some("carol"),
            "2026-01-02T00:00:00Z",
        )
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

    #[test]
    fn review_profile_changed_content_updates_profile_and_attribution() {
        let conn = test_conn();
        seed(&conn, "alice", "tok-alice", "active");
        AgentRepository::review_profile(
            &conn,
            "alice",
            Some("v1"),
            Some("bob"),
            "2026-01-01T00:00:00Z",
        )
        .unwrap();

        let second = AgentRepository::review_profile(
            &conn,
            "alice",
            Some("v2"),
            Some("carol"),
            "2026-01-02T00:00:00Z",
        )
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

    #[test]
    fn clear_current_task_for_clears_every_matching_agent_and_returns_count() {
        let conn = test_conn();
        seed(&conn, "a1", "t1", "active");
        seed(&conn, "a2", "t2", "active");
        seed(&conn, "a3", "t3", "active");
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

    #[test]
    fn clear_current_task_for_many_clears_across_the_whole_set() {
        let conn = test_conn();
        seed(&conn, "a1", "t1", "active");
        seed(&conn, "a2", "t2", "active");
        seed(&conn, "a3", "t3", "active");
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

    #[test]
    fn reconcile_current_task_on_reassign_clears_loser_and_sets_gainer_if_free() {
        let conn = test_conn();
        seed(&conn, "loser", "t1", "active");
        seed(&conn, "gainer", "t2", "active");
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

    #[test]
    fn reconcile_current_task_on_reassign_never_clobbers_a_busy_gainer() {
        let conn = test_conn();
        seed(&conn, "gainer", "t1", "active");
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

    #[test]
    fn insert_tombstone_creates_placeholder_row() {
        let conn = test_conn();
        AgentRepository::insert_tombstone(
            &conn,
            "purged-token",
            "purged-agent",
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        let row = AgentRepository::get_by_id(&conn, "purged-agent")
            .unwrap()
            .unwrap();
        assert_eq!(row.status, "tombstone");
        assert_eq!(row.token, "purged-token");
        assert_eq!(row.working_directory, "");
        assert_eq!(row.color.as_deref(), Some("#000000"));
    }

    #[test]
    fn insert_tombstone_is_idempotent_via_insert_or_ignore() {
        let conn = test_conn();
        AgentRepository::insert_tombstone(
            &conn,
            "purged-token",
            "purged-agent",
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        // Re-purging the same id must not error (INSERT OR IGNORE).
        AgentRepository::insert_tombstone(
            &conn,
            "purged-token",
            "purged-agent",
            "2026-01-02T00:00:00Z",
        )
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

    #[test]
    fn query_default_sort_is_created_at_desc_with_agent_id_tiebreaker() {
        let conn = test_conn();
        seed_with_timestamp(&conn, "z", "t1", "active", "2026-01-01T00:00:00Z");
        seed_with_timestamp(&conn, "a", "t2", "active", "2026-01-01T00:00:00Z"); // same timestamp
        seed_with_timestamp(&conn, "m", "t3", "active", "2026-01-02T00:00:00Z");

        let repo = AgentRepository::new();
        let (rows, total) = repo.query(&conn, AgentQueryFilters::default()).unwrap();
        assert_eq!(total, 3);
        // "m" is newest -> first. "z"/"a" tie on created_at -> broken by agent_id ASC.
        assert_eq!(ids(&rows), vec!["m", "a", "z"]);
    }

    #[test]
    fn query_excludes_tombstones_unconditionally() {
        let conn = test_conn();
        seed_with_timestamp(&conn, "live", "t1", "active", "2026-01-01T00:00:00Z");
        AgentRepository::insert_tombstone(&conn, "t2", "tomb", "2026-01-01T00:00:00Z").unwrap();

        let repo = AgentRepository::new();
        let (rows, total) = repo.query(&conn, AgentQueryFilters::default()).unwrap();
        assert_eq!(total, 1);
        assert_eq!(ids(&rows), vec!["live"]);
    }

    #[test]
    fn query_explicit_tombstone_status_filter_is_self_contradictory_and_returns_empty() {
        let conn = test_conn();
        seed_with_timestamp(&conn, "live", "t1", "active", "2026-01-01T00:00:00Z");
        AgentRepository::insert_tombstone(&conn, "t2", "tomb", "2026-01-01T00:00:00Z").unwrap();

        let repo = AgentRepository::new();
        let (rows, total) = repo
            .query(
                &conn,
                AgentQueryFilters {
                    status: Some("tombstone"),
                    ..Default::default()
                },
            )
            .unwrap();
        assert_eq!((rows.len(), total), (0, 0));
    }

    #[test]
    fn query_offset_beyond_total_returns_empty_rows_but_real_total() {
        let conn = test_conn();
        seed_with_timestamp(&conn, "a1", "t1", "active", "2026-01-01T00:00:00Z");
        seed_with_timestamp(&conn, "a2", "t2", "active", "2026-01-02T00:00:00Z");

        let repo = AgentRepository::new();
        let (rows, total) = repo
            .query(
                &conn,
                AgentQueryFilters {
                    offset: 100,
                    ..Default::default()
                },
            )
            .unwrap();
        assert_eq!(rows.len(), 0);
        assert_eq!(
            total, 2,
            "total must still reflect real matching rows, not 0"
        );
    }

    #[test]
    fn query_limit_and_offset_are_clamped_like_python() {
        let conn = test_conn();
        seed_with_timestamp(&conn, "a1", "t1", "active", "2026-01-01T00:00:00Z");

        let repo = AgentRepository::new();
        // limit=0 clamps to 1, not "everything"; offset=-5 clamps to 0.
        let (rows, _) = repo
            .query(
                &conn,
                AgentQueryFilters {
                    limit: 0,
                    offset: -5,
                    ..Default::default()
                },
            )
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
    #[test]
    fn query_offset_pagination_survives_concurrent_status_change() {
        let conn = test_conn();
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
                &conn,
                AgentQueryFilters {
                    offset: 0,
                    ..filters()
                },
            )
            .unwrap();
        assert_eq!(ids(&page1), vec!["pg-a5", "pg-a4"]);

        // Concurrent mutation OUTSIDE the paginated API: pg-a5 (rank
        // #1) flips to terminated, which would normally drop it from
        // this include_terminated=false filter and shift every
        // later-ranked agent up by one.
        AgentRepository::terminate(&conn, "pg-a5", "2026-01-01T00:10:00Z").unwrap();

        // offset=2 replays the ANCHOR from page1, not a re-filtered
        // live query -- so the window is still ordered_ids[2:4] from
        // the ORIGINAL 5-element ordering.
        let (page2, _) = repo
            .query(
                &conn,
                AgentQueryFilters {
                    offset: 2,
                    ..filters()
                },
            )
            .unwrap();
        assert_eq!(ids(&page2), vec!["pg-a3", "pg-a2"]);

        // pg-a3 was in-filter for the entire sweep and must never be
        // silently skipped despite pg-a5's status flip mid-sweep.
        let seen: Vec<&str> = ids(&page1).into_iter().chain(ids(&page2)).collect();
        assert!(seen.contains(&"pg-a3"));
    }

    /// Port of Python's `test_query_total_excludes_agent_deleted_mid_sweep`.
    #[test]
    fn query_total_excludes_agent_deleted_mid_sweep() {
        let conn = test_conn();
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
        let (page1, total1) = repo.query(&conn, filters(0)).unwrap();
        assert_eq!(total1, 7);
        let mut delivered = page1.len();

        // Hard-delete the rank-3 agent (tc-a5) -- not yet delivered
        // by any page.
        assert!(AgentRepository::delete(&conn, "tc-a5").unwrap());

        for offset in [2, 4, 6] {
            let (page, total) = repo.query(&conn, filters(offset)).unwrap();
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

    #[test]
    fn query_pagination_cache_is_per_repository_instance_not_global() {
        let conn = test_conn();
        seed_with_timestamp(&conn, "a1", "t1", "active", "2026-01-01T00:00:00Z");
        seed_with_timestamp(&conn, "a2", "t2", "active", "2026-01-02T00:00:00Z");

        let repo_a = AgentRepository::new();
        let repo_b = AgentRepository::new();
        repo_a
            .query(
                &conn,
                AgentQueryFilters {
                    offset: 0,
                    limit: 1,
                    ..Default::default()
                },
            )
            .unwrap();

        // A fresh repository instance has no anchor for this shape,
        // so its own offset>0 call must compute fresh rather than
        // panicking or seeing repo_a's private cache state.
        let (page, _) = repo_b
            .query(
                &conn,
                AgentQueryFilters {
                    offset: 1,
                    limit: 1,
                    ..Default::default()
                },
            )
            .unwrap();
        assert_eq!(page.len(), 1);
    }

    #[test]
    fn dump_all_includes_terminal_statuses_unlike_every_other_listing() {
        let conn = test_conn();
        seed(&conn, "live", "t1", "active");
        seed(&conn, "dead", "t2", "terminated");
        AgentRepository::insert_tombstone(&conn, "t3", "tomb", "2026-01-01T00:00:00Z").unwrap();

        let mut ids: Vec<_> = AgentRepository::dump_all(&conn)
            .unwrap()
            .into_iter()
            .map(|a| a.agent_id)
            .collect();
        ids.sort();
        assert_eq!(ids, vec!["dead", "live", "tomb"]);
    }

    // -- list_profile_changes_since --------------------------------------

    #[test]
    fn list_profile_changes_since_excludes_the_editors_own_edit() {
        let conn = test_conn();
        seed(&conn, "manager", "t1", "active");
        seed(&conn, "worker", "t2", "active");
        AgentRepository::review_profile(
            &conn,
            "worker",
            Some("curated by manager"),
            Some("manager"),
            "2026-01-01T00:00:01Z",
        )
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

    #[test]
    fn list_profile_changes_since_excludes_own_null_editor_seed_but_not_a_peers() {
        let conn = test_conn();
        seed(&conn, "manager", "t1", "active");
        seed(&conn, "peer", "t2", "active");
        // A NULL-editor seed (e.g. an initial charter) on "manager".
        AgentRepository::review_profile(
            &conn,
            "manager",
            Some("initial charter"),
            None,
            "2026-01-01T00:00:01Z",
        )
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

    #[test]
    fn list_profile_changes_since_excludes_rows_at_or_before_the_cursor() {
        let conn = test_conn();
        seed(&conn, "manager", "t1", "active");
        seed(&conn, "worker", "t2", "active");
        AgentRepository::review_profile(
            &conn,
            "worker",
            Some("v1"),
            Some("manager"),
            "2026-01-01T00:00:00Z",
        )
        .unwrap();

        assert!(AgentRepository::list_profile_changes_since(
            &conn,
            "2026-01-01T00:00:00Z",
            "someone-else",
        )
        .unwrap()
        .is_empty());
    }

    #[test]
    fn list_profile_changes_since_excludes_tombstone_terminated_and_system_rows() {
        let conn = test_conn();
        seed(&conn, "alice", "t1", "active");
        AgentRepository::review_profile(
            &conn,
            "alice",
            Some("v1"),
            Some("bob"),
            "2026-01-01T00:00:01Z",
        )
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

    #[test]
    fn is_live_true_for_an_active_agent() {
        let conn = test_conn();
        seed(&conn, "alice", "t1", "active");
        assert!(AgentRepository::is_live(&conn, "alice").unwrap());
    }

    #[test]
    fn is_live_false_for_a_terminated_agent() {
        let conn = test_conn();
        seed(&conn, "alice", "t1", "terminated");
        assert!(!AgentRepository::is_live(&conn, "alice").unwrap());
    }

    #[test]
    fn is_live_false_for_a_tombstone() {
        let conn = test_conn();
        AgentRepository::insert_tombstone(&conn, "t1", "[deleted-alice]", "2026-01-01T00:00:00Z")
            .unwrap();
        assert!(!AgentRepository::is_live(&conn, "[deleted-alice]").unwrap());
    }

    #[test]
    fn is_live_false_for_an_unknown_agent() {
        let conn = test_conn();
        assert!(!AgentRepository::is_live(&conn, "nobody").unwrap());
    }
}
