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
    ListToolsResult, PaginatedRequestParams, ProtocolVersion, ServerCapabilities, ServerInfo, Tool,
};
use rmcp::service::RequestContext;
use rmcp::{ErrorData as McpError, RoleServer, ServerHandler};

use conexus_core::principal::Principal;
use conexus_core::tool_result::ToolResult;
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
        let conn = self.shared.conn.lock().await;
        let result = conexus_auth::dispatch(
            descriptor,
            Some(&principal),
            &conexus_auth::NoPolicyOverrides,
            &arguments,
            &conn,
            &now,
        );
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
}
