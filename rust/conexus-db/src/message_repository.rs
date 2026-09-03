//! Port of `agent_mcp/repositories/message_repository.py` — Phase 1
//! (core CRUD). Pagination (`query`/`count_query`, the `StableOrderCache`
//! pair), threading (`fetch_thread`), and admin/maintenance surface
//! (`rename_participant`, `list_participants`, `prune_read_before`,
//! subject-backfill) are deliberately deferred to follow-up PRs — see
//! the migration plan's progress log for the phase split rationale.
//!
//! A module of plain functions. Python's version is class-based
//! (`MessageRepository`) but its own docstring states there is NO
//! message cache — "every read goes straight to the DB" — matching
//! this crate's rule (`project_context_repository`) that no state to
//! hold means no wrapper type is needed for THIS phase. (The
//! pagination phase will need a real `StableOrderCache` instance
//! field, at which point this module gains a struct — matching
//! `AgentRepository`'s own precedent of starting as free functions and
//! growing a wrapper only once real state showed up.)
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

use rusqlite::{Connection, OptionalExtension, Result, Row};

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
}
