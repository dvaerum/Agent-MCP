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

use crate::observe;
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
    /// id/title/tool/status/project_path. Retained as a capability, but the
    /// bridge now sources liveness from AoE's richer web REST `GET
    /// /api/sessions` (which also exposes worker state), so this is currently
    /// unused for reconcile.
    #[allow(dead_code)]
    pub async fn sessions_list(&self) -> Result<Value> {
        self.call("sessions.list", json!({})).await
    }

    /// `config.get` (needs only `runtime.worker`) — the value of one of THIS
    /// plugin's own declared settings. Returns `{ value, revision }`.
    pub async fn config_get(&self, key: &str) -> Result<Value> {
        self.call("config.get", json!({ "key": key })).await
    }

    /// `session.mcp.set` (needs `session.mcp`) — replace one session's
    /// per-session MCP layer with `params.servers`. Idempotent on the host
    /// side: an unchanged set is a no-op (no respawn), so the bridge may
    /// re-assert it every reconcile. Returns `{ "status": ... }`.
    pub async fn session_mcp_set(&self, params: Value) -> Result<Value> {
        self.call("session.mcp.set", params).await
    }

    /// `ui.notify` (needs `notifications`) — surface a toast to the operator.
    /// Best-effort; callers ignore the result.
    ///
    /// The host caps `title` at 256 bytes and `body` at 4096
    /// (`src/plugin/ui_state.rs`) and rejects the whole call if either is over,
    /// so both are clipped here rather than losing the notification. `tone` is a
    /// closed host-side set: `neutral|info|success|warn|danger`.
    pub async fn ui_notify(&self, tone: &str, title: &str, body: Option<&str>) -> Result<Value> {
        self.call(
            "ui.notify",
            json!({
                "tone": tone,
                "title": clip_bytes(title, 256),
                "body": body.map(|b| clip_bytes(b, 4096)),
            }),
        )
        .await
    }

    /// `ui.state.set` (needs `runtime.worker` AND the `(slot, id)` pair declared
    /// in the manifest's `[[ui]]`) — replace this plugin's entry in a
    /// host-rendered slot. The bridge uses the global `settings-page` slot, which
    /// the AoE dashboard mounts as its own Settings nav entry.
    pub async fn ui_state_set(&self, slot: &str, id: &str, payload: Value) -> Result<Value> {
        self.call(
            "ui.state.set",
            json!({ "slot": slot, "id": id, "payload": payload }),
        )
        .await
    }
}

/// Clip a string to at most `max` BYTES without splitting a UTF-8 character.
fn clip_bytes(s: &str, max: usize) -> String {
    if s.len() <= max {
        return s.to_string();
    }
    let mut end = max;
    while end > 0 && !s.is_char_boundary(end) {
        end -= 1;
    }
    s[..end].to_string()
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

/// Read host lines until EOF, demuxing responses from requests.
///
/// - `reconcile` is pinged whenever the host reports a settings change so the
///   bridge can re-resolve its routes immediately instead of waiting for a tick.
/// - `publish` carries UI-repaint requests raised by host-invoked commands.
pub async fn run_stdin_reader(
    conn: Arc<PluginConn>,
    reconcile: Arc<Notify>,
    publish: mpsc::UnboundedSender<PublishReason>,
) {
    let mut lines = BufReader::new(tokio::io::stdin()).lines();
    loop {
        match lines.next_line().await {
            Ok(Some(line)) => handle_line(&conn, &reconcile, &publish, &line),
            Ok(None) => break, // EOF: host closed stdin -> worker exits.
            Err(e) => {
                observe::error(&format!("stdin read error: {e}"));
                break;
            }
        }
    }
}

/// Why the UI state is being republished. `StatusCommand` additionally toasts a
/// one-line summary, because a command invocation is fire-and-forget: the
/// operator gets no HTTP response body to read, so the toast IS the answer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PublishReason {
    /// Routine repaint (reconcile finished, frame handled, periodic refresh).
    Tick,
    /// The operator ran the plugin's `status` command.
    StatusCommand,
}

fn handle_line(
    conn: &Arc<PluginConn>,
    reconcile: &Arc<Notify>,
    publish: &mpsc::UnboundedSender<PublishReason>,
    line: &str,
) {
    let incoming: Incoming = match protocol::parse_incoming(line) {
        Ok(Some(i)) => i,
        Ok(None) => return,
        Err(e) => {
            observe::warn(&format!("ignoring unparseable host line: {e}"));
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
            None => handle_notification(reconcile, publish, method, &incoming.params),
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

/// Handle a host notification (a `method` with no `id`, so no reply is possible).
///
/// `plugin.command.invoke` is how the host delivers a `[[commands]]` entry:
/// `POST /api/plugins/commands/{fqid}/invoke` answers `202 {"ok": true}` and
/// forwards `{ command: <fqid>, session_id }` as a **notification**
/// (`src/server/api/plugins.rs` → `PluginHost::notify_worker`). The command id
/// is therefore in `params.command`, NOT in the method name — a worker that
/// dispatches on the method's trailing segment never sees its own commands at
/// all, which is why the bridge's `status` command had never once run.
fn handle_notification(
    reconcile: &Arc<Notify>,
    publish: &mpsc::UnboundedSender<PublishReason>,
    method: &str,
    params: &Value,
) {
    match method {
        "plugin.settings.changed" => reconcile.notify_one(),
        "plugin.command.invoke" => {
            let fqid = params
                .get("command")
                .and_then(Value::as_str)
                .unwrap_or_default();
            match command_id(fqid) {
                "status" => {
                    observe::info("status command invoked; republishing bridge state");
                    let _ = publish.send(PublishReason::StatusCommand);
                }
                other => observe::warn(&format!("ignoring unknown command {other:?}")),
            }
        }
        // Any other host notification is not one we handle: ignore it.
        _ => {}
    }
}

/// The bare command id from a fully-qualified `plugin.<plugin-id>.<command>`.
/// Plugin ids contain dots (`dev.dvaerum.agent-mcp-delivery`), so only the
/// trailing segment is the command.
fn command_id(fqid: &str) -> &str {
    fqid.rsplit('.').next().unwrap_or(fqid)
}

/// Handle a host-initiated *request* (a `method` carrying an `id`).
///
/// The current host has no request-shaped command path — commands arrive as
/// notifications (see [`handle_notification`]) — but the protocol allows a host
/// request, so an unknown one must answer `METHOD_NOT_FOUND` rather than hang
/// the host's parked call.
fn dispatch_command(method: &str) -> std::result::Result<Value, (i64, String)> {
    match command_id(method) {
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

    fn reader_bits() -> (
        Arc<Notify>,
        mpsc::UnboundedSender<PublishReason>,
        mpsc::UnboundedReceiver<PublishReason>,
    ) {
        let (tx, rx) = mpsc::unbounded_channel();
        (Arc::new(Notify::new()), tx, rx)
    }

    #[test]
    fn command_id_takes_the_trailing_segment_of_a_dotted_plugin_id() {
        assert_eq!(
            command_id("plugin.dev.dvaerum.agent-mcp-delivery.status"),
            "status"
        );
        assert_eq!(command_id("status"), "status");
    }

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

    #[test]
    fn command_invoke_notification_requests_a_status_publish() {
        // The regression this closes: the host names EVERY command with the same
        // method (`plugin.command.invoke`) and puts the fqid in params, so
        // dispatching on the method's trailing segment never fired the command.
        let (reconcile, tx, mut rx) = reader_bits();
        handle_notification(
            &reconcile,
            &tx,
            "plugin.command.invoke",
            &json!({
                "command": "plugin.dev.dvaerum.agent-mcp-delivery.status",
                "session_id": "sid-1",
            }),
        );
        assert_eq!(rx.try_recv().unwrap(), PublishReason::StatusCommand);
    }

    #[test]
    fn unknown_command_invoke_publishes_nothing() {
        let (reconcile, tx, mut rx) = reader_bits();
        handle_notification(
            &reconcile,
            &tx,
            "plugin.command.invoke",
            &json!({ "command": "plugin.x.frobnicate" }),
        );
        assert!(rx.try_recv().is_err());
    }

    #[tokio::test]
    async fn settings_changed_notification_pings_reconcile_without_publishing() {
        let (reconcile, tx, mut rx) = reader_bits();
        handle_notification(
            &reconcile,
            &tx,
            "plugin.settings.changed",
            &json!({ "revision": 4, "changed_keys": ["sessions"] }),
        );
        // A settings change re-resolves routes; it is not a repaint request (the
        // reconcile it triggers publishes one when it finishes).
        assert!(rx.try_recv().is_err());
        // `notify_one` before any waiter parks stores a permit, so this resolves.
        tokio::time::timeout(Duration::from_secs(1), reconcile.notified())
            .await
            .expect("reconcile should have been notified");
    }

    #[test]
    fn ui_notify_clips_oversized_title_and_body_to_the_host_caps() {
        // The host rejects the whole call past 256/4096 bytes, so clipping is
        // what keeps a long AoE error from silently dropping the toast.
        assert_eq!(clip_bytes("abc", 256), "abc");
        assert_eq!(clip_bytes(&"x".repeat(300), 256).len(), 256);
        // Never split a multi-byte char: 'é' is 2 bytes, so a 3-byte cap keeps 1.
        assert_eq!(clip_bytes("ééé", 3), "é");
    }
}
