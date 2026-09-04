//! Port of `agent_mcp/tools/file_management_tools.py` (Phase D5, PR 4)
//! — the two file-claim / file-status tools (`check_file_status`,
//! `update_file_status`). No DB interaction: both manage the
//! process-wide in-memory advisory file map (`g.file_map` in Python,
//! [`conexus_wakeloop::file_map::FileMap`] here — see that module's
//! doc for why it's an ADVISORY, non-durable map).
//!
//! `Requirement::Predicate`, not `Cap`: admission is agent-bearer AND
//! `files.use`-gated (SEC round-2 defense-in-depth). The `kind ==
//! AgentBearer` half is load-bearing, not decorative — the file map
//! keys on `agent_id`, which an operator-session `Principal` doesn't
//! carry, so this tool can't be reduced to `Cap(files.use)` alone
//! (an operator with `files.use` granted would pass the Cap check but
//! have no `agent_id` to key the claim on). Matches Python's own
//! `agent_bearer_with_capability` rationale.
//!
//! Path resolution: Python's `os.path.abspath(os.path.join(agent_wd,
//! filepath_arg))` is a pure LEXICAL normalization (no filesystem
//! access, no symlink resolution, works on a path that doesn't exist)
//! — `std::fs::canonicalize` is the wrong Rust primitive here (it
//! touches disk and errors on a missing path, which is the common
//! case for a not-yet-created file). [`resolve_abs_filepath`]
//! implements the same lexical join-then-normalize by hand.
//!
//! Deliberately NOT ported, with an explicit reason (never a silent
//! drop): the in-memory `log_audit` trail — same precedent as every
//! prior Phase D5 tool (no Rust reader, no durable `agent_actions` row
//! exists here either since Python's own calls write only to the
//! transient trail).

use std::path::{Component, Path, PathBuf};

use conexus_auth::{Requirement, Tool};
use conexus_core::capability::Capability;
use conexus_core::principal::{Principal, PrincipalKind};
use conexus_core::tool_result::ToolResult;
use conexus_db::agent_repository::AgentRepository;
use rusqlite::Connection;
use serde_json::Value;
use tokio::sync::Mutex as AsyncMutex;

use crate::task_tools::str_arg;

fn is_file_capable_agent(principal: Option<&Principal>) -> bool {
    principal.is_some_and(|p| {
        p.kind == PrincipalKind::AgentBearer && p.has_capability(Capability::FilesUse)
    })
}

const CHECK_DENIED: &str =
    "Unauthorized: agent token with files.use capability required to check file status";
const UPDATE_DENIED: &str =
    "Unauthorized: agent token with files.use capability required to update file status";

/// Lexically join `base` with `path` (if `path` isn't already
/// absolute) and normalize `.`/`..` components without touching the
/// filesystem — see this module's doc for why this, not
/// `fs::canonicalize`, is the right primitive.
fn resolve_abs_filepath(base: &str, path: &str) -> String {
    let p = Path::new(path);
    let joined = if p.is_absolute() {
        p.to_path_buf()
    } else {
        Path::new(base).join(p)
    };
    normalize_lexically(&joined).display().to_string()
}

fn normalize_lexically(path: &Path) -> PathBuf {
    let mut out: Vec<Component> = Vec::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => match out.last() {
                Some(Component::Normal(_)) => {
                    out.pop();
                }
                Some(Component::RootDir) => {}
                _ => out.push(component),
            },
            other => out.push(other),
        }
    }
    out.iter().collect()
}

/// The agent's working directory, DB-lookup-then-server-CWD fallback
/// — matches Python's `agent_repo.get_working_directory(...) or
/// os.getcwd()`. Deliberately synchronous (no internal `.await`): an
/// `async fn` holding `&Connection` across even a single yield point
/// makes the caller's whole boxed future `!Send` (`Connection: Send`
/// but not `Sync`) -- the same root cause already documented on
/// `Tool::call`'s own `BoxFuture` doc, avoided here at the source by
/// never making this helper `async` in the first place.
fn working_directory_for(conn: &Connection, agent_id: &str) -> String {
    AgentRepository::get_by_id(conn, agent_id)
        .ok()
        .flatten()
        .map(|r| r.working_directory)
        .filter(|d| !d.is_empty())
        .unwrap_or_else(|| {
            std::env::current_dir()
                .map(|p| p.display().to_string())
                .unwrap_or_default()
        })
}

pub struct CheckFileStatusTool;

impl Tool for CheckFileStatusTool {
    const NAME: &'static str = "check_file_status";
    const REQUIRED: Requirement = Requirement::Predicate {
        check: is_file_capable_agent,
        reason: CHECK_DENIED,
    };
    const DESCRIPTION: &'static str = "Check if a file is currently being used by another \
        agent, based on the server's in-memory file map.";
    // "maxLength": 4096 mirrors conexus_core::schema_limits::PATH_MAX_LEN
    // -- kept as a literal (SCHEMA must be `const`-constructible) and
    // cross-checked against that constant by this module's own test.
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "filepath": {
                "type": "string",
                "description": "Path to the file to check (can be relative to agent's CWD or absolute)",
                "maxLength": 4096
            }
        },
        "required": ["filepath"],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        _now: &'a str,
        ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let Some(filepath_arg) = str_arg(arguments, "filepath").filter(|s| !s.is_empty())
            else {
                return ToolResult::Invalid {
                    field: Some("filepath".to_string()),
                    message: "filepath is required and must be a string.".to_string(),
                };
            };

            let agent_id = principal.and_then(|p| p.agent_id.as_deref()).unwrap_or("");

            let resolved = {
                let guard = conn.lock().await;
                let wd = working_directory_for(&guard, agent_id);
                resolve_abs_filepath(&wd, &filepath_arg)
            };

            match ctx.file_map.get(&resolved) {
                Some(entry) => {
                    let message = if entry.agent_id == agent_id {
                        format!(
                            "File '{filepath_arg}' (resolved: {resolved}) is currently being \
                             used by YOU ({agent_id}) since {}. Status: {}",
                            entry.timestamp, entry.status
                        )
                    } else {
                        format!(
                            "File '{filepath_arg}' (resolved: {resolved}) is currently being \
                             used by agent '{}' since {}. Status: {}",
                            entry.agent_id, entry.timestamp, entry.status
                        )
                    };
                    ToolResult::Ok {
                        data: Some(serde_json::json!({
                            "filepath": resolved,
                            "original_path": filepath_arg,
                            "in_use": true,
                            "agent_id": entry.agent_id,
                            "status": entry.status,
                            "timestamp": entry.timestamp,
                        })),
                        message: Some(message),
                    }
                }
                None => ToolResult::Ok {
                    data: Some(serde_json::json!({
                        "filepath": resolved,
                        "original_path": filepath_arg,
                        "in_use": false,
                    })),
                    message: Some(format!(
                        "File '{filepath_arg}' (resolved: {resolved}) is not currently being \
                         used by any agent according to the file map."
                    )),
                },
            }
        })
    }
}

const VALID_STATUSES: &[&str] = &["editing", "reading", "reviewing", "released"];

pub struct UpdateFileStatusTool;

impl Tool for UpdateFileStatusTool {
    const NAME: &'static str = "update_file_status";
    const REQUIRED: Requirement = Requirement::Predicate {
        check: is_file_capable_agent,
        reason: UPDATE_DENIED,
    };
    const DESCRIPTION: &'static str = "Update the status of a file in the server's in-memory \
        map (e.g., claim for editing, reading, or release it).";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "filepath": {
                "type": "string",
                "description": "Path to the file to update (can be relative or absolute)",
                "maxLength": 4096
            },
            "status": {
                "type": "string",
                "description": "New status for the file.",
                "enum": ["editing", "reading", "reviewing", "released"]
            }
        },
        "required": ["filepath", "status"],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        now: &'a str,
        ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let Some(filepath_arg) = str_arg(arguments, "filepath").filter(|s| !s.is_empty())
            else {
                return ToolResult::Invalid {
                    field: Some("filepath".to_string()),
                    message: "filepath is required and must be a string.".to_string(),
                };
            };
            let Some(new_status) = str_arg(arguments, "status").filter(|s| !s.is_empty()) else {
                return ToolResult::Invalid {
                    field: Some("status".to_string()),
                    message: "status is required and must be a string.".to_string(),
                };
            };
            if !VALID_STATUSES.contains(&new_status.as_str()) {
                return ToolResult::Invalid {
                    field: Some("status".to_string()),
                    message: format!(
                        "Invalid status: '{new_status}'. Must be one of: {}",
                        VALID_STATUSES.join(", ")
                    ),
                };
            }

            let agent_id = principal.and_then(|p| p.agent_id.as_deref()).unwrap_or("");

            let resolved = {
                let guard = conn.lock().await;
                let wd = working_directory_for(&guard, agent_id);
                resolve_abs_filepath(&wd, &filepath_arg)
            };

            // Ownership gate: only the holder may mutate a foreign-held
            // lock -- claim OR release. SEC-R20/AZ-R20-1: a non-holder
            // release must be denied the SAME way a non-holder claim
            // is (no "release" carve-out) -- see this module's doc /
            // the Python source comment this ports verbatim.
            if let Some(holder) = ctx.file_map.get(&resolved) {
                if holder.agent_id != agent_id {
                    return ToolResult::Conflict {
                        reason: format!(
                            "File '{filepath_arg}' (resolved: {resolved}) is already claimed by \
                             agent '{}' since {} (status: {}). This is an advisory lock with no \
                             auto-expiry -- it frees only when '{}' releases it. Use \
                             check_file_status for current holder/timestamp, coordinate with \
                             that agent, or work on a different file.",
                            holder.agent_id, holder.timestamp, holder.status, holder.agent_id
                        ),
                    };
                }
            }

            if new_status == "released" {
                if ctx.file_map.release(&resolved) {
                    ToolResult::Ok {
                        data: Some(serde_json::json!({
                            "filepath": resolved,
                            "original_path": filepath_arg,
                            "status": "released",
                        })),
                        message: Some(format!(
                            "File '{filepath_arg}' (resolved: {resolved}) has been released."
                        )),
                    }
                } else {
                    // Untracked path: releasing it is idempotent from
                    // the caller's point of view, not an error --
                    // matches Python's informational-success framing.
                    ToolResult::Ok {
                        data: Some(serde_json::json!({
                            "filepath": resolved,
                            "original_path": filepath_arg,
                            "in_use": false,
                        })),
                        message: Some(format!(
                            "File '{filepath_arg}' (resolved: {resolved}) was not found in the \
                             active file map (already considered released or never tracked)."
                        )),
                    }
                }
            } else {
                ctx.file_map.claim(&resolved, agent_id, &new_status, now);
                ToolResult::Ok {
                    data: Some(serde_json::json!({
                        "filepath": resolved,
                        "original_path": filepath_arg,
                        "agent_id": agent_id,
                        "status": new_status,
                    })),
                    message: Some(format!(
                        "File '{filepath_arg}' (resolved: {resolved}) is now registered to \
                         agent '{agent_id}' with status '{new_status}'."
                    )),
                }
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_auth::ToolCallContext;
    use conexus_core::capability::Capabilities;
    use conexus_core::schema_limits::PATH_MAX_LEN;
    use conexus_db::agent_repository::NewAgent;
    use conexus_db::schema::init_schema;
    use conexus_wakeloop::file_map::FileMap;
    use conexus_wakeloop::waiter_registry::WaiterRegistry;
    use std::collections::HashSet;

    fn agent_principal(agent_id: &str) -> Principal {
        Principal {
            kind: PrincipalKind::AgentBearer,
            user_id: None,
            agent_id: Some(agent_id.to_string()),
            project_name: None,
            project_role: None,
            agent_role: Some(conexus_core::capability::AgentRole::Worker),
            can_wake_loop: true,
            source_token: None,
            capabilities: Capabilities::Set(HashSet::from([Capability::FilesUse])),
        }
    }

    async fn setup() -> AsyncMutex<Connection> {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        AgentRepository::create(
            &conn,
            NewAgent {
                token: "tok",
                agent_id: "alice",
                created_at: "2026-06-01T00:00:00Z",
                status: "active",
                current_task: None,
                working_directory: "/home/alice/repo",
                color: None,
                agent_role: "worker",
            },
        )
        .unwrap();
        AgentRepository::create(
            &conn,
            NewAgent {
                token: "tok2",
                agent_id: "bob",
                created_at: "2026-06-01T00:00:00Z",
                status: "active",
                current_task: None,
                working_directory: "/home/bob/repo",
                color: None,
                agent_role: "worker",
            },
        )
        .unwrap();
        AsyncMutex::new(conn)
    }

    #[test]
    fn resolve_abs_filepath_normalizes_a_relative_path_lexically() {
        assert_eq!(
            resolve_abs_filepath("/home/alice/repo", "src/../lib/./main.rs"),
            "/home/alice/repo/lib/main.rs"
        );
    }

    #[test]
    fn resolve_abs_filepath_leaves_an_absolute_path_untouched() {
        assert_eq!(
            resolve_abs_filepath("/home/alice/repo", "/etc/hosts"),
            "/etc/hosts"
        );
    }

    #[test]
    fn a_predicate_denies_a_non_agent_bearer_principal() {
        let mut op = agent_principal("alice");
        op.kind = PrincipalKind::ForwardingHeader;
        assert!(!is_file_capable_agent(Some(&op)));
    }

    #[test]
    fn a_predicate_denies_missing_principal() {
        assert!(!is_file_capable_agent(None));
    }

    #[tokio::test]
    async fn check_reports_a_free_file_as_not_in_use() {
        let conn = setup().await;
        let principal = agent_principal("alice");
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map);
        let result = CheckFileStatusTool::call(
            Some(&principal),
            &serde_json::json!({"filepath": "main.rs"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &ctx,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let data = data.unwrap();
        assert_eq!(data["in_use"], false);
        assert_eq!(data["filepath"], "/home/alice/repo/main.rs");
    }

    #[tokio::test]
    async fn claim_then_check_reports_in_use_by_self() {
        let conn = setup().await;
        let principal = agent_principal("alice");
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map);
        UpdateFileStatusTool::call(
            Some(&principal),
            &serde_json::json!({"filepath": "main.rs", "status": "editing"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &ctx,
        )
        .await;
        let result = CheckFileStatusTool::call(
            Some(&principal),
            &serde_json::json!({"filepath": "main.rs"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &ctx,
        )
        .await;
        let ToolResult::Ok { data, message } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let data = data.unwrap();
        assert_eq!(data["in_use"], true);
        assert_eq!(data["agent_id"], "alice");
        assert!(message.unwrap().contains("YOU"));
    }

    #[tokio::test]
    async fn a_foreign_holder_claim_attempt_is_a_conflict_not_a_permission_denial() {
        let conn = setup().await;
        let alice = agent_principal("alice");
        let bob = agent_principal("bob");
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map);
        UpdateFileStatusTool::call(
            Some(&alice),
            &serde_json::json!({"filepath": "/shared/main.rs", "status": "editing"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &ctx,
        )
        .await;
        let result = UpdateFileStatusTool::call(
            Some(&bob),
            &serde_json::json!({"filepath": "/shared/main.rs", "status": "reading"}),
            &conn,
            "2026-06-01T00:01:00Z",
            &ctx,
        )
        .await;
        assert!(matches!(result, ToolResult::Conflict { .. }));
    }

    #[tokio::test]
    async fn a_foreign_holder_release_attempt_is_also_a_conflict_not_a_silent_steal() {
        // SEC-R20/AZ-R20-1 regression: a non-holder must not be able to
        // release (and thereby free) another agent's claim.
        let conn = setup().await;
        let alice = agent_principal("alice");
        let bob = agent_principal("bob");
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map);
        UpdateFileStatusTool::call(
            Some(&alice),
            &serde_json::json!({"filepath": "/shared/main.rs", "status": "editing"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &ctx,
        )
        .await;
        let result = UpdateFileStatusTool::call(
            Some(&bob),
            &serde_json::json!({"filepath": "/shared/main.rs", "status": "released"}),
            &conn,
            "2026-06-01T00:01:00Z",
            &ctx,
        )
        .await;
        assert!(matches!(result, ToolResult::Conflict { .. }));
        // The claim must still be there afterward -- prove it wasn't
        // silently dropped despite the denial.
        assert!(file_map.get("/shared/main.rs").is_some());
    }

    #[tokio::test]
    async fn the_holder_can_release_their_own_claim() {
        let conn = setup().await;
        let alice = agent_principal("alice");
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map);
        UpdateFileStatusTool::call(
            Some(&alice),
            &serde_json::json!({"filepath": "main.rs", "status": "editing"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &ctx,
        )
        .await;
        let result = UpdateFileStatusTool::call(
            Some(&alice),
            &serde_json::json!({"filepath": "main.rs", "status": "released"}),
            &conn,
            "2026-06-01T00:01:00Z",
            &ctx,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        assert!(file_map.get("/home/alice/repo/main.rs").is_none());
    }

    #[tokio::test]
    async fn releasing_an_untracked_path_is_an_idempotent_ok_not_an_error() {
        let conn = setup().await;
        let alice = agent_principal("alice");
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map);
        let result = UpdateFileStatusTool::call(
            Some(&alice),
            &serde_json::json!({"filepath": "never-claimed.rs", "status": "released"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &ctx,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert_eq!(data.unwrap()["in_use"], false);
    }

    #[tokio::test]
    async fn an_invalid_status_value_is_rejected() {
        let conn = setup().await;
        let alice = agent_principal("alice");
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map);
        let result = UpdateFileStatusTool::call(
            Some(&alice),
            &serde_json::json!({"filepath": "main.rs", "status": "sleeping"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &ctx,
        )
        .await;
        assert!(matches!(result, ToolResult::Invalid { .. }));
    }

    #[tokio::test]
    async fn a_missing_filepath_is_invalid_on_both_tools() {
        let conn = setup().await;
        let alice = agent_principal("alice");
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map);
        let r1 = CheckFileStatusTool::call(
            Some(&alice),
            &serde_json::json!({}),
            &conn,
            "2026-06-01T00:00:00Z",
            &ctx,
        )
        .await;
        assert!(matches!(r1, ToolResult::Invalid { .. }));
        let r2 = UpdateFileStatusTool::call(
            Some(&alice),
            &serde_json::json!({"status": "editing"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &ctx,
        )
        .await;
        assert!(matches!(r2, ToolResult::Invalid { .. }));
    }

    #[test]
    fn schema_max_lengths_match_the_shared_constant() {
        for schema in [CheckFileStatusTool::SCHEMA, UpdateFileStatusTool::SCHEMA] {
            let parsed: Value = serde_json::from_str(schema).unwrap();
            let max_len = parsed["properties"]["filepath"]["maxLength"]
                .as_u64()
                .unwrap();
            assert_eq!(max_len as usize, PATH_MAX_LEN);
        }
    }
}
