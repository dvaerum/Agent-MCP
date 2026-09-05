//! `RouterState` -- the shared, cheap-to-clone (behind `Arc`) bundle
//! every axum handler reaches into. Phase E2, `conexus-router-shared-
//! state` (PR23 step 1 of the app-wiring breakdown). Structurally
//! mirrors `conexus-backend::server::SharedState`'s own precedent
//! (one connection behind an async mutex -- SQLite is single-writer
//! regardless of driver, and this is a low-throughput admin surface,
//! not a high-QPS service), materially bigger because the router owns
//! several more subsystems the per-project backend never needed
//! (a project registry with its own flock-based locking, orthogonal
//! to the DB mutex; per-project runtime/lifecycle state; the proxy's
//! streaming-admission control; rate limiting).
//!
//! Every field here already has a real, tested Rust implementation
//! ported in an earlier PR this phase -- this struct's whole job is
//! bundling them, not inventing new logic (`conn`/`registry`/
//! `runtime`/`stream_caps`/`asset_prefix_cache`/`rate_limit`/
//! `ensure_config`/`mcp_handler_config`/`session_gate_config` are each
//! one line of construction). Real axum route registration (which
//! reads this state) is later PRs in the same breakdown, not this
//! one.
//!
//! Most fields have no reader yet (route registration is a later
//! step) -- same "helpers ahead of their first consumer" precedent as
//! `mount.rs`/`path_policy.rs`/`identity.rs` when they first landed.

#![allow(dead_code)]

use std::path::PathBuf;

use tokio::sync::Mutex as AsyncMutex;

use crate::asset_prefix::AssetPrefixCache;
use crate::mcp_handler::McpHandlerConfig;
use crate::orchestrator::ensure::EnsureConfig;
use crate::orchestrator::runtime::RuntimeStore;
use crate::project_registry::ProjectRegistry;
use crate::proxy_core::StreamCapRegistry;
use crate::rate_limit::{RateLimitConfig, RateLimitState};
use crate::session_gate::SessionGateConfig;

/// Every CLI-flag-derived setting `RouterState` needs at construction
/// time -- kept as its own struct (rather than passing 8 positional
/// args) since `main.rs`'s real `Cli` struct is the one real producer
/// and this is what makes the mapping from flags to state legible at
/// the call site.
#[derive(Debug, Clone)]
pub struct RouterStateConfig {
    pub sock_dir: PathBuf,
    pub dashboard_dir: Option<PathBuf>,
    pub external_url: Option<String>,
    pub idle_sec: u64,
    pub asset_prefix: Option<String>,
    pub single_tenant_name: Option<String>,
    pub single_tenant_workspace: Option<PathBuf>,
    pub max_streams_per_agent: u32,
    pub max_streams_global: u32,
    /// Parent dir for a new project's workspace when the caller omits
    /// one -- port of `app.py`'s `DEFAULT_WORKSPACE_PARENT`
    /// (`$AGENT_MCP_DEFAULT_WORKSPACE`, default `~/.local/share/
    /// agent-mcp/projects`). Needed by `project_gate::
    /// decide_create_project`/`lifecycle::workspace_label`.
    pub default_workspace_parent: PathBuf,
    /// Agent-token directory -- port of `admin_api.py::_token_dir()`
    /// (`$AGENT_MCP_TOKENS_DIR`, default `~/.config/agent-mcp/tokens`).
    /// `None` only in a test/dev context with no real `$HOME` to fall
    /// back to; a production boot always resolves a concrete path (the
    /// env-var-presence branch this mirrors is itself a fix for a real
    /// bug -- `_token_dir()`'s own doc: an unset var must NOT resolve
    /// to `Path("")`, which is truthy and silently pointed at the
    /// process CWD). Needed by `project_rename::finish_rename_project`
    /// and (once fixed) `project_teardown::finish_delete_project`.
    pub token_dir: Option<PathBuf>,
}

pub struct RouterState {
    pub conn: AsyncMutex<rusqlite::Connection>,
    pub registry: ProjectRegistry,
    pub runtime: RuntimeStore,
    /// `Arc`-wrapped (rather than bare, like every other field here)
    /// because `mcp_handler::backend_mcp_handler`/`backend_api_handler`
    /// take it by `&Arc<StreamCapRegistry>` -- their own admission-
    /// guard type (`StreamCapGuard`) is held across the request's
    /// whole streaming lifetime and needs to outlive a single
    /// borrowed reference to `RouterState`.
    pub stream_caps: std::sync::Arc<StreamCapRegistry>,
    pub asset_prefix_cache: AssetPrefixCache,
    pub rate_limit_config: RateLimitConfig,
    pub rate_limit_state: std::sync::Mutex<RateLimitState>,
    pub ensure_config: EnsureConfig,
    pub mcp_handler_config: McpHandlerConfig,
    pub session_gate_config: SessionGateConfig,
    pub sock_dir: PathBuf,
    pub dashboard_dir: Option<PathBuf>,
    pub external_url: Option<String>,
    pub idle_sec: u64,
    pub asset_prefix: Option<String>,
    pub single_tenant_workspace: Option<PathBuf>,
    pub default_workspace_parent: PathBuf,
    pub token_dir: Option<PathBuf>,
}

impl RouterState {
    /// Assemble the full bundle from an already-open router DB
    /// connection, an already-constructed `ProjectRegistry`, and the
    /// CLI-derived config -- everything else is a fresh, empty
    /// in-memory structure (this is process startup; no prior state
    /// to recover, matching every already-ported subsystem's own
    /// `::new()`).
    pub fn new(
        conn: rusqlite::Connection,
        registry: ProjectRegistry,
        rate_limit_config: RateLimitConfig,
        ensure_config: EnsureConfig,
        config: RouterStateConfig,
    ) -> Self {
        let rate_limit_state = RateLimitState::new(&rate_limit_config);
        Self {
            conn: AsyncMutex::new(conn),
            registry,
            runtime: RuntimeStore::new(),
            stream_caps: std::sync::Arc::new(StreamCapRegistry::new(
                config.max_streams_per_agent,
                config.max_streams_global,
            )),
            asset_prefix_cache: AssetPrefixCache::new(),
            rate_limit_config,
            rate_limit_state: std::sync::Mutex::new(rate_limit_state),
            ensure_config,
            mcp_handler_config: McpHandlerConfig {
                single_tenant_name: config.single_tenant_name.clone(),
                ..McpHandlerConfig::default()
            },
            session_gate_config: SessionGateConfig {
                single_tenant_name: config.single_tenant_name,
                // Port of `path_policy.py`'s own `public_route`
                // registration for `GET /agent-mcp/api/router/health`
                // (`admin_api.py:1723-1728`) -- the ONE lifecycle-rest
                // route with no session requirement at all.
                extra_exact_paths: vec!["/agent-mcp/api/router/health".to_string()],
            },
            sock_dir: config.sock_dir,
            dashboard_dir: config.dashboard_dir,
            external_url: config.external_url,
            idle_sec: config.idle_sec,
            asset_prefix: config.asset_prefix,
            single_tenant_workspace: config.single_tenant_workspace,
            default_workspace_parent: config.default_workspace_parent,
            token_dir: config.token_dir,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_db::schema::init_router_schema;

    fn test_config() -> RouterStateConfig {
        RouterStateConfig {
            sock_dir: PathBuf::from("/tmp/agent-mcp-sockets"),
            dashboard_dir: None,
            external_url: None,
            idle_sec: 14400,
            asset_prefix: None,
            single_tenant_name: None,
            single_tenant_workspace: None,
            max_streams_per_agent: 4,
            max_streams_global: 64,
            default_workspace_parent: PathBuf::from("/tmp/agent-mcp-projects"),
            token_dir: None,
        }
    }

    #[test]
    fn new_assembles_every_subsystem_from_cli_config() {
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        init_router_schema(&conn).unwrap();
        let dir = tempfile::TempDir::new().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let state = RouterState::new(
            conn,
            registry,
            RateLimitConfig::resolve_from_process_env(),
            EnsureConfig::from_env(|_| None),
            test_config(),
        );
        assert_eq!(state.sock_dir, PathBuf::from("/tmp/agent-mcp-sockets"));
        assert_eq!(state.idle_sec, 14400);
        assert!(state.mcp_handler_config.single_tenant_name.is_none());
        assert!(state.session_gate_config.single_tenant_name.is_none());
    }

    #[test]
    fn new_threads_single_tenant_name_into_both_configs() {
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        init_router_schema(&conn).unwrap();
        let dir = tempfile::TempDir::new().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let mut config = test_config();
        config.single_tenant_name = Some("demo".to_string());
        let state = RouterState::new(
            conn,
            registry,
            RateLimitConfig::resolve_from_process_env(),
            EnsureConfig::from_env(|_| None),
            config,
        );
        assert_eq!(
            state.mcp_handler_config.single_tenant_name.as_deref(),
            Some("demo")
        );
        assert_eq!(
            state.session_gate_config.single_tenant_name.as_deref(),
            Some("demo")
        );
    }

    #[test]
    fn new_makes_the_health_route_public_and_threads_workspace_and_token_dirs() {
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        init_router_schema(&conn).unwrap();
        let dir = tempfile::TempDir::new().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let mut config = test_config();
        config.default_workspace_parent = dir.path().join("workspaces");
        config.token_dir = Some(dir.path().join("tokens"));
        let state = RouterState::new(
            conn,
            registry,
            RateLimitConfig::resolve_from_process_env(),
            EnsureConfig::from_env(|_| None),
            config,
        );
        assert!(state
            .session_gate_config
            .extra_exact_paths
            .iter()
            .any(|p| p == "/agent-mcp/api/router/health"));
        assert_eq!(
            state.default_workspace_parent,
            dir.path().join("workspaces")
        );
        assert_eq!(
            state.token_dir.as_deref(),
            Some(dir.path().join("tokens")).as_deref()
        );
    }
}
