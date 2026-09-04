//! The `ServerHandler` surface: `list_tools`/`call_tool` wired to
//! `conexus_tools::all_tools()` + `conexus_auth::dispatch`.
//!
//! Structurally mirrors the sibling repos' own shipping pattern
//! (`pikvm_mcp_server`'s `server.rs`) -- hand-written `ServerHandler`
//! impl, no `#[tool_router]`/macro auto-collection, per the migration
//! plan's explicit choice.
//!
//! Per-request identity: an axum middleware (`crate::auth_gate`)
//! resolves the caller's [`Principal`] BEFORE rmcp ever sees the
//! request (mirrors Python's `AuthHeaderMiddleware`'s `/mcp`-gating
//! responsibility -- there is no anonymous path onto `/mcp`) and
//! stashes it into the request's `http::request::Parts::extensions`.
//! rmcp threads that same `Parts` into `RequestContext::extensions`
//! for every JSON-RPC request on the session (confirmed directly
//! against the vendored `rmcp` 3.1.4 source, not assumed) -- so
//! `call_tool` reads it back via `context.extensions.get::<Parts>()`
//! rather than re-deriving it from headers a second time.

use std::sync::Arc;

use rmcp::model::{
    CallToolRequestParams, CallToolResponse, CallToolResult, ContentBlock, Implementation,
    ListToolsResult, PaginatedRequestParams, ProgressNotificationParam, ProgressToken,
    ProtocolVersion, ServerCapabilities, ServerInfo, Tool,
};
use rmcp::service::{Peer, RequestContext};
use rmcp::{ErrorData as McpError, RoleServer, ServerHandler};

use conexus_auth::{BoxFuture, ProgressSink, ToolCallContext};
use conexus_core::principal::Principal;
use conexus_core::tool_result::ToolResult;
use conexus_wakeloop::waiter_registry::WaiterRegistry;
use tokio::sync::Mutex as AsyncMutex;

use crate::auth_gate::ResolvedPrincipal;

/// Shared, cheap-to-clone state every per-session [`ConexusServer`]
/// wraps. A single connection behind an async mutex -- SQLite is
/// single-writer regardless of driver, and this is a low-throughput
/// per-project backend, not a high-QPS service (matches the migration
/// plan's own "run blocking rusqlite calls" data-layer framing; the
/// `spawn_blocking` wrapping that framing also calls for is a
/// documented future refinement, not a correctness requirement at
/// this project-settings-only traffic volume -- revisit if Phase D3's
/// wake-loop raises the DB call rate enough to matter).
pub struct SharedState {
    pub conn: AsyncMutex<rusqlite::Connection>,
    /// `None` when `--forwarding-hmac-in` was unset/unreadable/empty
    /// (the dormant-key state -- see `crate::boot::
    /// load_forwarding_hmac_key`). Read by `crate::auth_gate`, not
    /// this module.
    pub forwarding_hmac_key: Option<Vec<u8>>,
    /// The per-agent `wait_for_events` waiter registry -- one instance
    /// per backend process (this project's single-writer-DB scope),
    /// shared by every session's `ConexusServer` the same way `conn`
    /// is.
    pub waiter_registry: WaiterRegistry,
}

/// [`ProgressSink`] backed by a real MCP [`Peer`]/[`ProgressToken`]
/// pair -- the only place in the workspace that bridges `conexus-auth`
/// (which stays `rmcp`-free) to the real transport. Constructed fresh
/// per `call_tool` invocation; cheap (`Peer` is a cheap `Clone`).
struct PeerProgressSink {
    peer: Peer<RoleServer>,
    progress_token: ProgressToken,
}

impl ProgressSink for PeerProgressSink {
    fn notify_progress<'a>(&'a self, progress: f64) -> BoxFuture<'a, bool> {
        Box::pin(async move {
            self.peer
                .notify_progress(ProgressNotificationParam::new(
                    self.progress_token.clone(),
                    progress,
                ))
                .await
                .is_ok()
        })
    }
}

/// A per-request [`conexus_auth::PolicySource`] snapshot: the real
/// `config_*` overrides a `Requirement::Policy`-gated tool's OWN
/// declared `keys` resolve to in `project_settings`, read through one
/// short-lived lock BEFORE `dispatch` runs (never held across it --
/// see `call_tool`'s own comment on why).
///
/// Closes a gap flagged repeatedly through Phase D ("`PolicySource`
/// stays deferred... no real `Policy`-gated tool exists yet to need
/// it") and left as `NoPolicyOverrides` until now: `update_task_status`/
/// `update_task` (Phase D4, PR 5) are the first real `Policy`-gated
/// tools, so a project operator's `config_allow_worker_update_own_status`
/// toggle must actually be read, not silently ignored in favor of the
/// `Requirement`'s hardcoded `default` forever.
struct SnapshotPolicySource(std::collections::HashMap<&'static str, bool>);

impl SnapshotPolicySource {
    async fn resolve(
        conn: &AsyncMutex<rusqlite::Connection>,
        required: &conexus_auth::Requirement,
    ) -> Self {
        let conexus_auth::Requirement::Policy { keys, .. } = required else {
            // Every other Requirement variant never calls
            // `PolicySource::get_bool` at all (see `Requirement::check`) --
            // no point paying for a lock on a tool that isn't
            // Policy-gated.
            return SnapshotPolicySource(std::collections::HashMap::new());
        };
        let guard = conn.lock().await;
        let mut map = std::collections::HashMap::new();
        for key in *keys {
            if let Some(value) =
                conexus_db::project_settings_repository::get_bool_override(&guard, key)
            {
                map.insert(*key, value);
            }
        }
        SnapshotPolicySource(map)
    }
}

impl conexus_auth::PolicySource for SnapshotPolicySource {
    fn get_bool(&self, key: &str) -> Option<bool> {
        self.0.get(key).copied()
    }
}

#[derive(Clone)]
pub struct ConexusServer {
    shared: Arc<SharedState>,
}

impl ConexusServer {
    pub fn new(shared: Arc<SharedState>) -> Self {
        Self { shared }
    }
}

fn principal_from_context(context: &RequestContext<RoleServer>) -> Option<Principal> {
    context
        .extensions
        .get::<axum::http::request::Parts>()
        .and_then(|parts| parts.extensions.get::<ResolvedPrincipal>())
        .map(|p| p.0.clone())
}

fn tool_result_to_call_tool_result(result: &ToolResult) -> CallToolResult {
    let content: Vec<ContentBlock> = result
        .render_as_text_content()
        .into_iter()
        .map(ContentBlock::text)
        .collect();
    let mut call_result = if result.is_error() {
        CallToolResult::error(content)
    } else {
        CallToolResult::success(content)
    };
    // Carry `Ok`'s JSON payload as structured content too -- lets a
    // client that wants the machine-readable shape skip re-parsing
    // the text block, without changing what the text rendering says.
    if let ToolResult::Ok { data, .. } = result {
        call_result.structured_content = data.clone();
    }
    call_result
}

impl ServerHandler for ConexusServer {
    fn get_info(&self) -> ServerInfo {
        ServerInfo::new(ServerCapabilities::builder().enable_tools().build())
            .with_server_info(Implementation::new(
                "conexus-backend",
                env!("CARGO_PKG_VERSION"),
            ))
            .with_protocol_version(ProtocolVersion::LATEST)
    }

    async fn list_tools(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, McpError> {
        // Every tool in the catalogue is listed regardless of the
        // caller's capabilities -- matches Python's `mcp_list_tools_
        // handler` (visibility filtering by role happens in
        // `access.py::TOOL_ACCESS` for the WORKER-facing tool
        // subset, a mechanism not yet ported; the 3 project_settings
        // tools are all operator-only and Python lists them
        // unconditionally too).
        let tools = conexus_tools::all_tools()
            .iter()
            .map(|descriptor| {
                let schema = descriptor
                    .parsed_schema()
                    .as_object()
                    .cloned()
                    .unwrap_or_default();
                Tool::new(descriptor.name, descriptor.description, schema)
            })
            .collect();
        Ok(ListToolsResult {
            tools,
            ..Default::default()
        })
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, McpError> {
        let name = request.name.to_string();
        let Some(descriptor) = conexus_tools::all_tools().iter().find(|t| t.name == name) else {
            return Err(McpError::invalid_params(
                format!("Unknown tool: {name}"),
                None,
            ));
        };

        // The auth_gate middleware already rejected any request with
        // no admitted identity before rmcp ever routed it here -- a
        // missing Principal at this point means the extensions chain
        // broke somewhere, not a real anonymous caller. Fail closed
        // (PermissionDenied), never treat it as an implicit `None`
        // that a Public-requirement tool might wave through.
        let Some(principal) = principal_from_context(&context) else {
            return Ok(
                tool_result_to_call_tool_result(&ToolResult::PermissionDenied {
                    reason: "Unauthorized: no resolved identity for this request".to_string(),
                })
                .into(),
            );
        };

        let arguments = request
            .arguments
            .map(serde_json::Value::Object)
            .unwrap_or(serde_json::Value::Null);

        let now = chrono::Utc::now().to_rfc3339();

        // Only `call_tool` holds the real `RequestContext` -- extract
        // the MCP-transport facts a tool might need (currently just
        // `wait_for_events`) once, here, and hand them down through
        // `ToolCallContext` rather than smuggling them through
        // `arguments`. `client_info()`/`get_progress_token()` are both
        // read directly off `context`, never re-derived from raw
        // headers a second time (see this module's own doc on that
        // convention for `Principal`).
        let progress_token = context.meta.get_progress_token();
        let client_info = context.client_info();
        let sink = progress_token.clone().map(|token| PeerProgressSink {
            peer: context.peer.clone(),
            progress_token: token,
        });
        let ctx = ToolCallContext {
            progress_token_present: progress_token.is_some(),
            client_name: client_info.as_ref().map(|i| i.name.as_str()),
            progress_sink: sink.as_ref().map(|s| s as &dyn ProgressSink),
            waiter_registry: &self.shared.waiter_registry,
        };

        // `dispatch`/`Tool::call` now lock `shared.conn` themselves
        // (see `conexus_auth::tool`'s module doc for why they take
        // `&Mutex<Connection>`, not an already-locked guard) -- don't
        // pre-lock here, or a tool needing the connection AND an
        // internal `.await` (Phase D2's `ask_project_rag`) would
        // deadlock against its own already-held guard. The
        // `SnapshotPolicySource` build below is the ONE exception: it
        // takes and releases its own short-lived lock BEFORE calling
        // `dispatch`, specifically so it never overlaps the tool's own
        // lock (seq: lock+read+unlock, THEN dispatch).
        let policy_source =
            SnapshotPolicySource::resolve(&self.shared.conn, &descriptor.required).await;
        let result = conexus_auth::dispatch(
            descriptor,
            Some(&principal),
            &policy_source,
            &arguments,
            &self.shared.conn,
            &now,
            &ctx,
        )
        .await;
        Ok(tool_result_to_call_tool_result(&result).into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_core::tool_result::ToolResult;

    #[test]
    fn ok_result_renders_as_success_with_structured_content() {
        let result = ToolResult::Ok {
            data: Some(serde_json::json!({"a": 1})),
            message: Some("done".to_string()),
        };
        let call_result = tool_result_to_call_tool_result(&result);
        assert_eq!(call_result.is_error, Some(false));
        assert_eq!(
            call_result.structured_content,
            Some(serde_json::json!({"a": 1}))
        );
    }

    #[test]
    fn failed_result_renders_as_error_with_no_structured_content() {
        let result = ToolResult::Failed {
            message: "internal db error with a leaked path".to_string(),
        };
        let call_result = tool_result_to_call_tool_result(&result);
        assert_eq!(call_result.is_error, Some(true));
        assert_eq!(call_result.structured_content, None);
        // SEC-R8-1: Failed's rendered text must be the static generic
        // string, never the internal message verbatim.
        let rendered = call_result
            .content
            .iter()
            .filter_map(|c| c.as_text().map(|t| t.text.clone()))
            .collect::<Vec<_>>()
            .join(" ");
        assert!(!rendered.contains("leaked path"));
    }

    fn test_conn() -> AsyncMutex<rusqlite::Connection> {
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        conexus_db::schema::init_schema(&conn).unwrap();
        AsyncMutex::new(conn)
    }

    #[tokio::test]
    async fn snapshot_policy_source_is_empty_for_a_non_policy_requirement() {
        let conn = test_conn();
        let required = conexus_auth::Requirement::Cap {
            cap: conexus_core::capability::Capability::TasksView,
            reason: None,
        };
        let source = SnapshotPolicySource::resolve(&conn, &required).await;
        assert_eq!(
            conexus_auth::PolicySource::get_bool(&source, "config_allow_worker_update_own_status"),
            None
        );
    }

    #[tokio::test]
    async fn snapshot_policy_source_reads_a_real_project_settings_override() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            conexus_db::project_settings_repository::upsert(
                &guard,
                "config_allow_worker_update_own_status",
                "false",
                None,
                false,
                "operator",
                "2026-01-01T00:00:00Z",
            )
            .unwrap();
        }
        let required = conexus_auth::Requirement::Policy {
            keys: &["config_allow_worker_update_own_status"],
            default: true,
        };
        let source = SnapshotPolicySource::resolve(&conn, &required).await;
        assert_eq!(
            conexus_auth::PolicySource::get_bool(&source, "config_allow_worker_update_own_status"),
            Some(false)
        );
    }

    #[tokio::test]
    async fn snapshot_policy_source_has_no_override_when_no_row_exists() {
        let conn = test_conn();
        let required = conexus_auth::Requirement::Policy {
            keys: &["config_allow_worker_update_own_status"],
            default: true,
        };
        let source = SnapshotPolicySource::resolve(&conn, &required).await;
        assert_eq!(
            conexus_auth::PolicySource::get_bool(&source, "config_allow_worker_update_own_status"),
            None
        );
    }
}
