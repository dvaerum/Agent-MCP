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
//! - [`observe`]  — leveled, redacted, durable diagnostics.
//! - [`status`]   — observable state + the AoE settings page it renders into.
//! - [`bridge`]   — the async orchestrator wiring it all together.
//!
//! IMPORTANT: stdout is the JSON-RPC channel. Nothing but protocol lines may be
//! written there — a non-JSON line makes the host stop reading the worker.
//! Diagnostics go through [`observe`], which writes stderr AND a durable log
//! file (the host's own stderr sink is an unnamed per-spawn file nothing reads;
//! see the [`observe`] module docs).

mod bridge;
mod config;
mod inject;
mod mode;
mod observe;
mod plugin;
mod protocol;
mod render;
mod status;

use std::sync::{Arc, Mutex};

use tokio::sync::{mpsc, Notify};

#[tokio::main]
async fn main() {
    observe::init();
    observe::info(&format!(
        "aoe-bridge worker {} starting (log level {}, log file {})",
        env!("CARGO_PKG_VERSION"),
        observe::level().as_str(),
        observe::log_path().unwrap_or_else(|| "disabled".to_string()),
    ));

    // Single stdout writer, fed by one channel so protocol lines never interleave.
    let (out_tx, out_rx) = mpsc::unbounded_channel::<String>();
    tokio::spawn(plugin::run_stdout_writer(out_rx));

    let conn = plugin::PluginConn::new(out_tx);
    let reconcile = Arc::new(Notify::new());
    let state = Arc::new(Mutex::new(status::Snapshot::new(observe::now_secs())));
    // Repaint requests for the plugin's settings page, raised by the reconcile
    // loop, the per-session stream tasks, and the host-invoked `status` command.
    let (publish_tx, publish_rx) = mpsc::unbounded_channel::<plugin::PublishReason>();

    // The bridge orchestrator runs until the process exits.
    let bridge_handle = tokio::spawn(bridge::run_bridge(
        conn.clone(),
        reconcile.clone(),
        state.clone(),
        publish_tx.clone(),
    ));
    let publisher_handle = tokio::spawn(bridge::run_publisher(conn.clone(), state, publish_rx));

    // Block on the stdin reader; it returns when the host closes the pipe (EOF),
    // which is the worker's shutdown signal per the plugin protocol.
    plugin::run_stdin_reader(conn, reconcile, publish_tx).await;

    bridge_handle.abort();
    publisher_handle.abort();
    observe::info("stdin closed; worker exiting");
}
