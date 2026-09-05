//! CoNexus always-on URL-keyed router (Phase E2 PR 1/24: scaffold).
//!
//! Port target: `agent_mcp/cli.py::router_cmd` + `agent_mcp/router/
//! app.py`. This first slice is deliberately minimal -- matching
//! Phase 0's own "green from day one, zero production risk" precedent
//! -- and boots real, but serves only a trivial health check. No
//! proxying, no auth, no dashboard. Every real router responsibility
//! (proxy, SSO, session, project lifecycle, group management) is a
//! later, dedicated PR per the migration plan's own 24-PR breakdown.
//!
//! The CLI flag surface matches `router_cmd`'s real flags exactly (the
//! invocation contract `agent-mcp-router`'s nix wrapper already
//! generates), even though this slice only wires up `--port` -- the
//! rest are accepted now so `nix/packages.nix`'s eventual
//! `conexus-router` wrapper needs no flag-shape changes as later PRs
//! wire each one up, matching `conexus-backend`'s own PR1 precedent
//! for `--debug`/`--advanced`/etc.
//!
//! Guiding Principle 2 / ADR-0020: this binary crate depends on
//! `conexus-auth`/`conexus-core` only -- never `conexus-tools` or
//! `conexus-mcp` (see `Cargo.toml`'s own comment). The router treating
//! a per-project backend as an opaque process is enforced at compile
//! time here, not just by convention.

mod identity;
mod mount;
mod orchestrator;
mod path_policy;
mod project_registry;

use std::net::SocketAddr;

use anyhow::{Context, Result};
use axum::routing::get;
use axum::Router;
use clap::Parser;

/// `agent-mcp router`'s real flag surface (`router_cmd` in
/// `agent_mcp/cli.py`). Flags beyond `--port` are accepted-but-unused
/// in this first slice -- each is wired up by the PR that ports the
/// subsystem it configures (see this module's own doc).
#[derive(Parser, Debug)]
#[command(name = "conexus-router")]
struct Cli {
    /// Port to listen on for the URL-keyed router.
    #[arg(long, default_value = "1337")]
    port: u16,

    /// JSON file mapping project name -> workspace path. Wired up by
    /// the `conexus-router-project-registry` PR.
    #[arg(long)]
    projects_file: Option<std::path::PathBuf>,

    /// Directory containing per-project Unix-domain backend sockets.
    /// Wired up by the `conexus-router-orchestrator`/`-proxy-core` PRs.
    #[arg(long)]
    sock_dir: Option<std::path::PathBuf>,

    /// Directory holding the Next.js static dashboard export. Wired up
    /// by the `conexus-router-app-wiring` PR.
    #[arg(long)]
    dashboard_dir: Option<std::path::PathBuf>,

    /// Base URL the router is reachable at. Wired up alongside the
    /// project-lifecycle REST surface.
    #[arg(long)]
    external_url: Option<String>,

    /// Idle seconds before stopping an inactive backend. Wired up by
    /// the `conexus-router-orchestrator` PR.
    #[arg(long, default_value = "14400")]
    idle_sec: u64,

    /// Optional installer.sh.in template path. Wired up alongside the
    /// project-lifecycle REST surface.
    #[arg(long)]
    installer_template: Option<std::path::PathBuf>,

    /// Optional README rendered to HTML. Wired up alongside app-wiring.
    #[arg(long)]
    readme_html: Option<std::path::PathBuf>,

    /// Runtime dashboard asset-prefix substitution. Wired up by the
    /// `conexus-router-headers-misc` PR.
    #[arg(long)]
    asset_prefix: Option<String>,

    /// Single-tenant mode project name (ADR-0008). Wired up by the
    /// `conexus-router-headers-misc` PR.
    #[arg(long)]
    single_tenant: Option<String>,

    /// Single-tenant mode workspace path.
    #[arg(long)]
    single_workspace: Option<std::path::PathBuf>,
}

async fn health() -> &'static str {
    "ok"
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();

    let app = Router::new().route("/health", get(health));
    let addr = SocketAddr::from(([0, 0, 0, 0], cli.port));
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .with_context(|| format!("bind {addr}"))?;
    eprintln!("conexus-router: listening on {addr} (scaffold -- no proxying yet)");
    axum::serve(listener, app)
        .await
        .context("serve conexus-router")
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
