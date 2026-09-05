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

use std::path::PathBuf;
use std::sync::Arc;

use rmcp::model::{
    CallToolRequestParams, CallToolResponse, CallToolResult, ContentBlock, GetPromptRequestParams,
    GetPromptResponse, GetPromptResult, Implementation, InitializeRequestParams, InitializeResult,
    ListPromptsResult, ListResourcesResult, ListToolsResult, PaginatedRequestParams,
    ProgressNotificationParam, ProgressToken, Prompt, PromptArgument, PromptMessage,
    ProtocolVersion, ReadResourceRequestParams, ReadResourceResponse, ReadResourceResult, Resource,
    ResourceContents, Role, ServerCapabilities, ServerInfo, Tool,
};
use rmcp::service::{Peer, RequestContext};
use rmcp::{ErrorData as McpError, RoleServer, ServerHandler};

use conexus_auth::{BoxFuture, ProgressSink, ToolCallContext};
use conexus_core::principal::Principal;
use conexus_core::tool_result::ToolResult;
use conexus_wakeloop::file_map::FileMap;
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
    /// The process-wide in-memory advisory file-claim map --
    /// `file_management_tools.py`'s `g.file_map`. Same one-per-process
    /// scope as `waiter_registry` above.
    pub file_map: FileMap,
    /// The `--project-dir` this process was started with -- threaded
    /// into every `ToolCallContext` (Phase D5, `backup_project_context`
    /// is the first real consumer).
    pub project_dir: PathBuf,
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

/// Run a named tool from a NON-MCP caller (the `/api` REST surface) --
/// the same tool-lookup + `SnapshotPolicySource` + `dispatch` dance
/// `call_tool` runs for `/mcp`, factored out so both surfaces share
/// one dispatch mechanism rather than drifting apart. Deliberately a
/// SEPARATE function rather than a refactor of `call_tool` itself: the
/// two `ToolCallContext`s are shaped differently by construction (REST
/// has no `rmcp::RequestContext` to source `client_name`/
/// `progress_sink` from -- it fills those `None`, matching what a
/// non-MCP caller genuinely doesn't have) and `call_tool` is
/// already-tested, already-live production code this port doesn't
/// touch just to share ~15 lines.
///
/// Returns `Err` only for "no such tool" (a REST handler maps that to
/// its own 404, not a `ToolResult` shape) -- capability/policy denial
/// and every other outcome are ordinary `ToolResult` variants, exactly
/// like the MCP path.
pub(crate) async fn dispatch_rest_tool(
    shared: &Arc<SharedState>,
    tool_name: &str,
    arguments: serde_json::Value,
    principal: Option<&Principal>,
) -> Result<ToolResult, ()> {
    let Some(descriptor) = conexus_tools::all_tools()
        .iter()
        .find(|t| t.name == tool_name)
    else {
        return Err(());
    };
    let now = chrono::Utc::now().to_rfc3339();
    let ctx = ToolCallContext {
        progress_token_present: false,
        client_name: None,
        progress_sink: None,
        waiter_registry: &shared.waiter_registry,
        file_map: &shared.file_map,
        project_dir: &shared.project_dir,
    };
    let policy_source = SnapshotPolicySource::resolve(&shared.conn, &descriptor.required).await;
    Ok(conexus_auth::dispatch(
        descriptor,
        principal,
        &policy_source,
        &arguments,
        &shared.conn,
        &now,
        &ctx,
    )
    .await)
}

impl ServerHandler for ConexusServer {
    fn get_info(&self) -> ServerInfo {
        ServerInfo::new(
            ServerCapabilities::builder()
                .enable_tools()
                .enable_prompts()
                .enable_resources()
                .build(),
        )
        .with_server_info(Implementation::new(
            "conexus-backend",
            env!("CARGO_PKG_VERSION"),
        ))
        .with_protocol_version(ProtocolVersion::LATEST)
    }

    // Overrides `ServerHandler`'s provided default (which just calls
    // `get_info()`) so per-request `instructions` can be appended --
    // `get_info(&self)` has no `RequestContext` parameter, so it can
    // never see the caller's resolved `Principal`. This duplicates
    // rmcp 3.1.4's own tiny protocol-negotiation fallback
    // (`service::server::negotiate_protocol_version`, `pub(crate)` in
    // the SDK and therefore uncallable from here) rather than
    // reimplementing the rest of `initialize` -- see this fn's own
    // regression test pinning that echo-or-fallback behavior.
    async fn initialize(
        &self,
        request: InitializeRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<InitializeResult, McpError> {
        context.peer.set_peer_info(request.clone());
        let mut info = self.get_info();
        info.protocol_version = if self
            .supported_protocol_versions()
            .contains(&request.protocol_version)
        {
            request.protocol_version
        } else {
            info.protocol_version
        };
        if let Some(extra) =
            crate::instructions::render_all(principal_from_context(&context).as_ref())
        {
            let mut text = info.instructions.take().unwrap_or_default();
            text.push_str(extra);
            info.instructions = Some(text);
        }
        Ok(info)
    }

    // Port of `mcp_list_tools_handler` (Phase E1 PR C): filtered by
    // the caller's `CatalogRole` per `conexus_tools::access`, closing
    // the gap this fn's own doc comment used to flag as un-ported.
    async fn list_tools(
        &self,
        _request: Option<PaginatedRequestParams>,
        context: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, McpError> {
        let role = conexus_core::principal::catalog_role(principal_from_context(&context).as_ref());
        let guard = self.shared.conn.lock().await;
        let tools = conexus_tools::all_tools()
            .iter()
            .filter(|descriptor| {
                let tier = conexus_tools::access::access_tier(descriptor);
                conexus_tools::access::is_visible_to_role(tier, role, &guard)
            })
            .map(|descriptor| {
                let schema = descriptor
                    .parsed_schema()
                    .as_object()
                    .cloned()
                    .unwrap_or_default();
                Tool::new(descriptor.name, descriptor.description, schema)
            })
            .collect();
        drop(guard);
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
            file_map: &self.shared.file_map,
            project_dir: &self.shared.project_dir,
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

    // Port of `mcp_list_prompts_handler` (Phase E1 PR B1) -- the
    // Prompt Book catalogue, filtered to what `role` may see.
    async fn list_prompts(
        &self,
        _request: Option<PaginatedRequestParams>,
        context: RequestContext<RoleServer>,
    ) -> Result<ListPromptsResult, McpError> {
        let role = conexus_core::principal::catalog_role(principal_from_context(&context).as_ref());
        let prompts = conexus_tools::prompts::list_visible(role)
            .into_iter()
            .map(|entry| {
                // Python always passes `arguments=args` as a (possibly
                // empty) list, never `None` -- match that on the wire
                // rather than omitting the field for a zero-variable
                // prompt.
                let arguments = entry
                    .variables
                    .iter()
                    .map(|v| {
                        PromptArgument::new(v.name.clone())
                            .with_description(v.description.clone())
                            .with_required(v.required)
                    })
                    .collect();
                Prompt::new(
                    entry.id.clone(),
                    Some(entry.description.clone()),
                    Some(arguments),
                )
                .with_title(entry.title.clone())
            })
            .collect();
        Ok(ListPromptsResult {
            prompts,
            ..Default::default()
        })
    }

    // Port of `mcp_get_prompt_handler` (Phase E1 PR B1). Error codes
    // match Python's `_PromptValueError`/`_PromptPermissionError`
    // exactly: an unknown id is INVALID_PARAMS, a visibility denial is
    // INTERNAL_ERROR (FLAG-R17-1 -- an `McpError` is the only way a
    // handler's error carries a spec-valid JSON-RPC code through the
    // SDK's dispatcher, in Python and here alike).
    async fn get_prompt(
        &self,
        request: GetPromptRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<GetPromptResponse, McpError> {
        let role = conexus_core::principal::catalog_role(principal_from_context(&context).as_ref());
        let entry = conexus_tools::prompts::get(&request.name).ok_or_else(|| {
            McpError::invalid_params(format!("Unknown prompt: {}", request.name), None)
        })?;
        let arguments: std::collections::HashMap<String, serde_json::Value> = request
            .arguments
            .map(|obj| obj.into_iter().collect())
            .unwrap_or_default();
        let rendered = conexus_tools::prompts::render(entry, &arguments, role).map_err(|_| {
            McpError::internal_error(
                format!(
                    "Prompt {:?} is not visible to role {:?}",
                    entry.id,
                    role.as_str()
                ),
                None,
            )
        })?;
        Ok(
            GetPromptResult::new(vec![PromptMessage::new_text(Role::User, rendered)])
                .with_description(entry.description.clone())
                .into(),
        )
    }

    // Port of `mcp_list_resources_handler` (Phase E1 PR B2) -- the two
    // per-agent ambient-state resources, scoped to the caller's own
    // agent_id (empty for an unauthenticated caller or an
    // operator/forwarding-header Principal, which carries none).
    async fn list_resources(
        &self,
        _request: Option<PaginatedRequestParams>,
        context: RequestContext<RoleServer>,
    ) -> Result<ListResourcesResult, McpError> {
        let principal = principal_from_context(&context);
        let agent_id = principal.as_ref().and_then(|p| p.agent_id.as_deref());
        let resources = conexus_tools::resources::list_for(agent_id)
            .into_iter()
            .map(|entry| {
                Resource::new(entry.uri, entry.name)
                    .with_description(entry.description)
                    .with_mime_type(entry.mime_type)
            })
            .collect();
        Ok(ListResourcesResult {
            resources,
            ..Default::default()
        })
    }

    // Port of `mcp_read_resource_handler` (Phase E1 PR B2). Error
    // codes match `ResourceReadError`'s contract exactly: an unknown
    // URI is INVALID_PARAMS, every denial kind (not-visible /
    // unauthenticated / out-of-scope) is INTERNAL_ERROR.
    async fn read_resource(
        &self,
        request: ReadResourceRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<ReadResourceResponse, McpError> {
        let principal = principal_from_context(&context);
        let now = chrono::Utc::now().to_rfc3339();
        let outcome = {
            let guard = self.shared.conn.lock().await;
            conexus_tools::resources::read(&guard, &request.uri, principal.as_ref(), &now)
        }
        .map_err(|_| {
            McpError::internal_error(format!("failed to read resource {:?}", request.uri), None)
        })?;
        match outcome {
            conexus_tools::resources::ReadOutcome::Ok { body, mime_type } => {
                Ok(ReadResourceResult::new(vec![
                    ResourceContents::text(body, request.uri).with_mime_type(mime_type)
                ])
                .into())
            }
            conexus_tools::resources::ReadOutcome::UnknownUri => Err(McpError::invalid_params(
                format!("Unknown resource URI: {}", request.uri),
                None,
            )),
            conexus_tools::resources::ReadOutcome::Denied(_) => Err(McpError::internal_error(
                "Unauthorized: callers may only read their own inbox / status resources"
                    .to_string(),
                None,
            )),
        }
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
