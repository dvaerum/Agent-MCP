//! agent-mcp delivery bridge — an Agent of Empires plugin worker.
//!
//! The bridge is the runtime side of ADR-0021's per-worker "delivery" fallback
//! channel. agent-mcp owns the *policy* (when to nudge a session that isn't
//! polling `wait_for_events`); this worker owns *delivery* — it reaches OUT to
//! agent-mcp, subscribes to each covered session's delivery SSE stream, reports
//! that session's transport-status back up, and injects the skinny frames it
//! receives into the AoE-run session via AoE's localhost REST.
//!
//! It is a long-lived daemon speaking newline-delimited JSON-RPC 2.0 over stdio
//! with the AoE plugin host (worker and host are peers over the one pipe).
//!
//! Module map:
//! - [`protocol`] — JSON-RPC wire types (pure).
//! - [`plugin`]   — the stdio plugin loop: writer, host-RPC client, reader/demux.
//! - [`config`]   — settings + the `session → (endpoint, token, mode)` resolver.
//! - [`mode`]     — mode + status classification (pure).
//! - [`render`]   — skinny frame renderer (pure).
//! - [`inject`]   — AoE REST request building + SSE data extraction (pure).
//! - [`bridge`]   — the async orchestrator wiring it all together.
//!
//! IMPORTANT: stdout is the JSON-RPC channel. All diagnostics go to stderr via
//! [`log`] (the host drains a worker's stderr to its worker log).

mod bridge;
mod config;
mod inject;
mod mode;
mod plugin;
mod protocol;
mod render;

use std::sync::Arc;

use tokio::sync::{mpsc, Notify};

/// Diagnostic log line — stderr only (stdout is the protocol channel).
pub fn log(msg: &str) {
    eprintln!("[aoe-bridge] {msg}");
}

#[tokio::main]
async fn main() {
    // Single stdout writer, fed by one channel so protocol lines never interleave.
    let (out_tx, out_rx) = mpsc::unbounded_channel::<String>();
    tokio::spawn(plugin::run_stdout_writer(out_rx));

    let conn = plugin::PluginConn::new(out_tx);
    let reconcile = Arc::new(Notify::new());

    // The bridge orchestrator runs until the process exits.
    let bridge_handle = tokio::spawn(bridge::run_bridge(conn.clone(), reconcile.clone()));

    // Block on the stdin reader; it returns when the host closes the pipe (EOF),
    // which is the worker's shutdown signal per the plugin protocol.
    plugin::run_stdin_reader(conn, reconcile).await;

    bridge_handle.abort();
    log("stdin closed; worker exiting");
}
