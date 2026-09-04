//! CoNexus per-project MCP backend (Phase D1 step 3).
//!
//! Boot sequence + CLI flag surface are a faithful port of `agent_mcp/
//! cli.py`'s `server` command + `agent_mcp/app/server_lifecycle.py`'s
//! steps 1-2 -- the exact invocation contract
//! `nix/packages.nix`'s wrapper already generates for the Python
//! binary (`--uds <sock> --project-dir <path> --forwarding-hmac-in
//! <path> --no-tui`, with `--transport sse` always added) so a
//! `backend_impl` flip needs no wrapper changes on either side.
//!
//! NOT yet ported in this first slice (see the migration plan's Phase
//! D1 "Next step" tracking): loading agent/task state into memory at
//! boot (nothing in `conexus-tools` needs an in-memory mirror the way
//! Python's caches do -- every repository call reads the DB directly),
//! `--debug`/`--advanced`/`--no-index` (RAG-indexing flags, Phase D2
//! territory), and `--port`/host:port serving (this binary only
//! serves over `--uds`, matching every REAL deployment path -- the
//! host:port fallback is a local-dev convenience Python's CLI offers
//! that has no `conexus@<name>.service` caller to replicate for yet).

mod auth_gate;
mod boot;
mod principal_resolve;
mod server;
mod uds;

use std::path::PathBuf;
use std::sync::Arc;

use anyhow::{Context, Result};
use axum::middleware;
use axum::Router;
use clap::Parser;
use rmcp::transport::streamable_http_server::session::local::LocalSessionManager;
use rmcp::transport::streamable_http_server::tower::{
    StreamableHttpServerConfig, StreamableHttpService,
};

use server::{ConexusServer, SharedState};

/// `agent-mcp server`'s flag surface, the subset this binary actually
/// serves (see the module doc for what's deliberately not ported yet).
#[derive(Parser, Debug)]
#[command(name = "conexus-backend")]
struct Cli {
    /// Unix domain socket path to listen on.
    #[arg(long)]
    uds: PathBuf,

    /// Transport type for MCP communication. Only "sse" (Streamable
    /// HTTP) is implemented -- accepted as a flag for CLI-surface
    /// compatibility with the wrapper that always passes it; any
    /// other value is a hard error.
    #[arg(long, default_value = "sse")]
    transport: String,

    /// Project directory. The `.agent` folder is created/used here.
    #[arg(long)]
    project_dir: PathBuf,

    /// Read the per-project HMAC key (raw bytes) for verifying the
    /// router's signed forwarding header.
    #[arg(long)]
    forwarding_hmac_in: Option<PathBuf>,

    /// Accepted for CLI-surface compatibility with the wrapper; this
    /// binary is always headless (no TUI exists to disable).
    #[arg(long)]
    no_tui: bool,
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();

    if cli.transport != "sse" {
        anyhow::bail!(
            "conexus-backend only implements the \"sse\" (Streamable HTTP) transport, got {:?}",
            cli.transport
        );
    }

    boot::ensure_project_dirs(&cli.project_dir)?;
    let conn = boot::open_and_init_db(&cli.project_dir)?;
    let forwarding_hmac_key = boot::load_forwarding_hmac_key(cli.forwarding_hmac_in.as_deref());
    if cli.forwarding_hmac_in.is_some() && forwarding_hmac_key.is_none() {
        eprintln!(
            "conexus-backend: forwarding-hmac key at {:?} was unreadable or empty -- \
             forwarding-header auth stays dormant",
            cli.forwarding_hmac_in
        );
    }

    let shared = Arc::new(SharedState {
        conn: tokio::sync::Mutex::new(conn),
        forwarding_hmac_key,
        waiter_registry: conexus_wakeloop::waiter_registry::WaiterRegistry::new(),
        file_map: conexus_wakeloop::file_map::FileMap::new(),
    });

    let shared_for_factory = shared.clone();
    let mcp_service: StreamableHttpService<ConexusServer, LocalSessionManager> =
        StreamableHttpService::new(
            move || Ok(ConexusServer::new(shared_for_factory.clone())),
            Arc::new(LocalSessionManager::default()),
            // DNS-rebinding `Host` validation exists to protect a TCP
            // listener reachable from a browser's cross-origin
            // request; a Unix socket, gated by filesystem permissions
            // to this uid alone, isn't reachable that way at all --
            // the threat model this default guards against doesn't
            // apply here (unlike pikvm_mcp_server/m365-bridge, which
            // widen the *list* because they DO bind TCP).
            StreamableHttpServerConfig::default().disable_allowed_hosts(),
        );

    let mcp_router =
        Router::new()
            .nest_service("/mcp", mcp_service)
            .layer(middleware::from_fn_with_state(
                shared.clone(),
                auth_gate::require_identity,
            ));
    let app = Router::new().merge(mcp_router).with_state(shared.clone());

    uds::serve_router_unix(&cli.uds, app)
        .await
        .with_context(|| format!("serve {}", cli.uds.display()))
}
