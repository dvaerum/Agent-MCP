//! Port of `agent_mcp/repositories/message_repository.py` — Phases 1-2
//! (core CRUD + pagination). Threading (`fetch_thread`) and
//! admin/maintenance surface (`rename_participant`, `list_participants`,
//! `prune_read_before`, subject-backfill) are deliberately deferred to
//! a follow-up PR 3/3 — see the migration plan's progress log for the
//! phase split rationale.
//!
//! Mostly plain functions, plus [`MessageRepository`] for the two
//! methods (`query`/`count_query`) that need a real
//! [`StableOrderCache`] instance — Python's version is fully
//! class-based, but its own docstring states there is NO message
//! *row* cache ("every read goes straight to the DB"); the pagination
//! *ordering* cache is a different concern (consistency, not perf),
//! which is why only those two methods live on a struct, matching
//! `AgentRepository`'s own precedent of starting as free functions and
//! growing a wrapper only once real state showed up.
//!
//! Simplified from Python's three-connection-shape design (`None`
//! self-opening / raw sqlite3 cursor / SQLAlchemy `Session`, detected
//! structurally per call) to this crate's uniform `&Connection`-only
//! seam — matching every other repository here. Connection-pool
//! ownership and transaction-joining are an app-layer concern in this
//! design, not a repository one.
//!
//! ## Load-bearing invariants preserved from Python
//! - **PF-R32-1**: `parent_message_id` existence is validated BEFORE
//!   the INSERT, never inferred from catching an FK violation — a
//!   swallowed FK error here once silently discarded a reply as a
//!   false success.
//! - **VM e2e 2026-06-16 recipient bypass**: `recipient_exists` must
//!   run before every INSERT; `"admin"` is a valid recipient despite
//!   having no `agents` row (the synthetic admin row was deleted in
//!   migration 0014); a `[deleted-<id>]` tombstone row IS a live
//!   `agents` row (status `tombstone`) and satisfies the same check.
//! - **Reply-implies-read**: replying to a message clears the
//!   PARENT's unread flag, scoped to `recipient_id == <the replier>`
//!   so a reply can't clear someone ELSE's unread flag, and runs on
//!   the same connection as the reply INSERT (atomic).
//! - **`mark_read` (single message) never publishes** — matches the
//!   dashboard PATCH path's legacy raw-UPDATE behavior. (This crate
//!   doesn't publish events at all yet — noted for when a
//!   composition layer adds that.)

use rusqlite::{Connection, OptionalExtension, Result, Row, ToSql};
use std::collections::HashMap;

use crate::pagination_cache::StableOrderCache;
use crate::sql_util::{in_placeholders, to_sql_refs};

/// One row of the `agent_messages` table.
#[derive(Debug, Clone, PartialEq)]
pub struct MessageRow {
    pub message_id: String,
    pub sender_id: String,
    pub recipient_id: String,
    pub message_content: String,
    pub message_type: String,
    pub priority: String,
    pub timestamp: String,
    pub delivered: bool,
    pub read: bool,
    pub subject: Option<String>,
    pub parent_message_id: Option<String>,
}

const COLUMNS: &str =
    "message_id, sender_id, recipient_id, message_content, message_type, priority, \
     timestamp, delivered, read, subject, parent_message_id";

fn row_to_message(row: &Row) -> rusqlite::Result<MessageRow> {
    Ok(MessageRow {
        message_id: row.get(0)?,
        sender_id: row.get(1)?,
        recipient_id: row.get(2)?,
        message_content: row.get(3)?,
        message_type: row.get(4)?,
        priority: row.get(5)?,
        timestamp: row.get(6)?,
        delivered: row.get(7)?,
        read: row.get(8)?,
        subject: row.get(9)?,
        parent_message_id: row.get(10)?,
    })
}

pub fn get_by_id(conn: &Connection, message_id: &str) -> Result<Option<MessageRow>> {
    conn.query_row(
        &format!("SELECT {COLUMNS} FROM agent_messages WHERE message_id = ?1"),
        [message_id],
        row_to_message,
    )
    .optional()
}

/// Count of unread messages ADDRESSED TO `recipient_id` (never counts
/// messages this recipient sent, nor other recipients' messages).
pub fn count_unread(conn: &Connection, recipient_id: &str) -> Result<i64> {
    conn.query_row(
        "SELECT COUNT(*) FROM agent_messages WHERE recipient_id = ?1 AND read = 0",
        [recipient_id],
        |row| row.get(0),
    )
}

/// `true` iff `recipient_id` is a valid message target: the literal
/// `"admin"` label, or any row in `agents` (live OR tombstone — a
/// purged agent's `[deleted-<id>]` row still satisfies this, which is
/// exactly what lets historical messages keep referencing it).
pub fn recipient_exists(conn: &Connection, recipient_id: &str) -> Result<bool> {
    if recipient_id == "admin" {
        return Ok(true);
    }
    conn.query_row(
        "SELECT 1 FROM agents WHERE agent_id = ?1",
        [recipient_id],
        |_| Ok(()),
    )
    .optional()
    .map(|found| found.is_some())
}

pub fn parent_message_exists(conn: &Connection, parent_message_id: &str) -> Result<bool> {
    conn.query_row(
        "SELECT 1 FROM agent_messages WHERE message_id = ?1",
        [parent_message_id],
        |_| Ok(()),
    )
    .optional()
    .map(|found| found.is_some())
}

/// Best-effort: flips the replied-to parent to `read = 1`, scoped to
/// `recipient_id == replier_id` — only the person who RECEIVED the
/// parent message can implicitly mark it read by replying to it.
fn mark_read_on_reply(conn: &Connection, parent_message_id: &str, replier_id: &str) -> Result<()> {
    conn.execute(
        "UPDATE agent_messages SET read = 1 WHERE message_id = ?1 AND recipient_id = ?2",
        (parent_message_id, replier_id),
    )?;
    Ok(())
}

/// Parameters for [`send`].
pub struct NewMessage<'a> {
    pub message_id: &'a str,
    pub sender_id: &'a str,
    pub recipient_id: &'a str,
    pub message_content: &'a str,
    pub message_type: &'a str,
    pub priority: &'a str,
    pub timestamp: &'a str,
    pub delivered: bool,
    pub read: bool,
    pub subject: Option<&'a str>,
    pub parent_message_id: Option<&'a str>,
}

/// Failure modes of [`send`]. A distinct `ParentMessageNotFound`
/// variant (mirroring Python's `ParentMessageNotFound(LookupError)`
/// subclass) — not folded into `RecipientNotFound` — because callers
/// map the two to genuinely different surfaces (e.g. a different
/// `ToolResult`/HTTP-status message), matching Python's own reason
/// for making it a distinct exception type rather than a shared one.
#[derive(Debug)]
pub enum SendMessageError {
    RecipientNotFound(String),
    ParentMessageNotFound(String),
    Db(rusqlite::Error),
}

impl From<rusqlite::Error> for SendMessageError {
    fn from(e: rusqlite::Error) -> Self {
        SendMessageError::Db(e)
    }
}

impl std::fmt::Display for SendMessageError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SendMessageError::RecipientNotFound(id) => {
                write!(f, "recipient not found: {id:?} is not a known agent")
            }
            SendMessageError::ParentMessageNotFound(id) => {
                write!(f, "parent message not found: {id:?}")
            }
            SendMessageError::Db(e) => write!(f, "database error: {e}"),
        }
    }
}

impl std::error::Error for SendMessageError {}

/// INSERT a message, validating BOTH the recipient and (if given) the
/// parent message BEFORE any write — see the module doc's PF-R32-1
/// and VM-e2e invariants. On a reply (`parent_message_id` set), also
/// flips the parent's `read` flag on the SAME connection (atomic with
/// the INSERT) — see [`mark_read_on_reply`].
pub fn send(
    conn: &Connection,
    msg: NewMessage,
) -> std::result::Result<MessageRow, SendMessageError> {
    if !recipient_exists(conn, msg.recipient_id)? {
        return Err(SendMessageError::RecipientNotFound(
            msg.recipient_id.to_string(),
        ));
    }
    if let Some(parent_id) = msg.parent_message_id {
        if !parent_message_exists(conn, parent_id)? {
            return Err(SendMessageError::ParentMessageNotFound(
                parent_id.to_string(),
            ));
        }
    }

    conn.execute(
        "INSERT INTO agent_messages (message_id, sender_id, recipient_id, message_content, message_type, \
         priority, timestamp, delivered, read, subject, parent_message_id) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
        (
            msg.message_id,
            msg.sender_id,
            msg.recipient_id,
            msg.message_content,
            msg.message_type,
            msg.priority,
            msg.timestamp,
            msg.delivered,
            msg.read,
            msg.subject,
            msg.parent_message_id,
        ),
    )?;

    if let Some(parent_id) = msg.parent_message_id {
        mark_read_on_reply(conn, parent_id, msg.sender_id)?;
    }

    get_by_id(conn, msg.message_id)?
        .ok_or(SendMessageError::Db(rusqlite::Error::QueryReturnedNoRows))
}

pub fn mark_delivered(conn: &Connection, message_id: &str, delivered: bool) -> Result<bool> {
    let changed = conn.execute(
        "UPDATE agent_messages SET delivered = ?1 WHERE message_id = ?2",
        (delivered, message_id),
    )?;
    Ok(changed > 0)
}

/// Flips the `read` flag on exactly ONE message. Deliberately
/// separate from [`mark_read_for_recipient`]/[`mark_read_by_ids`]
/// (bulk operations) — matches Python's dashboard PATCH path.
pub fn mark_read(conn: &Connection, message_id: &str, read: bool) -> Result<bool> {
    let changed = conn.execute(
        "UPDATE agent_messages SET read = ?1 WHERE message_id = ?2",
        (read, message_id),
    )?;
    Ok(changed > 0)
}

/// Flips `read = 1` on every UNREAD row addressed to `recipient_id`.
/// Returns the count actually changed — callers use this to decide
/// whether a "message.read" notification is warranted (a no-op repeat
/// call must not fire one).
pub fn mark_read_for_recipient(conn: &Connection, recipient_id: &str) -> Result<i64> {
    let changed = conn.execute(
        "UPDATE agent_messages SET read = 1 WHERE recipient_id = ?1 AND read = 0",
        [recipient_id],
    )?;
    Ok(changed as i64)
}

/// Flips `read = 1` on exactly the enumerated ids (still-unread only),
/// optionally scoped to one recipient. A no-op (no query run) for an
/// empty slice.
pub fn mark_read_by_ids(
    conn: &Connection,
    message_ids: &[&str],
    recipient_id: Option<&str>,
) -> Result<i64> {
    if message_ids.is_empty() {
        return Ok(0);
    }

    let mut sql = format!(
        "UPDATE agent_messages SET read = 1 WHERE read = 0 AND message_id IN ({})",
        in_placeholders(message_ids.len())
    );
    let mut owned_params: Vec<String> = message_ids.iter().map(|id| id.to_string()).collect();
    if let Some(rid) = recipient_id {
        sql.push_str(" AND recipient_id = ?");
        owned_params.push(rid.to_string());
    }

    let params = to_sql_refs(&owned_params);
    let changed = conn.execute(&sql, params.as_slice())?;
    Ok(changed as i64)
}

pub fn delete(conn: &Connection, message_id: &str) -> Result<bool> {
    let changed = conn.execute(
        "DELETE FROM agent_messages WHERE message_id = ?1",
        [message_id],
    )?;
    Ok(changed > 0)
}

/// Filters accepted by [`MessageRepository::query`]/
/// [`MessageRepository::count_query`] — a typed port of Python's
/// `_apply_query_filters` dict, following `AgentQueryFilters`'s
/// pattern. `limit`/`offset` live here too (matching Python passing
/// the same `filters` dict to both `query` and `count_query`) but are
/// deliberately EXCLUDED from the pagination cache key — see
/// [`MessageQueryCacheKey`].
pub struct MessageQueryFilters<'a> {
    pub from: Option<&'a str>,
    pub to: Option<&'a str>,
    /// Either-direction sender/recipient pair: `(a, b)` matches
    /// `a->b` OR `b->a`.
    pub between: Option<(&'a str, &'a str)>,
    pub message_type: Option<&'a str>,
    pub priority: Option<&'a str>,
    pub read: Option<bool>,
    pub since: Option<&'a str>,
    pub until: Option<&'a str>,
    /// Substring match across `message_content`, `subject`,
    /// `sender_id`, `recipient_id`.
    pub q: Option<&'a str>,
    pub limit: i64,
    pub offset: i64,
}

impl Default for MessageQueryFilters<'_> {
    fn default() -> Self {
        Self {
            from: None,
            to: None,
            between: None,
            message_type: None,
            priority: None,
            read: None,
            since: None,
            until: None,
            q: None,
            limit: 50,
            offset: 0,
        }
    }
}

/// The `StableOrderCache` key: every filter dimension EXCEPT
/// `limit`/`offset` (those vary within one sweep and must not
/// fragment the anchor), plus `oldest_first`. `count_query`
/// deliberately hard-codes `oldest_first: false` when building this,
/// regardless of what a caller might pass elsewhere — matching Python
/// exactly (`count_query` has no `oldest_first` parameter at all).
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct MessageQueryCacheKey {
    from: Option<String>,
    to: Option<String>,
    between: Option<(String, String)>,
    message_type: Option<String>,
    priority: Option<String>,
    read: Option<bool>,
    since: Option<String>,
    until: Option<String>,
    q: Option<String>,
    oldest_first: bool,
}

impl MessageQueryCacheKey {
    fn from_filters(filters: &MessageQueryFilters, oldest_first: bool) -> Self {
        Self {
            from: filters.from.map(String::from),
            to: filters.to.map(String::from),
            between: filters.between.map(|(a, b)| (a.to_string(), b.to_string())),
            message_type: filters.message_type.map(String::from),
            priority: filters.priority.map(String::from),
            read: filters.read,
            since: filters.since.map(String::from),
            until: filters.until.map(String::from),
            q: filters.q.map(String::from),
            oldest_first,
        }
    }
}

/// Builds the `WHERE` fragment + bind params for `filters` — shared
/// verbatim by [`compute_ordered_ids`] (the only query that actually
/// needs it; `query`/`count_query` fetch rows/counts by anchored id
/// list afterward, not by re-applying filters).
fn build_where_clause(filters: &MessageQueryFilters) -> (String, Vec<Box<dyn ToSql>>) {
    let mut clauses: Vec<&str> = Vec::new();
    let mut params: Vec<Box<dyn ToSql>> = Vec::new();

    if let Some(v) = filters.from {
        clauses.push("sender_id = ?");
        params.push(Box::new(v.to_string()));
    }
    if let Some(v) = filters.to {
        clauses.push("recipient_id = ?");
        params.push(Box::new(v.to_string()));
    }
    if let Some((a, b)) = filters.between {
        clauses
            .push("((sender_id = ? AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?))");
        params.push(Box::new(a.to_string()));
        params.push(Box::new(b.to_string()));
        params.push(Box::new(b.to_string()));
        params.push(Box::new(a.to_string()));
    }
    if let Some(v) = filters.message_type {
        clauses.push("message_type = ?");
        params.push(Box::new(v.to_string()));
    }
    if let Some(v) = filters.priority {
        clauses.push("priority = ?");
        params.push(Box::new(v.to_string()));
    }
    if let Some(v) = filters.read {
        clauses.push("read = ?");
        params.push(Box::new(v));
    }
    if let Some(v) = filters.since {
        clauses.push("timestamp >= ?");
        params.push(Box::new(v.to_string()));
    }
    if let Some(v) = filters.until {
        clauses.push("timestamp <= ?");
        params.push(Box::new(v.to_string()));
    }
    if let Some(v) = filters.q {
        let pattern = format!("%{v}%");
        clauses.push(
            "(message_content LIKE ? OR subject LIKE ? OR sender_id LIKE ? OR recipient_id LIKE ?)",
        );
        params.push(Box::new(pattern.clone()));
        params.push(Box::new(pattern.clone()));
        params.push(Box::new(pattern.clone()));
        params.push(Box::new(pattern));
    }

    let where_sql = if clauses.is_empty() {
        "1=1".to_string()
    } else {
        clauses.join(" AND ")
    };
    (where_sql, params)
}

/// The ONE `ORDER BY` both `query` and `count_query` must route
/// through (R18-F2: `count_query` used to have its own unordered
/// closure with no `.order_by()` at all, which — because
/// `get_or_anchor` unconditionally overwrites the shared cache entry
/// on every `offset == 0` call — silently clobbered `query`'s
/// correctly-ordered anchor with whatever unspecified order SQLite's
/// planner picked). A fixed `message_id ASC` tiebreak guarantees a
/// deterministic order even when `timestamp` collides.
fn compute_ordered_ids(
    conn: &Connection,
    filters: &MessageQueryFilters,
    oldest_first: bool,
) -> Result<Vec<String>> {
    let (where_sql, params) = build_where_clause(filters);
    let order_dir = if oldest_first { "ASC" } else { "DESC" };
    let sql = format!("SELECT message_id FROM agent_messages WHERE {where_sql} ORDER BY timestamp {order_dir}, message_id ASC");

    let mut stmt = conn.prepare(&sql)?;
    let param_refs: Vec<&dyn ToSql> = params.iter().map(|b| b.as_ref()).collect();
    let rows = stmt.query_map(param_refs.as_slice(), |row| row.get::<_, String>(0))?;
    rows.collect()
}

/// Owns the [`StableOrderCache`] instance `query`/`count_query` need.
/// A per-instance field (NOT a process-wide singleton) — Python's
/// `MessageRepository._pagination_cache` is a class attribute (shared
/// across every instance/request), but the Rust convention already
/// established for `AgentRepository` is deliberately per-instance
/// (see `pagination_cache.rs`'s own doc: "never a single process-wide
/// singleton"); this repository follows that same established
/// convention rather than copying Python's.
#[derive(Default)]
pub struct MessageRepository {
    pagination_cache: StableOrderCache<MessageQueryCacheKey, String>,
}

impl MessageRepository {
    pub fn new() -> Self {
        Self::default()
    }

    /// Filtered, sorted, paginated listing. Ordering is anchored via
    /// [`StableOrderCache`] exactly like `AgentRepository::query` —
    /// `offset == 0` always recomputes+re-anchors; `offset > 0`
    /// replays the anchor from this sweep unless it's missing/expired.
    /// Returns `Vec::new()` for an empty/exhausted window rather than
    /// an error.
    pub fn query(
        &self,
        conn: &Connection,
        filters: &MessageQueryFilters,
        oldest_first: bool,
    ) -> Result<Vec<MessageRow>> {
        let limit = filters.limit.clamp(1, 500);
        let offset = filters.offset.max(0);
        let cache_key = MessageQueryCacheKey::from_filters(filters, oldest_first);

        let ordered_ids: Vec<String> =
            self.pagination_cache.get_or_anchor(cache_key, offset, || {
                compute_ordered_ids(conn, filters, oldest_first)
            })?;

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
            return Ok(Vec::new());
        }

        let sql = format!(
            "SELECT {COLUMNS} FROM agent_messages WHERE message_id IN ({})",
            in_placeholders(window_ids.len())
        );
        let params = to_sql_refs(&window_ids);
        let mut stmt = conn.prepare(&sql)?;
        let rows_by_id: HashMap<String, MessageRow> = stmt
            .query_map(params.as_slice(), row_to_message)?
            .collect::<Result<Vec<_>>>()?
            .into_iter()
            .map(|row| (row.message_id.clone(), row))
            .collect();

        // Reassemble in window_ids (anchored) order, silently
        // dropping any id that no longer resolves.
        Ok(window_ids
            .into_iter()
            .filter_map(|id| rows_by_id.get(id).cloned())
            .collect())
    }

    /// Total matching `filters`, anchored to the SAME `StableOrderCache`
    /// entry `query` uses for identical filters (R18-F2) — NOT a fresh
    /// `COUNT(*)`. Reconciles the anchored id list against rows that
    /// still exist right now (R21-F3), so a hard delete of an
    /// already-anchored-but-undelivered row is reflected on every
    /// later page. Always uses `oldest_first: false` for its cache key
    /// (matching Python: `count_query` has no such parameter).
    pub fn count_query(&self, conn: &Connection, filters: &MessageQueryFilters) -> Result<i64> {
        let offset = filters.offset.max(0);
        let cache_key = MessageQueryCacheKey::from_filters(filters, false);

        let ordered_ids: Vec<String> =
            self.pagination_cache.get_or_anchor(cache_key, offset, || {
                compute_ordered_ids(conn, filters, false)
            })?;

        if ordered_ids.is_empty() {
            return Ok(0);
        }

        let sql = format!(
            "SELECT COUNT(*) FROM agent_messages WHERE message_id IN ({})",
            in_placeholders(ordered_ids.len())
        );
        let params = to_sql_refs(&ordered_ids);
        conn.query_row(&sql, params.as_slice(), |row| row.get(0))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::agent_repository::{AgentRepository, NewAgent};
    use crate::schema::init_schema;

    fn test_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        conn
    }

    fn seed_agent(conn: &Connection, agent_id: &str) {
        AgentRepository::create(
            conn,
            NewAgent {
                token: &format!("tok-{agent_id}"),
                agent_id,
                created_at: "2026-01-01T00:00:00Z",
                status: "active",
                current_task: None,
                working_directory: "/tmp",
                color: None,
                agent_role: "worker",
            },
        )
        .unwrap();
    }

    fn new_msg<'a>(
        id: &'a str,
        sender: &'a str,
        recipient: &'a str,
        content: &'a str,
    ) -> NewMessage<'a> {
        NewMessage {
            message_id: id,
            sender_id: sender,
            recipient_id: recipient,
            message_content: content,
            message_type: "text",
            priority: "normal",
            timestamp: "2026-01-01T00:00:00Z",
            delivered: false,
            read: false,
            subject: None,
            parent_message_id: None,
        }
    }

    #[test]
    fn get_by_id_returns_none_for_unknown_message() {
        let conn = test_conn();
        assert_eq!(get_by_id(&conn, "nope").unwrap(), None);
    }

    #[test]
    fn recipient_exists_admin_is_always_valid() {
        let conn = test_conn();
        assert!(recipient_exists(&conn, "admin").unwrap());
    }

    #[test]
    fn recipient_exists_true_for_live_agent_false_for_unknown() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        assert!(recipient_exists(&conn, "alice").unwrap());
        assert!(!recipient_exists(&conn, "nope").unwrap());
    }

    #[test]
    fn recipient_exists_true_for_tombstoned_agent() {
        let conn = test_conn();
        AgentRepository::insert_tombstone(
            &conn,
            "tok-x",
            "[deleted-alice]",
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        assert!(recipient_exists(&conn, "[deleted-alice]").unwrap());
    }

    #[test]
    fn parent_message_exists_checks_message_id_not_agent_id() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        seed_agent(&conn, "bob");
        send(&conn, new_msg("msg-1", "alice", "bob", "hello")).unwrap();
        assert!(parent_message_exists(&conn, "msg-1").unwrap());
        assert!(!parent_message_exists(&conn, "msg-nope").unwrap());
    }

    #[test]
    fn send_succeeds_and_returns_the_inserted_row() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        seed_agent(&conn, "bob");

        let row = send(&conn, new_msg("msg-1", "alice", "bob", "hello bob")).unwrap();
        assert_eq!(row.message_id, "msg-1");
        assert_eq!(row.sender_id, "alice");
        assert_eq!(row.recipient_id, "bob");
        assert_eq!(row.message_content, "hello bob");
        assert!(!row.delivered);
        assert!(!row.read);
    }

    #[test]
    fn send_to_unknown_recipient_fails_and_writes_nothing() {
        let conn = test_conn();
        seed_agent(&conn, "alice");

        let err = send(&conn, new_msg("msg-1", "alice", "nope", "hello")).unwrap_err();
        assert!(matches!(err, SendMessageError::RecipientNotFound(id) if id == "nope"));
        assert_eq!(get_by_id(&conn, "msg-1").unwrap(), None);
    }

    #[test]
    fn send_with_unknown_parent_fails_and_writes_nothing() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        seed_agent(&conn, "bob");

        let mut msg = new_msg("msg-2", "alice", "bob", "a reply");
        msg.parent_message_id = Some("does-not-exist");
        let err = send(&conn, msg).unwrap_err();
        assert!(
            matches!(err, SendMessageError::ParentMessageNotFound(id) if id == "does-not-exist")
        );
        assert_eq!(get_by_id(&conn, "msg-2").unwrap(), None);
    }

    #[test]
    fn send_admin_as_recipient_does_not_require_an_agents_row() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        let row = send(&conn, new_msg("msg-1", "alice", "admin", "hi")).unwrap();
        assert_eq!(row.recipient_id, "admin");
    }

    #[test]
    fn reply_marks_the_parent_read_scoped_to_the_original_recipient() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        seed_agent(&conn, "bob");

        // alice -> bob (bob is the recipient, so bob is the one whose
        // reply should be able to mark it read).
        send(&conn, new_msg("msg-1", "alice", "bob", "question")).unwrap();
        assert!(!get_by_id(&conn, "msg-1").unwrap().unwrap().read);

        let mut reply = new_msg("msg-2", "bob", "alice", "answer");
        reply.parent_message_id = Some("msg-1");
        send(&conn, reply).unwrap();

        assert!(
            get_by_id(&conn, "msg-1").unwrap().unwrap().read,
            "bob's reply must mark the original message read"
        );
    }

    #[test]
    fn reply_from_a_non_recipient_does_not_mark_the_parent_read() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        seed_agent(&conn, "bob");
        seed_agent(&conn, "carol");

        // alice -> bob. carol is NOT the recipient, so even if carol
        // somehow references msg-1 as a parent, it must not flip read.
        send(&conn, new_msg("msg-1", "alice", "bob", "question")).unwrap();

        let mut reply = new_msg("msg-2", "carol", "alice", "butting in");
        reply.parent_message_id = Some("msg-1");
        send(&conn, reply).unwrap();

        assert!(
            !get_by_id(&conn, "msg-1").unwrap().unwrap().read,
            "only the actual recipient's reply may mark it read"
        );
    }

    #[test]
    fn count_unread_only_counts_rows_addressed_to_the_recipient() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        seed_agent(&conn, "bob");
        seed_agent(&conn, "carol");
        send(&conn, new_msg("m1", "alice", "bob", "x")).unwrap();
        send(&conn, new_msg("m2", "carol", "bob", "y")).unwrap();
        send(&conn, new_msg("m3", "bob", "alice", "z")).unwrap(); // bob is sender here, not recipient
        mark_read(&conn, "m2", true).unwrap();

        assert_eq!(count_unread(&conn, "bob").unwrap(), 1);
    }

    #[test]
    fn mark_delivered_flips_the_flag_and_reports_whether_a_row_matched() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        seed_agent(&conn, "bob");
        send(&conn, new_msg("m1", "alice", "bob", "x")).unwrap();

        assert!(mark_delivered(&conn, "m1", true).unwrap());
        assert!(get_by_id(&conn, "m1").unwrap().unwrap().delivered);
        assert!(!mark_delivered(&conn, "nope", true).unwrap());
    }

    #[test]
    fn mark_read_single_message() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        seed_agent(&conn, "bob");
        send(&conn, new_msg("m1", "alice", "bob", "x")).unwrap();

        assert!(mark_read(&conn, "m1", true).unwrap());
        assert!(get_by_id(&conn, "m1").unwrap().unwrap().read);
    }

    #[test]
    fn mark_read_for_recipient_flips_only_unread_rows_for_that_recipient() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        seed_agent(&conn, "bob");
        seed_agent(&conn, "carol");
        send(&conn, new_msg("m1", "alice", "bob", "x")).unwrap();
        send(&conn, new_msg("m2", "alice", "bob", "y")).unwrap();
        send(&conn, new_msg("m3", "alice", "carol", "z")).unwrap();

        let changed = mark_read_for_recipient(&conn, "bob").unwrap();
        assert_eq!(changed, 2);
        assert!(get_by_id(&conn, "m1").unwrap().unwrap().read);
        assert!(get_by_id(&conn, "m2").unwrap().unwrap().read);
        assert!(
            !get_by_id(&conn, "m3").unwrap().unwrap().read,
            "carol's message must be untouched"
        );

        // Second call: nothing left unread -> a real, honest 0 (used
        // by callers to suppress a spurious "message.read" event).
        assert_eq!(mark_read_for_recipient(&conn, "bob").unwrap(), 0);
    }

    #[test]
    fn mark_read_by_ids_empty_slice_is_a_noop() {
        let conn = test_conn();
        assert_eq!(mark_read_by_ids(&conn, &[], None).unwrap(), 0);
    }

    #[test]
    fn mark_read_by_ids_flips_only_the_enumerated_still_unread_ids() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        seed_agent(&conn, "bob");
        send(&conn, new_msg("m1", "alice", "bob", "x")).unwrap();
        send(&conn, new_msg("m2", "alice", "bob", "y")).unwrap();
        send(&conn, new_msg("m3", "alice", "bob", "z")).unwrap();

        let changed = mark_read_by_ids(&conn, &["m1", "m2", "does-not-exist"], None).unwrap();
        assert_eq!(changed, 2);
        assert!(get_by_id(&conn, "m1").unwrap().unwrap().read);
        assert!(get_by_id(&conn, "m2").unwrap().unwrap().read);
        assert!(!get_by_id(&conn, "m3").unwrap().unwrap().read);
    }

    #[test]
    fn mark_read_by_ids_recipient_scoping_excludes_other_recipients_messages() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        seed_agent(&conn, "bob");
        seed_agent(&conn, "carol");
        send(&conn, new_msg("m1", "alice", "bob", "x")).unwrap();
        send(&conn, new_msg("m2", "alice", "carol", "y")).unwrap();

        // Scoped to "bob" -- m2 (addressed to carol) must not flip
        // even though its id was explicitly listed.
        let changed = mark_read_by_ids(&conn, &["m1", "m2"], Some("bob")).unwrap();
        assert_eq!(changed, 1);
        assert!(get_by_id(&conn, "m1").unwrap().unwrap().read);
        assert!(!get_by_id(&conn, "m2").unwrap().unwrap().read);
    }

    #[test]
    fn delete_removes_row_and_returns_true() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        seed_agent(&conn, "bob");
        send(&conn, new_msg("m1", "alice", "bob", "x")).unwrap();

        assert!(delete(&conn, "m1").unwrap());
        assert_eq!(get_by_id(&conn, "m1").unwrap(), None);
    }

    #[test]
    fn delete_missing_message_returns_false() {
        let conn = test_conn();
        assert!(!delete(&conn, "nope").unwrap());
    }

    fn seed_msg(conn: &Connection, id: &str, sender: &str, recipient: &str, timestamp: &str) {
        let mut msg = new_msg(id, sender, recipient, "content");
        msg.timestamp = timestamp;
        send(conn, msg).unwrap();
    }

    fn ids(rows: &[MessageRow]) -> Vec<&str> {
        rows.iter().map(|r| r.message_id.as_str()).collect()
    }

    #[test]
    fn query_default_sort_is_newest_first_with_message_id_tiebreaker() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        seed_agent(&conn, "bob");
        seed_msg(&conn, "z", "alice", "bob", "2026-01-01T00:00:00Z");
        seed_msg(&conn, "a", "alice", "bob", "2026-01-01T00:00:00Z"); // same timestamp
        seed_msg(&conn, "m", "alice", "bob", "2026-01-02T00:00:00Z");

        let repo = MessageRepository::new();
        let rows = repo
            .query(&conn, &MessageQueryFilters::default(), false)
            .unwrap();
        // "m" newest -> first. "z"/"a" tie on timestamp -> message_id ASC.
        assert_eq!(ids(&rows), vec!["m", "a", "z"]);
    }

    #[test]
    fn query_oldest_first_reverses_order() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        seed_agent(&conn, "bob");
        seed_msg(&conn, "old", "alice", "bob", "2026-01-01T00:00:00Z");
        seed_msg(&conn, "new", "alice", "bob", "2026-01-02T00:00:00Z");

        let repo = MessageRepository::new();
        let rows = repo
            .query(&conn, &MessageQueryFilters::default(), true)
            .unwrap();
        assert_eq!(ids(&rows), vec!["old", "new"]);
    }

    #[test]
    fn query_filters_from_to_and_substring_q() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        seed_agent(&conn, "bob");
        seed_agent(&conn, "carol");
        send(&conn, new_msg("m1", "alice", "bob", "hello there")).unwrap();
        send(&conn, new_msg("m2", "alice", "carol", "unrelated")).unwrap();
        send(&conn, new_msg("m3", "bob", "alice", "hello back")).unwrap();

        let repo = MessageRepository::new();
        let from_alice = repo
            .query(
                &conn,
                &MessageQueryFilters {
                    from: Some("alice"),
                    ..Default::default()
                },
                false,
            )
            .unwrap();
        assert_eq!(ids(&from_alice).len(), 2);

        let to_carol = repo
            .query(
                &conn,
                &MessageQueryFilters {
                    to: Some("carol"),
                    ..Default::default()
                },
                false,
            )
            .unwrap();
        assert_eq!(ids(&to_carol), vec!["m2"]);

        let hello = repo
            .query(
                &conn,
                &MessageQueryFilters {
                    q: Some("hello"),
                    ..Default::default()
                },
                false,
            )
            .unwrap();
        assert_eq!(ids(&hello).len(), 2);
    }

    #[test]
    fn query_between_matches_either_direction() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        seed_agent(&conn, "bob");
        seed_agent(&conn, "carol");
        send(&conn, new_msg("m1", "alice", "bob", "a to b")).unwrap();
        send(&conn, new_msg("m2", "bob", "alice", "b to a")).unwrap();
        send(&conn, new_msg("m3", "alice", "carol", "a to c")).unwrap();

        let repo = MessageRepository::new();
        let rows = repo
            .query(
                &conn,
                &MessageQueryFilters {
                    between: Some(("alice", "bob")),
                    ..Default::default()
                },
                false,
            )
            .unwrap();
        let mut got = ids(&rows);
        got.sort();
        assert_eq!(got, vec!["m1", "m2"]);
    }

    #[test]
    fn query_limit_is_clamped_between_1_and_500() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        seed_agent(&conn, "bob");
        seed_msg(&conn, "m1", "alice", "bob", "2026-01-01T00:00:00Z");

        let repo = MessageRepository::new();
        let rows = repo
            .query(
                &conn,
                &MessageQueryFilters {
                    limit: 0,
                    ..Default::default()
                },
                false,
            )
            .unwrap();
        assert_eq!(rows.len(), 1, "limit=0 clamps to 1, not 'everything'");
    }

    #[test]
    fn count_query_matches_query_total_for_unfiltered_sweep() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        seed_agent(&conn, "bob");
        for i in 1..=3 {
            seed_msg(
                &conn,
                &format!("m{i}"),
                "alice",
                "bob",
                &format!("2026-01-0{i}T00:00:00Z"),
            );
        }

        let repo = MessageRepository::new();
        assert_eq!(
            repo.count_query(&conn, &MessageQueryFilters::default())
                .unwrap(),
            3
        );
    }

    /// Port of Python's
    /// `test_query_offset_pagination_survives_concurrent_read_flag_change`
    /// (R17-F2).
    #[test]
    fn query_offset_pagination_survives_concurrent_read_flag_change() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        seed_agent(&conn, "bob");
        for i in 1..=5 {
            seed_msg(
                &conn,
                &format!("m{i}"),
                "alice",
                "bob",
                &format!("2026-01-01T00:0{i}:00Z"),
            );
        }
        let repo = MessageRepository::new();
        let filters = || MessageQueryFilters {
            to: Some("bob"),
            read: Some(false),
            limit: 2,
            ..Default::default()
        };

        // Newest-first: m5, m4, m3, m2, m1. Anchors that ordering.
        let page1 = repo
            .query(
                &conn,
                &MessageQueryFilters {
                    offset: 0,
                    ..filters()
                },
                false,
            )
            .unwrap();
        assert_eq!(ids(&page1), vec!["m5", "m4"]);

        // Concurrent mutation OUTSIDE the paginated API: m5 (rank #1)
        // gets marked read, which would drop it from this
        // read=false filter and shift every later-ranked message up.
        mark_read(&conn, "m5", true).unwrap();

        // offset=2 must replay the ANCHOR from page1, not a
        // re-filtered live query.
        let page2 = repo
            .query(
                &conn,
                &MessageQueryFilters {
                    offset: 2,
                    ..filters()
                },
                false,
            )
            .unwrap();
        assert_eq!(ids(&page2), vec!["m3", "m2"]);

        // m3 was in-filter the entire sweep and must never be skipped.
        let seen: Vec<&str> = ids(&page1).into_iter().chain(ids(&page2)).collect();
        assert!(seen.contains(&"m3"));
    }

    /// Port of Python's
    /// `test_count_query_offset_pagination_survives_concurrent_read_flag_change`.
    #[test]
    fn count_query_total_stays_anchored_despite_concurrent_read_flag_change() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        seed_agent(&conn, "bob");
        for i in 1..=5 {
            seed_msg(
                &conn,
                &format!("m{i}"),
                "alice",
                "bob",
                &format!("2026-01-01T00:0{i}:00Z"),
            );
        }
        let repo = MessageRepository::new();
        let filters = |offset| MessageQueryFilters {
            to: Some("bob"),
            read: Some(false),
            limit: 2,
            offset,
            ..Default::default()
        };

        assert_eq!(repo.count_query(&conn, &filters(0)).unwrap(), 5);
        mark_read(&conn, "m5", true).unwrap();
        // Total stays anchored at 5 despite the mid-sweep flag flip --
        // NOT a fresh live COUNT (which would now be 4).
        assert_eq!(repo.count_query(&conn, &filters(2)).unwrap(), 5);
    }

    /// Port of Python's `test_count_query_total_excludes_message_deleted_mid_sweep`
    /// (R21-F3).
    #[test]
    fn count_query_total_excludes_message_deleted_mid_sweep() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        seed_agent(&conn, "bob");
        for i in 1..=7 {
            seed_msg(
                &conn,
                &format!("m{i}"),
                "alice",
                "bob",
                &format!("2026-01-01T00:0{i}:00Z"),
            );
        }
        let repo = MessageRepository::new();
        let filters = |offset| MessageQueryFilters {
            to: Some("bob"),
            limit: 2,
            offset,
            ..Default::default()
        };

        let page1 = repo.query(&conn, &filters(0), false).unwrap();
        assert_eq!(repo.count_query(&conn, &filters(0)).unwrap(), 7);
        let mut delivered = page1.len();

        // Hard-delete the rank-3 message (m5, newest-first: m7 m6 m5
        // m4 m3 m2 m1) -- not yet delivered by any page.
        assert!(delete(&conn, "m5").unwrap());

        for offset in [2, 4, 6] {
            let total = repo.count_query(&conn, &filters(offset)).unwrap();
            assert_eq!(
                total, 6,
                "total must reconcile the anchor against currently-existing rows"
            );
            delivered += repo.query(&conn, &filters(offset), false).unwrap().len();
        }

        // 2 (page1) + 1 (offset=2, m5's slot dropped) + 2 (offset=4) + 1 (offset=6) == 6.
        assert_eq!(delivered, 6);
    }

    /// Port of Python's `test_query_and_count_query_anchor_the_identical_ordered_ids`.
    #[test]
    fn query_and_count_query_share_the_identical_anchor() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        seed_agent(&conn, "bob");
        for i in 1..=4 {
            seed_msg(
                &conn,
                &format!("m{i}"),
                "alice",
                "bob",
                &format!("2026-01-01T00:0{i}:00Z"),
            );
        }
        let repo = MessageRepository::new();
        let filters = MessageQueryFilters {
            to: Some("bob"),
            limit: 2,
            ..Default::default()
        };

        // query() anchors the ordering first...
        let page1 = repo.query(&conn, &filters, false).unwrap();
        assert_eq!(ids(&page1), vec!["m4", "m3"]);

        // ...count_query() must reuse that SAME anchor, not compute
        // its own (unordered) closure -- if it did, this total could
        // silently clobber query()'s anchor with a different order
        // (R18-F2). Confirmed indirectly: a later query() page must
        // still see the original DESC ordering.
        assert_eq!(repo.count_query(&conn, &filters).unwrap(), 4);
        let page2 = repo
            .query(
                &conn,
                &MessageQueryFilters {
                    offset: 2,
                    ..filters
                },
                false,
            )
            .unwrap();
        assert_eq!(ids(&page2), vec!["m2", "m1"]);
    }
}
