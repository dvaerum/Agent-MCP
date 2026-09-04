//! MCP resources subsystem (Phase E1 PR B2).
//!
//! Port of `agent_mcp/resources/*` -- the data + auth half of the
//! `resources/list`/`resources/read` MCP surfaces (`main_app.py`'s
//! `mcp_list_resources_handler`/`mcp_read_resource_handler`; wiring
//! into `rmcp`'s `ServerHandler` lives in `conexus-backend::server`,
//! not here). Two per-agent "ambient state" resources, both scoped to
//! the calling bearer's own agent_id:
//!
//! * `agent-mcp://inbox/<agent_id>` -- the same JSON envelope
//!   `wait_for_events`/`fetch_events_since` return, routed through
//!   the identical `conexus_wakeloop::event_feed::assemble_event_feed`
//!   pipeline (Phase D3) so this can never silently diverge the way
//!   Python's original `_collect_events_for` shim once did (that shim
//!   omitted the unassigned-task stream + the merged-boundary clamp;
//!   routing both through the one pipeline owner makes that
//!   divergence unrepresentable here too). `fire_scheduled=false`,
//!   `drain_queue=[]` -- a passive poll, deliberately NOT the same as
//!   `fetch_events_since`'s `fire_scheduled=true`: this resource never
//!   fires scheduled directives/pokes as a side effect of being read.
//! * `agent-mcp://status/<agent_id>` -- ambient counters
//!   (`unread_messages`, `unfinished_tasks`).
//!
//! **Deliberately NOT ported**: the generic `core.registry.
//! Registry[T]`/`core.access.decide()` engine. Only two resources
//! exist and their cross-agent scoping need is identical (own-id-only
//! unless admin) -- a purpose-built [`resolve_read_scope`] replaces
//! both the registry dispatch AND the generic `decide()` seam,
//! matching this migration's own "promote a shared primitive once two
//! call sites need it" precedent, applied in reverse for a two-entry
//! catalog with no callable-visibility need (see `crate::prompts`'
//! own doc for the same call on the prompts side).
//!
//! R21-F4 (preserved bit-for-bit): the admin branch is checked BEFORE
//! resolving the caller's own scope -- every real operator-tier
//! Principal reachable in production carries `agent_id: None` (that's
//! the whole point of admin cross-agent access: an admin reads
//! someone else's state and legitimately has none of their own), so
//! requiring a truthy own-scope first would make the admin branch
//! unreachable for every real production caller.

use conexus_core::principal::{CatalogRole, Principal};
use conexus_db::{message_repository, task_repository};
use conexus_wakeloop::event_feed::{self, UNASSIGNED_TASK_TERMINAL_STATUSES};
use rusqlite::Connection;
use serde_json::json;

pub const INBOX_URI_PREFIX: &str = "agent-mcp://inbox/";
pub const STATUS_URI_PREFIX: &str = "agent-mcp://status/";

/// One `resources/list` catalog entry.
pub struct ResourceListing {
    pub uri: String,
    pub name: String,
    pub description: &'static str,
    pub mime_type: &'static str,
}

/// The resources visible to `agent_id`. Port of
/// `mcp_list_resources_handler`: an unauthenticated caller, or one
/// with no `agent_id` of their own (an operator/forwarding-header
/// Principal), sees an EMPTY list -- there is no "someone else's"
/// resource to browse via `resources/list`; only `resources/read`'s
/// admin branch grants cross-agent access, and only when the caller
/// already knows the URI to ask for.
pub fn list_for(agent_id: Option<&str>) -> Vec<ResourceListing> {
    let Some(agent_id) = agent_id else {
        return Vec::new();
    };
    vec![
        ResourceListing {
            uri: format!("{INBOX_URI_PREFIX}{agent_id}"),
            name: format!("inbox/{agent_id}"),
            description: "Event timeline for this agent — pending messages, \
                broadcasts, and task assignments / changes. JSON envelope: \
                {events: [...], next_cursor: \"<iso-ts>\"}.",
            mime_type: "application/json",
        },
        ResourceListing {
            uri: format!("{STATUS_URI_PREFIX}{agent_id}"),
            name: format!("status/{agent_id}"),
            description: "Ambient counters for this agent: \
                {unread_messages, unfinished_tasks, ...}.",
            mime_type: "application/json",
        },
    ]
}

/// Why a `resources/read` call was refused. Port of
/// `core.access.DenialKind`, narrowed to the two variants this
/// two-entry, all-`visibility="any"` catalog can actually produce (a
/// `not_visible` denial has no Rust analogue -- there is no
/// admin-only resource to port).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReadDenial {
    /// Neither the forwarding header nor the bearer resolved to a
    /// real agent_id.
    Unauthenticated,
    /// A real caller, but neither this URI's owner nor admin.
    OutOfScope,
}

/// The outcome of a `resources/read` call.
pub enum ReadOutcome {
    Ok {
        body: String,
        mime_type: &'static str,
    },
    UnknownUri,
    Denied(ReadDenial),
}

/// Which resource `uri` addresses, and the `agent_id` segment
/// embedded in its path -- `None` for an unrecognized URI.
fn match_uri(uri: &str) -> Option<(&'static str, &str)> {
    if let Some(rest) = uri.strip_prefix(INBOX_URI_PREFIX) {
        Some(("inbox", rest.trim_end_matches('/')))
    } else if let Some(rest) = uri.strip_prefix(STATUS_URI_PREFIX) {
        Some(("status", rest.trim_end_matches('/')))
    } else {
        None
    }
}

/// May `principal` read `uri_agent_id`'s resource? Port of
/// `resolve_agent_id_for_uri`'s authorization half (the URI-parsing
/// half is [`match_uri`]) -- the bearer always wins over whatever
/// agent_id the URI's own path segment names; a mismatch is a
/// denial, never a silent redirect to the caller's own id.
fn resolve_read_scope(uri_agent_id: &str, principal: Option<&Principal>) -> Result<(), ReadDenial> {
    let role = conexus_core::principal::catalog_role(principal);
    // R21-F4: admin bypass MUST precede the own-scope resolution --
    // see module doc.
    if role == CatalogRole::Admin {
        return Ok(());
    }
    let own = principal.and_then(|p| p.agent_id.as_deref());
    let Some(own) = own else {
        return Err(ReadDenial::Unauthenticated);
    };
    if own != uri_agent_id {
        return Err(ReadDenial::OutOfScope);
    }
    Ok(())
}

/// Render `agent_id`'s inbox envelope. Port of
/// `resources/inbox.py::render_inbox` -- see module doc for the
/// `fire_scheduled=false`/`drain_queue=[]` rationale. `since` is
/// exposed for testability; the real `resources/read` wiring always
/// passes `None` (MCP's `resources/read` has no query params, per
/// Python's own docstring).
pub fn render_inbox(
    conn: &Connection,
    agent_id: &str,
    since: Option<&str>,
    now_iso: &str,
) -> rusqlite::Result<String> {
    let assembled = event_feed::assemble_event_feed(
        conn,
        agent_id,
        since,
        now_iso,
        Vec::new(),
        false,
        crate::agent_communication_tools::process_env,
    )?;
    Ok(json!({"events": assembled.events, "next_cursor": assembled.next_cursor}).to_string())
}

/// Render `agent_id`'s ambient status counters. Port of
/// `resources/status.py::render_status`.
pub fn render_status(conn: &Connection, agent_id: &str) -> rusqlite::Result<String> {
    let unread_messages = message_repository::count_unread(conn, agent_id)?;
    let unfinished_tasks = task_repository::count_active_by_assignee(
        conn,
        agent_id,
        &UNASSIGNED_TASK_TERMINAL_STATUSES,
    )?;
    Ok(json!({
        "agent_id": agent_id,
        "unread_messages": unread_messages,
        "unfinished_tasks": unfinished_tasks,
    })
    .to_string())
}

/// Read `uri` on behalf of `principal`. Port of
/// `ResourceRegistry.read` + `resolve_agent_id_for_uri` combined into
/// one call -- resolves the URI, applies the cross-agent scope gate,
/// then renders. Never carries MCP wire types; the caller maps
/// [`ReadOutcome`] onto its own JSON-RPC error codes/text (matching
/// Python's two-distinct-raise-site error contract).
pub fn read(
    conn: &Connection,
    uri: &str,
    principal: Option<&Principal>,
    now_iso: &str,
) -> Result<ReadOutcome, rusqlite::Error> {
    let Some((kind, uri_agent_id)) = match_uri(uri) else {
        return Ok(ReadOutcome::UnknownUri);
    };
    if let Err(denial) = resolve_read_scope(uri_agent_id, principal) {
        return Ok(ReadOutcome::Denied(denial));
    }
    let (body, mime_type) = match kind {
        "inbox" => (
            render_inbox(conn, uri_agent_id, None, now_iso)?,
            "application/json",
        ),
        "status" => (render_status(conn, uri_agent_id)?, "application/json"),
        _ => unreachable!("match_uri only ever returns a kind this match covers"),
    };
    Ok(ReadOutcome::Ok { body, mime_type })
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_core::capability::Capabilities;
    use conexus_core::principal::PrincipalKind;
    use conexus_db::schema::init_schema;
    use std::collections::HashSet;

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
            can_wake_loop: false,
            source_token: Some("tok".to_string()),
            capabilities: Capabilities::Set(HashSet::new()),
        }
    }

    fn admin() -> Principal {
        Principal {
            kind: PrincipalKind::ForwardingHeader,
            user_id: Some("op1".to_string()),
            agent_id: None,
            project_name: None,
            project_role: Some(conexus_core::capability::ProjectRole::Operator),
            agent_role: None,
            can_wake_loop: false,
            source_token: None,
            capabilities: Capabilities::Sysadmin,
        }
    }

    // -- list_for -----------------------------------------------------

    #[test]
    fn list_for_none_is_empty() {
        assert!(list_for(None).is_empty());
    }

    #[test]
    fn list_for_a_real_agent_returns_both_resources_scoped_to_it() {
        let listing = list_for(Some("worker-1"));
        assert_eq!(listing.len(), 2);
        assert_eq!(listing[0].uri, "agent-mcp://inbox/worker-1");
        assert_eq!(listing[1].uri, "agent-mcp://status/worker-1");
    }

    // -- match_uri (indirectly via read) -------------------------------

    #[test]
    fn an_unrecognized_uri_is_unknown() {
        let conn = test_conn();
        let outcome = read(&conn, "agent-mcp://bogus/x", None, "2026-01-01T00:00:00Z").unwrap();
        assert!(matches!(outcome, ReadOutcome::UnknownUri));
    }

    // -- resolve_read_scope --------------------------------------------

    #[test]
    fn no_principal_at_all_is_unauthenticated() {
        assert_eq!(
            resolve_read_scope("worker-1", None),
            Err(ReadDenial::Unauthenticated)
        );
    }

    #[test]
    fn an_operator_with_no_agent_id_is_unauthenticated_when_not_admin() {
        // A viewer-tier operator carries agent_id: None but is NOT
        // admin -- must be denied Unauthenticated, not silently
        // treated as owning every resource.
        let p = Principal {
            kind: PrincipalKind::ForwardingHeader,
            user_id: Some("op1".to_string()),
            agent_id: None,
            project_name: None,
            project_role: Some(conexus_core::capability::ProjectRole::Viewer),
            agent_role: None,
            can_wake_loop: false,
            source_token: None,
            capabilities: Capabilities::Set(HashSet::new()),
        };
        assert_eq!(
            resolve_read_scope("worker-1", Some(&p)),
            Err(ReadDenial::Unauthenticated)
        );
    }

    #[test]
    fn a_worker_reading_their_own_resource_is_allowed() {
        let p = worker("worker-1");
        assert_eq!(resolve_read_scope("worker-1", Some(&p)), Ok(()));
    }

    #[test]
    fn a_worker_reading_a_foreign_resource_is_out_of_scope() {
        let p = worker("worker-1");
        assert_eq!(
            resolve_read_scope("worker-2", Some(&p)),
            Err(ReadDenial::OutOfScope)
        );
    }

    #[test]
    fn r21_f4_an_admin_with_no_agent_id_of_their_own_may_read_any_resource() {
        // The exact regression this test name references: an admin
        // Principal carries agent_id: None in every real production
        // shape, so the admin branch must be checked BEFORE the
        // own-scope resolution, or this would wrongly deny.
        let p = admin();
        assert_eq!(resolve_read_scope("worker-1", Some(&p)), Ok(()));
        assert_eq!(resolve_read_scope("worker-2", Some(&p)), Ok(()));
    }

    // -- render_status --------------------------------------------------

    #[test]
    fn render_status_counts_unread_messages_and_unfinished_tasks() {
        let conn = test_conn();
        conn.execute(
            "INSERT INTO agents (token, agent_id, created_at, status, working_directory, agent_role) \
             VALUES ('t1', 'worker-1', '2026-01-01T00:00:00Z', 'active', '/tmp', 'worker')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO agent_messages (message_id, sender_id, recipient_id, message_content, timestamp, read) \
             VALUES ('m1', 'admin', 'worker-1', 'hi', '2026-01-01T00:00:00Z', 0)",
            [],
        )
        .unwrap();
        task_repository::create(
            &conn,
            task_repository::NewTask {
                task_id: Some("t1"),
                title: "do a thing",
                description: None,
                assigned_to: Some("worker-1"),
                created_by: "admin",
                status: "in_progress",
                priority: "medium",
                parent_task: None,
                child_tasks: None,
                depends_on_tasks: None,
                notes: None,
                now: "2026-01-01T00:00:00Z",
            },
        )
        .unwrap();
        task_repository::create(
            &conn,
            task_repository::NewTask {
                task_id: Some("t2"),
                title: "done already",
                description: None,
                assigned_to: Some("worker-1"),
                created_by: "admin",
                status: "completed",
                priority: "medium",
                parent_task: Some("t1"),
                child_tasks: None,
                depends_on_tasks: None,
                notes: None,
                now: "2026-01-01T00:00:00Z",
            },
        )
        .unwrap();

        let body = render_status(&conn, "worker-1").unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(parsed["agent_id"], "worker-1");
        assert_eq!(parsed["unread_messages"], 1);
        assert_eq!(parsed["unfinished_tasks"], 1);
    }

    #[test]
    fn render_status_is_zero_for_an_agent_with_no_activity() {
        let conn = test_conn();
        let body = render_status(&conn, "nobody").unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(parsed["unread_messages"], 0);
        assert_eq!(parsed["unfinished_tasks"], 0);
    }

    // -- render_inbox -----------------------------------------------------

    #[test]
    fn render_inbox_returns_a_well_formed_empty_envelope_with_no_activity() {
        let conn = test_conn();
        let body = render_inbox(&conn, "worker-1", None, "2026-01-01T00:00:00Z").unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert!(parsed["events"].as_array().unwrap().is_empty());
        assert!(parsed["next_cursor"].is_string());
    }

    // -- read (end-to-end) ------------------------------------------------

    #[test]
    fn read_denies_a_foreign_workers_status_resource() {
        let conn = test_conn();
        let p = worker("worker-2");
        let outcome = read(
            &conn,
            "agent-mcp://status/worker-1",
            Some(&p),
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        assert!(matches!(
            outcome,
            ReadOutcome::Denied(ReadDenial::OutOfScope)
        ));
    }

    #[test]
    fn read_serves_a_workers_own_status_resource() {
        let conn = test_conn();
        let p = worker("worker-1");
        let outcome = read(
            &conn,
            "agent-mcp://status/worker-1",
            Some(&p),
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        match outcome {
            ReadOutcome::Ok { body, mime_type } => {
                assert_eq!(mime_type, "application/json");
                assert!(body.contains("worker-1"));
            }
            _ => panic!("expected Ok"),
        }
    }

    #[test]
    fn read_serves_an_admins_cross_agent_inbox_read() {
        let conn = test_conn();
        let p = admin();
        let outcome = read(
            &conn,
            "agent-mcp://inbox/worker-1",
            Some(&p),
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        assert!(matches!(outcome, ReadOutcome::Ok { .. }));
    }
}
