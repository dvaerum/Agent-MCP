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
#[derive(Debug, Clone, PartialEq)]
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

/// Pure DB-CRUD surface for the `agents` table. Stateless — every
/// method takes the connection it should run against, matching the
/// Python source's `connection=` seam (this Rust port has ONLY that
/// seam: there is no separate "standalone, opens its own connection"
/// path, since owning a connection pool is an app-layer concern, not
/// a repository concern).
pub struct AgentRepository;

impl AgentRepository {
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
}
