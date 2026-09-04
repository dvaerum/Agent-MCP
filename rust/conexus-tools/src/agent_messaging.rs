//! Port of `agent_communication_tools.py`'s outgoing-message machinery:
//! `_sender_label`, `_can_agents_communicate`,
//! `check_send_message_permission`, and
//! `send_agent_message_tool_impl`'s write core. Originally landed
//! (Phase D4, PR 8/8, decision 3) as a MINIMAL, unregistered helper
//! for `request_assistance_tool_impl`'s internal call (mirroring
//! Python's own plain-function call, never through the dispatcher) --
//! now ALSO the write core `SendAgentMessageTool`
//! (`agent_communication_tools.rs`) uses as the real, registered
//! `send_agent_message` MCP tool. Both callers share this one
//! permission-gate + INSERT implementation; only the post-send
//! niceties (wake, reply-nudge, response text) differ per caller.
//!
//! ## Re-derivation, documented: "both agents are active"
//!
//! Python's worker-to-worker gate's last clause requires BOTH sender
//! and recipient to be in `state.active_agents` -- an in-memory,
//! per-process registry of agents with a currently open session/
//! connection, populated by the auth middleware on each authenticated
//! request. This crate has no equivalent: no per-connection registry
//! exists beyond `WaiterRegistry` (which only tracks agents currently
//! blocked inside `wait_for_events`, not "has an open session" in
//! general). This port uses `AgentRepository::is_live` (DB
//! status != terminated/tombstone) instead -- the closest available
//! signal, and the one every other "is this agent reachable" check in
//! this crate already uses.
//!
//! This is a genuine, documented WIDENING, not a neutral swap: a
//! registered-but-currently-disconnected agent can now be messaged,
//! where Python would refuse (no open connection to wake). Now that
//! `send_agent_message` IS a real, externally-callable MCP tool, this
//! widening has real live surface (previously it did not, since the
//! only call site always targeted the literal `"admin"` recipient,
//! which hits the unconditional-allow clause above this branch) --
//! tracked here, not silently inherited, in case a future pentest
//! pass wants to re-examine it.

use conexus_core::principal::{is_operator_tier, Principal, PrincipalKind};
use conexus_core::tool_result::ToolResult;
use conexus_db::agent_repository::AgentRepository;
use conexus_db::message_repository::{self, NewMessage, SendMessageError};
use conexus_db::project_settings_repository;
use rusqlite::Connection;

/// Port of `_sender_label`.
pub fn sender_label(principal: &Principal) -> String {
    principal
        .agent_id
        .clone()
        .or_else(|| principal.user_id.clone())
        .unwrap_or_else(|| "operator".to_string())
}

/// Port of `_can_agents_communicate`. See this module's own doc for
/// the "both active" re-derivation.
pub fn can_agents_communicate(
    conn: &Connection,
    sender_id: &str,
    recipient_id: &str,
    is_admin: bool,
) -> (bool, String) {
    if is_admin {
        return (true, "Admin privileges".to_string());
    }
    if sender_id == recipient_id {
        return (
            false,
            "you cannot message yourself. To record your own progress/context on a task, \
             use add_task_comment(task_id=..., text=...) instead."
                .to_string(),
        );
    }
    // Match the canonical "admin" identity EXACTLY -- a startswith
    // wildcard would let a worker message any agent whose id merely
    // begins with "admin", bypassing the worker-to-worker default-deny.
    if recipient_id.to_lowercase() == "admin" {
        return (true, "Admin agent always contactable".to_string());
    }
    if !project_settings_repository::get_bool(conn, "config_allow_worker_to_worker", true) {
        return (
            false,
            "Worker-to-worker messaging disabled by policy".to_string(),
        );
    }
    let both_live = AgentRepository::is_live(conn, sender_id).unwrap_or(false)
        && AgentRepository::is_live(conn, recipient_id).unwrap_or(false);
    if both_live {
        return (true, "Both agents are active".to_string());
    }
    (
        false,
        "the recipient is not a currently-active agent (it may be offline, terminated, or \
         unknown). Only messages between two currently-active agents are delivered."
            .to_string(),
    )
}

/// Port of `check_send_message_permission`. `None` means permitted.
pub fn check_send_message_permission(
    conn: &Connection,
    principal: &Principal,
    recipient_id: &str,
    message_content: &str,
    message_type: &str,
) -> Option<ToolResult> {
    let is_admin = is_operator_tier(principal);
    let sender_id = sender_label(principal);

    if !is_admin {
        if principal.kind != PrincipalKind::AgentBearer {
            return Some(ToolResult::PermissionDenied {
                reason: "Valid token required".to_string(),
            });
        }
        if !project_settings_repository::get_bool(conn, "config_allow_worker_to_worker", true) {
            return Some(ToolResult::PermissionDenied {
                reason: "Communication denied: direct agent-to-agent messaging is disabled \
                    for workers by the config_allow_worker_to_worker policy (this also \
                    blocks messaging admins). To escalate to a human/admin, use \
                    request_assistance(task_id=<your task>, description=...), or ask an \
                    admin to enable worker messaging in dashboard Settings."
                    .to_string(),
            });
        }
    }

    if message_content.len() > 4000 {
        return Some(ToolResult::Invalid {
            field: Some("message".to_string()),
            message: "Message too long (max 4000 characters)".to_string(),
        });
    }

    if message_type == "stop_command" && !is_admin {
        return Some(ToolResult::PermissionDenied {
            reason: "message_type 'stop_command' is admin-only. If you need another agent \
                to stop, send a normal 'text' message explaining the request, or use \
                request_assistance to escalate to an admin."
                .to_string(),
        });
    }

    let (can_communicate, reason) =
        can_agents_communicate(conn, &sender_id, recipient_id, is_admin);
    if !can_communicate {
        return Some(ToolResult::PermissionDenied {
            reason: format!("Communication denied: {reason}"),
        });
    }

    None
}

/// The outcome of [`send_agent_message`]. A closed enum replacing
/// Python's mix of a returned `ToolResult` (permission denials) and
/// raised exceptions (`LookupError`/`ParentMessageNotFound`) -- every
/// caller matches exhaustively rather than string-sniffing a denial
/// reason, matching this migration's own `UpdateSingleTaskOutcome`/
/// `EditCommentError` precedent.
pub enum SendOutcome {
    Sent {
        message_id: String,
    },
    /// The exact `ToolResult` `check_send_message_permission` built
    /// (`PermissionDenied` or `Invalid` -- e.g. the 4000-char cap) --
    /// propagated verbatim so a caller that wants to return it
    /// directly (the real `send_agent_message` MCP tool) doesn't need
    /// to re-derive which variant it was.
    Denied(ToolResult),
    RecipientNotFound(String),
    ParentMessageNotFound(String),
    // No `StoreFailed` variant: Python's `_MessageStoreFailed` exists
    // because `send()` can swallow an internal error and return `None`
    // without raising (PF-R32-1's "never report a false success"
    // guard against that). `message_repository::send` has no such
    // silent-`None` path -- every failure is a typed `SendMessageError`
    // (`RecipientNotFound`/`ParentMessageNotFound`/`Db`), so that whole
    // failure class is structurally impossible here, not merely
    // unencountered.
}

/// Arguments for [`send_agent_message`] -- grouped into one struct
/// (matching this crate's own `NewMessage`/`NewTask` convention)
/// rather than a long positional parameter list.
pub struct SendMessageArgs<'a> {
    pub recipient_id: &'a str,
    pub message_content: &'a str,
    pub message_type: &'a str,
    pub priority: &'a str,
    pub subject: Option<&'a str>,
    pub parent_message_id: Option<&'a str>,
    pub now: &'a str,
}

/// Port of the write core of `send_agent_message_tool_impl` -- the
/// permission gate + the effective-subject computation + the DB
/// INSERT. Minus the dashboard-facing niceties no caller of this
/// function needs yet: `deliver_method` (already a documented Python
/// no-op -- every message is DB-stored, delivered via
/// `wait_for_events`) and the post-send recipient wake / reply-nudge
/// hint / response-text formatting, which are each caller's own
/// responsibility (`request_assistance` fires its own
/// `ctx.waiter_registry.notify`; `SendAgentMessageTool` does the same
/// plus the reply-nudge text).
///
/// Runs on the CALLER's own transaction (`tx`) so the message INSERT
/// and the caller's own writes commit or roll back together --
/// matches Python's own emit-iff-commit framing (a single
/// `unit_of_work()` covering the whole call).
pub fn send_agent_message(
    tx: &Connection,
    principal: &Principal,
    args: SendMessageArgs<'_>,
) -> Result<SendOutcome, rusqlite::Error> {
    let SendMessageArgs {
        recipient_id,
        message_content,
        message_type,
        priority,
        subject,
        parent_message_id,
        now,
    } = args;

    if let Some(denial) =
        check_send_message_permission(tx, principal, recipient_id, message_content, message_type)
    {
        return Ok(SendOutcome::Denied(denial));
    }

    let sender_id = sender_label(principal);
    let message_id = format!("msg_{:016x}", rand_u64());

    // A reply (parent_message_id set) always stores subject: NULL --
    // the dashboard surfaces the root's subject as the thread label;
    // replies don't carry their own. Matches Python's own
    // `effective_subject` branch exactly.
    let effective_subject = if parent_message_id.is_some() {
        None
    } else {
        subject
    };

    match message_repository::send(
        tx,
        NewMessage {
            message_id: &message_id,
            sender_id: &sender_id,
            recipient_id,
            message_content,
            message_type,
            priority,
            timestamp: now,
            delivered: false,
            read: false,
            subject: effective_subject,
            parent_message_id,
        },
    ) {
        Ok(_) => Ok(SendOutcome::Sent { message_id }),
        Err(SendMessageError::RecipientNotFound(id)) => Ok(SendOutcome::RecipientNotFound(id)),
        Err(SendMessageError::ParentMessageNotFound(id)) => {
            Ok(SendOutcome::ParentMessageNotFound(id))
        }
        Err(SendMessageError::Db(e)) => Err(e),
    }
}

/// `secrets.token_hex(8)`'s Rust equivalent -- a random 64-bit value
/// rendered as 16 lowercase hex digits, matching `_generate_message_id`'s
/// shape closely enough for a message id (uniqueness is what matters,
/// not CSPRNG-grade unpredictability -- this id is never a capability
/// or a security boundary, just a primary key).
fn rand_u64() -> u64 {
    use std::collections::hash_map::RandomState;
    use std::hash::BuildHasher;
    RandomState::new().hash_one(std::time::Instant::now())
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_core::capability::Capabilities;
    use conexus_db::agent_repository::NewAgent;
    use conexus_db::schema::init_schema;

    const NOW: &str = "2026-05-01T00:00:00Z";

    fn test_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        conn
    }

    fn worker(agent_id: &str) -> Principal {
        Principal {
            kind: PrincipalKind::AgentBearer,
            user_id: None,
            agent_id: Some(agent_id.to_string()),
            project_name: None,
            project_role: None,
            agent_role: None,
            can_wake_loop: true,
            source_token: None,
            capabilities: Capabilities::from_iter([]),
        }
    }

    fn seed_agent(conn: &Connection, agent_id: &str) {
        AgentRepository::create(
            conn,
            NewAgent {
                token: &format!("tok-{agent_id}"),
                agent_id,
                created_at: NOW,
                status: "active",
                current_task: None,
                working_directory: "/tmp",
                color: None,
                agent_role: "worker",
            },
        )
        .unwrap();
    }

    #[test]
    fn can_agents_communicate_admin_recipient_always_allowed() {
        let conn = test_conn();
        let (ok, _) = can_agents_communicate(&conn, "bob", "admin", false);
        assert!(ok);
    }

    #[test]
    fn can_agents_communicate_self_message_denied() {
        let conn = test_conn();
        let (ok, reason) = can_agents_communicate(&conn, "bob", "bob", false);
        assert!(!ok);
        assert!(reason.contains("add_task_comment"));
    }

    #[test]
    fn can_agents_communicate_admin_prefix_recipient_is_not_the_real_admin() {
        let conn = test_conn();
        // "admin-helper" must NOT match the exact "admin" carve-out.
        let (ok, _) = can_agents_communicate(&conn, "bob", "admin-helper", false);
        assert!(!ok);
    }

    #[test]
    fn can_agents_communicate_worker_to_worker_denied_when_policy_off() {
        let conn = test_conn();
        project_settings_repository::upsert(
            &conn,
            "config_allow_worker_to_worker",
            "false",
            None,
            false,
            "test",
            NOW,
        )
        .unwrap();
        let (ok, reason) = can_agents_communicate(&conn, "bob", "carol", false);
        assert!(!ok);
        assert!(reason.contains("disabled by policy"));
    }

    #[test]
    fn can_agents_communicate_both_live_agents_permitted() {
        let conn = test_conn();
        seed_agent(&conn, "bob");
        seed_agent(&conn, "carol");
        let (ok, _) = can_agents_communicate(&conn, "bob", "carol", false);
        assert!(ok);
    }

    #[test]
    fn can_agents_communicate_unknown_recipient_denied() {
        let conn = test_conn();
        seed_agent(&conn, "bob");
        let (ok, reason) = can_agents_communicate(&conn, "bob", "ghost", false);
        assert!(!ok);
        assert!(reason.contains("not a currently-active agent"));
    }

    #[test]
    fn check_send_message_permission_rejects_an_over_long_message() {
        let conn = test_conn();
        let long = "x".repeat(4001);
        let denial = check_send_message_permission(&conn, &worker("bob"), "admin", &long, "text");
        assert!(matches!(denial, Some(ToolResult::Invalid { .. })));
    }

    #[test]
    fn check_send_message_permission_rejects_a_worker_stop_command() {
        let conn = test_conn();
        let denial = check_send_message_permission(
            &conn,
            &worker("bob"),
            "admin",
            "stop please",
            "stop_command",
        );
        assert!(matches!(denial, Some(ToolResult::PermissionDenied { .. })));
    }

    #[test]
    fn check_send_message_permission_permits_a_worker_messaging_admin() {
        let conn = test_conn();
        let denial = check_send_message_permission(&conn, &worker("bob"), "admin", "help", "text");
        assert!(denial.is_none());
    }

    fn send_args<'a>(recipient_id: &'a str, message_content: &'a str) -> SendMessageArgs<'a> {
        SendMessageArgs {
            recipient_id,
            message_content,
            message_type: "text",
            priority: "normal",
            subject: None,
            parent_message_id: None,
            now: NOW,
        }
    }

    #[test]
    fn send_agent_message_persists_a_row_and_reports_sent() {
        let conn = test_conn();
        let outcome =
            send_agent_message(&conn, &worker("bob"), send_args("admin", "help please")).unwrap();
        assert!(matches!(outcome, SendOutcome::Sent { .. }));
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM agent_messages", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 1);
    }

    #[test]
    fn send_agent_message_denied_send_writes_no_row() {
        let conn = test_conn();
        project_settings_repository::upsert(
            &conn,
            "config_allow_worker_to_worker",
            "false",
            None,
            false,
            "test",
            NOW,
        )
        .unwrap();
        let outcome =
            send_agent_message(&conn, &worker("bob"), send_args("admin", "help please")).unwrap();
        assert!(matches!(outcome, SendOutcome::Denied(_)));
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM agent_messages", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 0);
    }

    #[test]
    fn send_agent_message_a_reply_stores_null_subject_regardless_of_explicit_subject() {
        let conn = test_conn();
        seed_agent(&conn, "bob");
        let root_outcome =
            send_agent_message(&conn, &worker("bob"), send_args("admin", "root message")).unwrap();
        let SendOutcome::Sent {
            message_id: root_id,
        } = root_outcome
        else {
            panic!("expected the root send to succeed");
        };
        let reply_outcome = send_agent_message(
            &conn,
            &worker("bob"),
            SendMessageArgs {
                subject: Some("this subject must be dropped"),
                parent_message_id: Some(&root_id),
                ..send_args("admin", "a reply")
            },
        )
        .unwrap();
        let SendOutcome::Sent {
            message_id: reply_id,
        } = reply_outcome
        else {
            panic!("expected the reply to succeed");
        };
        let row = message_repository::get_by_id(&conn, &reply_id)
            .unwrap()
            .unwrap();
        assert_eq!(row.subject, None, "a reply must never persist a subject");
        assert_eq!(row.parent_message_id.as_deref(), Some(root_id.as_str()));
    }

    #[test]
    fn send_agent_message_a_root_message_persists_its_explicit_subject() {
        let conn = test_conn();
        let outcome = send_agent_message(
            &conn,
            &worker("bob"),
            SendMessageArgs {
                subject: Some("a real subject"),
                ..send_args("admin", "hello")
            },
        )
        .unwrap();
        let SendOutcome::Sent { message_id } = outcome else {
            panic!("expected the send to succeed");
        };
        let row = message_repository::get_by_id(&conn, &message_id)
            .unwrap()
            .unwrap();
        assert_eq!(row.subject.as_deref(), Some("a real subject"));
    }

    #[test]
    fn send_agent_message_unknown_recipient_is_reported_distinctly() {
        let conn = test_conn();
        // Admin sender bypasses the active-recipient check
        // (`can_agents_communicate`'s admin-bypass clause), reaching
        // the repository's own unknown-recipient rejection.
        let admin = Principal {
            kind: PrincipalKind::AgentBearer,
            user_id: None,
            agent_id: Some("admin".to_string()),
            project_name: None,
            project_role: None,
            agent_role: None,
            can_wake_loop: false,
            source_token: None,
            capabilities: Capabilities::from_iter([]),
        };
        let outcome = send_agent_message(&conn, &admin, send_args("ghost", "hi")).unwrap();
        assert!(matches!(outcome, SendOutcome::RecipientNotFound(id) if id == "ghost"));
    }
}
