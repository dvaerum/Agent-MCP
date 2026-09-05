//! CoNexus always-on URL-keyed router. Port target: `agent_mcp/
//! cli.py::router_cmd` + `agent_mcp/router/app.py`.
//!
//! **App-wiring status (Phase E2, `conexus-router-app-wiring`
//! breakdown)**: this is PR23 step 1 (`conexus-router-shared-state`)
//! -- real `RouterState` construction, the fail-closed
//! `boot::assert_startup_safe` guard, the router DB boot sequence,
//! and background reaper/reconciliation task spawns are now real. The
//! HTTP surface itself is still just `GET /health` -- every real
//! route (the MCP/API proxy, login/setup, the admin REST surface) is
//! deliberately deferred to the LATER steps of the same breakdown
//! (steps 2-10: security-middleware, mcp-api-proxy, login-setup,
//! revalidation-fusion, lifecycle-rest, users-groups-rest, dashboard-
//! static, mount-aliases, sso-admin-config), matching this migration's
//! own "smallest, most foundational first" discipline for large
//! phases. Every decision function these later steps will wire is
//! already built and tested across `conexus-router/src/*.rs`.
//!
//! Guiding Principle 2 / ADR-0020: this binary crate depends on
//! `conexus-auth`/`conexus-core` only -- never `conexus-tools` or
//! `conexus-mcp` (see `Cargo.toml`'s own comment). The router treating
//! a per-project backend as an opaque process is enforced at compile
//! time here, not just by convention.

mod admin_group_capabilities;
mod admin_group_members;
mod admin_groups;
mod admin_project_memberships;
mod admin_users_gate;
mod admin_users_users;
mod asset_prefix;
mod boot;
mod client_disconnect;
mod identity;
mod lifecycle;
mod login;
mod mcp_handler;
mod middleware;
mod mount;
mod orchestrator;
mod path_policy;
mod project_gate;
mod project_reads;
mod project_registry;
mod project_rename;
mod project_teardown;
mod proxy_client;
mod proxy_core;
mod rate_limit;
mod security_headers;
mod session_gate;
mod single_tenant;
mod sso;
mod state;

use std::net::SocketAddr;

use anyhow::{Context, Result};
use axum::routing::get;
use axum::Router;
use clap::Parser;

/// `agent-mcp router`'s real flag surface (`router_cmd` in
/// `agent_mcp/cli.py`). `--host`/router-DB-path/single-tenant-safety
/// knobs have NO CLI flag in Python either (env-var-only:
/// `AGENT_MCP_ROUTER_HOST`/`AGENT_MCP_ROUTER_DB`/
/// `AGENT_MCP_ALLOW_INSECURE_BIND`/`AGENT_MCP_REQUIRE_SECURE_COOKIES`)
/// -- this struct deliberately does not invent flags Python's own CLI
/// doesn't have; `main()` reads those straight from the process
/// environment via `boot`, matching the real interface exactly.
#[derive(Parser, Debug)]
#[command(name = "conexus-router")]
struct Cli {
    /// Port to listen on for the URL-keyed router.
    #[arg(long, default_value = "1337")]
    port: u16,

    /// JSON file mapping project name -> workspace path.
    #[arg(long)]
    projects_file: Option<std::path::PathBuf>,

    /// Directory containing per-project Unix-domain backend sockets.
    #[arg(long)]
    sock_dir: Option<std::path::PathBuf>,

    /// Directory holding the Next.js static dashboard export. Wired up
    /// by the `conexus-router-dashboard-static` app-wiring step.
    #[arg(long)]
    dashboard_dir: Option<std::path::PathBuf>,

    /// Base URL the router is reachable at.
    #[arg(long)]
    external_url: Option<String>,

    /// Idle seconds before stopping an inactive backend.
    #[arg(long, default_value = "14400")]
    idle_sec: u64,

    /// Optional installer.sh.in template path. Wired up alongside the
    /// `client_config`/`installer` routes (currently deferred
    /// indefinitely -- confirmed inert token plumbing in production).
    #[arg(long)]
    installer_template: Option<std::path::PathBuf>,

    /// Optional README rendered to HTML. Wired up by the
    /// `conexus-router-dashboard-static` app-wiring step.
    #[arg(long)]
    readme_html: Option<std::path::PathBuf>,

    /// Runtime dashboard asset-prefix substitution.
    #[arg(long)]
    asset_prefix: Option<String>,

    /// Single-tenant mode project name (ADR-0008).
    #[arg(long)]
    single_tenant: Option<String>,

    /// Single-tenant mode workspace path.
    #[arg(long)]
    single_workspace: Option<std::path::PathBuf>,
}

async fn health() -> &'static str {
    "ok"
}

/// Port of `router_cmd`'s own `--projects-file` default resolution:
/// `$XDG_CONFIG_HOME/agent-mcp/projects.local.json`, falling back to
/// `$HOME/.config/agent-mcp/projects.local.json`. No new dependency
/// (a `dirs`/`home`-style crate) for a two-env-var lookup Python
/// itself does inline.
fn default_projects_file(get_env: impl Fn(&str) -> Option<String>) -> std::path::PathBuf {
    let config_home = get_env("XDG_CONFIG_HOME")
        .filter(|s| !s.is_empty())
        .or_else(|| {
            get_env("HOME")
                .filter(|s| !s.is_empty())
                .map(|h| format!("{h}/.config"))
        })
        .unwrap_or_else(|| ".config".to_string());
    std::path::PathBuf::from(config_home)
        .join("agent-mcp")
        .join("projects.local.json")
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    let get_env = |key: &str| std::env::var(key).ok();

    let host = boot::resolve_bind_host(get_env);
    if let Err(msg) = boot::assert_startup_safe(cli.single_tenant.as_deref(), &host, get_env) {
        anyhow::bail!(msg);
    }
    if let Some(warning) = boot::secure_cookie_warning(&host, get_env) {
        eprintln!("conexus-router: WARNING: {warning}");
    }

    let db_path = boot::router_db_path(get_env);
    let conn = boot::open_and_init_router_db(&db_path)
        .with_context(|| format!("boot router database at {}", db_path.display()))?;

    let projects_file = cli
        .projects_file
        .clone()
        .unwrap_or_else(|| default_projects_file(get_env));
    let registry = project_registry::ProjectRegistry::new(projects_file);

    let rate_limit_config = rate_limit::RateLimitConfig::resolve_from_process_env();
    let ensure_config = orchestrator::ensure::EnsureConfig::from_env(get_env);
    let max_streams_per_agent = get_env("AGENT_MCP_MAX_SSE_PER_AGENT")
        .and_then(|v| v.parse().ok())
        .unwrap_or(4);
    let max_streams_global = get_env("AGENT_MCP_MAX_SSE_GLOBAL")
        .and_then(|v| v.parse().ok())
        .unwrap_or(64);

    let sock_dir = cli
        .sock_dir
        .clone()
        .or_else(|| get_env("AGENT_MCP_SOCK_DIR").map(std::path::PathBuf::from))
        .context(
            "--sock-dir (or $AGENT_MCP_SOCK_DIR) is required: the router can't resolve any \
             project's backend socket without it",
        )?;

    let state = std::sync::Arc::new(state::RouterState::new(
        conn,
        registry,
        rate_limit_config,
        ensure_config,
        state::RouterStateConfig {
            sock_dir,
            dashboard_dir: cli.dashboard_dir.clone(),
            external_url: cli.external_url.clone(),
            idle_sec: cli.idle_sec,
            asset_prefix: cli.asset_prefix.clone(),
            single_tenant_name: cli.single_tenant.clone(),
            single_tenant_workspace: cli.single_workspace.clone(),
            max_streams_per_agent,
            max_streams_global,
        },
    ));

    spawn_background_tasks(std::sync::Arc::clone(&state));

    // Layer order matches Python's real app.py middleware chain
    // exactly: security headers outermost (touches every response,
    // even one an inner layer rejects) -> rate-limit -> empty-users-
    // redirect -> session-gate (innermost, closest to the real
    // handler). `Router::layer` wraps the CURRENT service, so the
    // LAST `.layer()` call becomes the OUTERMOST layer -- these four
    // are added in REVERSE of the request's own traversal order.
    let app = Router::new()
        .route("/health", get(health))
        .layer(axum::middleware::from_fn_with_state(
            std::sync::Arc::clone(&state),
            middleware::session_gate_layer,
        ))
        .layer(axum::middleware::from_fn_with_state(
            std::sync::Arc::clone(&state),
            middleware::empty_users_redirect_layer,
        ))
        .layer(axum::middleware::from_fn_with_state(
            std::sync::Arc::clone(&state),
            middleware::rate_limit_layer,
        ))
        .layer(axum::middleware::from_fn_with_state(
            std::sync::Arc::clone(&state),
            middleware::security_headers_layer,
        ));

    let addr = SocketAddr::from(([0, 0, 0, 0], cli.port));
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .with_context(|| format!("bind {addr}"))?;
    eprintln!("conexus-router: listening on {addr} (app-wiring in progress -- no proxying yet)");
    axum::serve(
        listener,
        app.into_make_service_with_connect_info::<SocketAddr>(),
    )
    .await
    .context("serve conexus-router")
}

/// Port of `app.py`'s `on_startup` reconciliation pass + the two
/// background sweep loops (`_start_reaper_task`/
/// `_start_alias_reaper_task`) -- `tokio::spawn`'d fire-and-forget
/// tasks, matching every other real-async-loop precedent in this
/// crate (`orchestrator::ensure`/`reaper` themselves; no supervisor/
/// restart-on-panic wrapper exists anywhere in this workspace to
/// reuse, so none is added here either).
fn spawn_background_tasks(state: std::sync::Arc<state::RouterState>) {
    let reconcile_state = std::sync::Arc::clone(&state);
    tokio::spawn(async move {
        orchestrator::reaper::reconcile_on_startup(
            &reconcile_state.runtime,
            std::time::SystemTime::now(),
            &reconcile_state.ensure_config.systemctl_program,
            reconcile_state.ensure_config.systemctl_mode,
            reconcile_state.ensure_config.systemctl_timeout,
        )
        .await;
    });

    let reaper_state = std::sync::Arc::clone(&state);
    tokio::spawn(async move {
        let idle = std::time::Duration::from_secs(reaper_state.idle_sec);
        loop {
            tokio::time::sleep(std::time::Duration::from_secs(60)).await;
            orchestrator::reaper::reaper_tick(
                &reaper_state.runtime,
                &reaper_state.registry,
                idle,
                std::time::SystemTime::now(),
                &reaper_state.ensure_config.systemctl_program,
                reaper_state.ensure_config.systemctl_mode,
                reaper_state.ensure_config.systemctl_timeout,
            )
            .await;
        }
    });

    let alias_reaper_state = std::sync::Arc::clone(&state);
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(std::time::Duration::from_secs(60)).await;
            orchestrator::reaper::alias_reaper_tick(
                &alias_reaper_state.registry,
                chrono::Utc::now(),
            );
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn health_returns_ok() {
        assert_eq!(health().await, "ok");
    }

    #[test]
    fn cli_parses_the_real_router_cmd_flag_surface() {
        let cli = Cli::parse_from([
            "conexus-router",
            "--port",
            "9999",
            "--projects-file",
            "/tmp/projects.local.json",
            "--sock-dir",
            "/tmp/sockets",
            "--dashboard-dir",
            "/tmp/dashboard",
            "--external-url",
            "https://example.test",
            "--idle-sec",
            "60",
            "--asset-prefix",
            "/agent-mcp/__dashboard",
            "--single-tenant",
            "demo",
            "--single-workspace",
            "/tmp/demo",
        ]);
        assert_eq!(cli.port, 9999);
        assert_eq!(cli.idle_sec, 60);
        assert_eq!(cli.single_tenant.as_deref(), Some("demo"));
    }

    #[test]
    fn cli_defaults_match_python_router_cmd() {
        let cli = Cli::parse_from(["conexus-router"]);
        assert_eq!(cli.port, 1337);
        assert_eq!(cli.idle_sec, 14400);
        assert!(cli.projects_file.is_none());
        assert!(cli.single_tenant.is_none());
    }
}
