//! The JSON-RPC stdio plugin loop: the bidirectional peer connection to the
//! AoE host.
//!
//! Three moving parts:
//! - [`run_stdout_writer`] — the **single** writer task. Everything the worker
//!   emits (its own requests AND its responses to host requests) is funnelled
//!   through one mpsc channel so lines never interleave on stdout.
//! - [`PluginConn`] — the host-RPC client. [`PluginConn::call`] allocates an
//!   id, parks a oneshot, writes the request, and awaits the matching response.
//! - [`run_stdin_reader`] — reads host lines and demuxes: a line with a
//!   `method` is a host request/notification (answered if it has an `id`); a
//!   line without a `method` is a response routed back to the parked caller.
//!
//! The worker exits when stdin reaches EOF (the host closed the pipe), matching
//! the plugin protocol contract.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{anyhow, Result};
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::sync::{mpsc, oneshot, Notify};

use crate::protocol::{self, codes, Incoming, RpcError};

/// How long a host RPC may take before we give up on it. The host answers
/// `sessions.list` / `config.get` synchronously, so this only guards against a
/// host that stopped reading our stdout.
const CALL_TIMEOUT: Duration = Duration::from_secs(30);

/// The peer connection to the host: a client for host RPCs plus the parked
/// in-flight calls awaiting their responses.
pub struct PluginConn {
    out_tx: mpsc::UnboundedSender<String>,
    next_id: AtomicU64,
    pending: Mutex<HashMap<u64, oneshot::Sender<std::result::Result<Value, RpcError>>>>,
}

impl PluginConn {
    pub fn new(out_tx: mpsc::UnboundedSender<String>) -> Arc<Self> {
        Arc::new(Self {
            out_tx,
            next_id: AtomicU64::new(1),
            pending: Mutex::new(HashMap::new()),
        })
    }

    /// Invoke a host RPC and await its result.
    pub async fn call(&self, method: &str, params: Value) -> Result<Value> {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let (tx, rx) = oneshot::channel();
        self.pending.lock().unwrap().insert(id, tx);
        let line = protocol::request_line(id, method, params);
        if self.out_tx.send(line).is_err() {
            self.pending.lock().unwrap().remove(&id);
            return Err(anyhow!("plugin stdout writer closed"));
        }
        match tokio::time::timeout(CALL_TIMEOUT, rx).await {
            Ok(Ok(Ok(v))) => Ok(v),
            Ok(Ok(Err(e))) => Err(anyhow!(
                "host rpc {method} failed: {} (code {})",
                e.message,
                e.code
            )),
            Ok(Err(_dropped)) => Err(anyhow!("host rpc {method} response channel dropped")),
            Err(_elapsed) => {
                self.pending.lock().unwrap().remove(&id);
                Err(anyhow!("host rpc {method} timed out"))
            }
        }
    }

    /// Route a response back to the caller that parked under `id`.
    fn complete(&self, id: u64, result: std::result::Result<Value, RpcError>) {
        if let Some(tx) = self.pending.lock().unwrap().remove(&id) {
            let _ = tx.send(result);
        }
    }

    /// Queue a raw, already-serialized line (a response to a host request).
    fn send_raw(&self, line: String) {
        let _ = self.out_tx.send(line);
    }

    // ---- Host RPC conveniences ------------------------------------------

    /// `sessions.list` (needs `session.read`) — live sessions with
    /// id/title/tool/status/project_path.
    pub async fn sessions_list(&self) -> Result<Value> {
        self.call("sessions.list", json!({})).await
    }

    /// `config.get` (needs only `runtime.worker`) — the value of one of THIS
    /// plugin's own declared settings. Returns `{ value, revision }`.
    pub async fn config_get(&self, key: &str) -> Result<Value> {
        self.call("config.get", json!({ "key": key })).await
    }

    /// `ui.notify` (needs `notifications`) — surface a message to the operator.
    /// Best-effort; callers ignore the result.
    #[allow(dead_code)]
    pub async fn ui_notify(&self, tone: &str, title: &str, body: Option<&str>) -> Result<Value> {
        self.call(
            "ui.notify",
            json!({ "tone": tone, "title": title, "body": body }),
        )
        .await
    }
}

/// Drain the outbound channel to stdout, one flushed line at a time.
pub async fn run_stdout_writer(mut rx: mpsc::UnboundedReceiver<String>) {
    let mut out = tokio::io::stdout();
    while let Some(line) = rx.recv().await {
        if out.write_all(line.as_bytes()).await.is_err() {
            break;
        }
        if out.flush().await.is_err() {
            break;
        }
    }
}

/// Read host lines until EOF, demuxing responses from requests. `reconcile`
/// is pinged whenever the host reports a settings change so the bridge can
/// re-resolve its routes immediately instead of waiting for the next tick.
pub async fn run_stdin_reader(conn: Arc<PluginConn>, reconcile: Arc<Notify>) {
    let mut lines = BufReader::new(tokio::io::stdin()).lines();
    loop {
        match lines.next_line().await {
            Ok(Some(line)) => handle_line(&conn, &reconcile, &line),
            Ok(None) => break, // EOF: host closed stdin -> worker exits.
            Err(e) => {
                crate::log(&format!("stdin read error: {e}"));
                break;
            }
        }
    }
}

fn handle_line(conn: &Arc<PluginConn>, reconcile: &Arc<Notify>, line: &str) {
    let incoming: Incoming = match protocol::parse_incoming(line) {
        Ok(Some(i)) => i,
        Ok(None) => return,
        Err(e) => {
            crate::log(&format!("ignoring unparseable host line: {e}"));
            return;
        }
    };

    // A `method` means the host is calling us (request if it has an id, else a
    // fire-and-forget notification). No `method` means it is answering us.
    if let Some(method) = incoming.method.as_deref() {
        match incoming.id.clone() {
            Some(id) => {
                let response = match dispatch_command(method) {
                    Ok(result) => protocol::success_line(id, result),
                    Err((code, msg)) => protocol::error_line(id, code, &msg),
                };
                conn.send_raw(response);
            }
            None => {
                if method == "plugin.settings.changed" {
                    reconcile.notify_one();
                }
                // Any other host notification is not one we handle: ignore it.
            }
        }
        return;
    }

    if let Some(id) = incoming.id.as_ref().and_then(Value::as_u64) {
        let result = match incoming.error {
            Some(e) => Err(e),
            None => Ok(incoming.result.unwrap_or(Value::Null)),
        };
        conn.complete(id, result);
    }
}

/// Handle a host-initiated command, dispatching on the trailing segment of the
/// namespaced method (`plugin.<id>.<command>`), per the plugin protocol.
fn dispatch_command(method: &str) -> std::result::Result<Value, (i64, String)> {
    match method.rsplit('.').next().unwrap_or(method) {
        "status" => Ok(json!({
            "ok": true,
            "message": "agent-mcp delivery bridge running",
        })),
        _ => Err((
            codes::METHOD_NOT_FOUND,
            format!("unknown method {method:?}"),
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_command_dispatches_on_trailing_segment() {
        let r = dispatch_command("plugin.dev.dvaerum.agent-mcp-delivery.status").unwrap();
        assert_eq!(r["ok"], json!(true));
    }

    #[test]
    fn unknown_command_is_method_not_found() {
        let (code, _msg) = dispatch_command("plugin.x.frobnicate").unwrap_err();
        assert_eq!(code, codes::METHOD_NOT_FOUND);
    }
}
