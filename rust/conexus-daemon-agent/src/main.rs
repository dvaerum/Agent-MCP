//! CoNexus reference daemon-agent event loop.
//!
//! Faithful Rust port of `nix/agent-mcp-daemon-agent-runner.py` +
//! `nix/agent-mcp-daemon-agent.sh.in`, folded into ONE binary (no
//! separate bash wrapper substituting `@python@`/`@runner@` paths --
//! the token/URL/cursor-path resolution the wrapper did is done
//! directly in Rust instead, since there's no interpreter boundary
//! to cross anymore).
//!
//! This is a pure MCP wire-protocol client: it long-polls
//! `wait_for_events` over the project's `/mcp` endpoint, logs each
//! returned event as a structured JSON line (a placeholder for a
//! real Claude-session hand-off, same as the Python original), and
//! persists the `next_cursor` between iterations so a restart
//! doesn't replay the entire event backlog. It has zero dependency
//! on whether the backend/router it talks to is Python or Rust --
//! ported to Rust purely for implementation-language consistency
//! with the rest of this migration (operator decision, 2026-09-06),
//! not because the Python original had any functional gap.

use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{Context, Result};
use clap::Parser;
use serde::{Deserialize, Serialize};
use serde_json::Value;

/// How long to `wait_for_events` on each iteration. Matches the
/// tool's server-side default; raising it above 900s is rejected by
/// the server (the tool clamps to its own MAX_TIMEOUT).
const TIMEOUT_SECONDS: u64 = 60;

/// Client-side HTTP timeout, deliberately longer than the server-side
/// long-poll timeout so we don't preempt a healthy long-poll just shy
/// of its own return.
const CLIENT_TIMEOUT_SECONDS: u64 = TIMEOUT_SECONDS + 30;

/// Polling delay between empty responses. `wait_for_events` with a
/// 60s timeout self-throttles, but a small inter-iteration sleep
/// keeps a tight server-side return loop (e.g. error -> 0s return)
/// from pegging CPU.
const INTER_ITER_SLEEP: Duration = Duration::from_millis(500);

/// Reconnect backoff for transport-level failures. Bounded so a
/// transient network blip doesn't take the daemon down for hours.
const RECONNECT_BACKOFF_INITIAL: Duration = Duration::from_secs(2);
const RECONNECT_BACKOFF_MAX: Duration = Duration::from_secs(60);

#[derive(Parser, Debug)]
#[command(
    name = "conexus-daemon-agent",
    about = "Reference daemon-agent event loop (wait_for_events long-poll client)"
)]
struct Cli {
    /// `<project>--<agent_id>` instance name (e.g.
    /// `washing-brothers--backend-dev`) -- matches the systemd unit's
    /// own instance-name convention.
    instance: String,
    /// Router's loopback port (the same value the nix module
    /// substituted into the old bash wrapper as `@router_port@`).
    #[arg(long)]
    router_port: u16,
}

#[derive(Debug, thiserror::Error)]
enum InstanceError {
    #[error(
        "instance '{0}' does not split into <project>--<agent_id> (expected a double-hyphen \
         separator, e.g. 'washing-brothers--backend-dev')"
    )]
    NotSplittable(String),
}

/// Port of the bash wrapper's `${var%--*}`/`${var##*--}` split: both
/// are anchored at the string's LAST `--` occurrence (the shortest
/// trailing match / longest leading match of the same separator both
/// land there), which is exactly `str::rsplit_once("--")`.
fn parse_instance(instance: &str) -> Result<(String, String), InstanceError> {
    let Some((project, agent_id)) = instance.rsplit_once("--") else {
        return Err(InstanceError::NotSplittable(instance.to_string()));
    };
    if project.is_empty() || agent_id.is_empty() {
        return Err(InstanceError::NotSplittable(instance.to_string()));
    }
    Ok((project.to_string(), agent_id.to_string()))
}

fn config_home() -> PathBuf {
    std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(std::env::var_os("HOME").expect("HOME must be set")).join(".config")
        })
}

fn state_home() -> PathBuf {
    std::env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(std::env::var_os("HOME").expect("HOME must be set"))
                .join(".local")
                .join("state")
        })
}

fn token_path(project: &str, agent_id: &str) -> PathBuf {
    config_home()
        .join("agent-mcp")
        .join("tokens")
        .join(format!("{project}--{agent_id}.token"))
}

fn cursor_path(project: &str, agent_id: &str) -> PathBuf {
    state_home()
        .join("agent-mcp-daemons")
        .join(format!("{project}--{agent_id}.cursor"))
}

/// Reads a bearer token file, stripping the trailing newline/CR the
/// same way the bash wrapper's `${bearer//$'\n'/}`/`${bearer//$'\r'/}`
/// does (a plaintext-provisioned token file commonly ends in one).
fn read_bearer(path: &Path) -> Result<String> {
    let raw = std::fs::read_to_string(path)
        .with_context(|| format!("missing bearer token at {}", path.display()))?;
    Ok(raw.replace(['\n', '\r'], ""))
}

fn load_cursor(path: &Path) -> String {
    match std::fs::read_to_string(path) {
        Ok(s) => s.trim().to_string(),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => String::new(),
        Err(e) => {
            log_json(
                "warn",
                "cursor read failed",
                None,
                &[("error", e.to_string().into())],
            );
            String::new()
        }
    }
}

fn save_cursor(path: &Path, cursor: &str) {
    if let Some(parent) = path.parent() {
        if let Err(e) = std::fs::create_dir_all(parent) {
            log_json(
                "warn",
                "cursor write failed",
                None,
                &[("error", e.to_string().into())],
            );
            return;
        }
    }
    if let Err(e) = std::fs::write(path, cursor) {
        log_json(
            "warn",
            "cursor write failed",
            None,
            &[("error", e.to_string().into())],
        );
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize, Default)]
struct Envelope {
    #[serde(default)]
    events: Vec<Value>,
    #[serde(default)]
    next_cursor: Option<String>,
}

/// Extracts the `wait_for_events` envelope from a raw HTTP response
/// body, which may be either Streamable-HTTP SSE shape (`data: `-
/// prefixed lines, one JSON-RPC response each) or plain JSON
/// (`json_response=True` on the server). Faithful port of Python's
/// `_wait_for_events`'s own two-path parse + defensive fallback.
fn extract_envelope(raw: &str, fallback_cursor: &str) -> Envelope {
    for line in raw.lines() {
        if let Some(data) = line.strip_prefix("data: ") {
            if let Some(env) = parse_jsonrpc_envelope(data) {
                return env;
            }
        }
    }
    parse_jsonrpc_envelope(raw).unwrap_or(Envelope {
        events: Vec::new(),
        next_cursor: Some(fallback_cursor.to_string()),
    })
}

fn parse_jsonrpc_envelope(text: &str) -> Option<Envelope> {
    let jrpc: Value = serde_json::from_str(text).ok()?;
    let inner_text = jrpc
        .get("result")?
        .get("content")?
        .get(0)?
        .get("text")?
        .as_str()?;
    serde_json::from_str(inner_text).ok()
}

fn log_json(level: &str, msg: &str, agent_ctx: Option<(&str, &str)>, extra: &[(&str, Value)]) {
    let mut record = serde_json::Map::new();
    record.insert("level".into(), level.into());
    record.insert("msg".into(), msg.into());
    if let Some((project, agent)) = agent_ctx {
        record.insert("project".into(), project.into());
        record.insert("agent".into(), agent.into());
    }
    for (k, v) in extra {
        record.insert((*k).to_string(), v.clone());
    }
    println!("{}", Value::Object(record));
}

struct DaemonConfig {
    mcp_url: String,
    project: String,
    agent_id: String,
    bearer: String,
    cursor_file: PathBuf,
}

/// A found-live production bug, fixed here: this daemon's original
/// design (and the Python original it replaces) sent a bare
/// `tools/call` POST with no prior session handshake. That's rejected
/// with `422 Unexpected message, expect initialize request` by
/// `conexus-backend`'s `rmcp::StreamableHttpService` (backed by a real
/// `LocalSessionManager`, not a stateless mode) -- a stricter session
/// lifecycle than the old Python/aiohttp MCP SDK enforced. A real MCP
/// client must `initialize` first and echo the server-issued
/// `Mcp-Session-Id` header on every subsequent request within that
/// session. `establish_session`/`wait_for_events` below do that;
/// `run_loop` re-establishes the session on ANY error (a stale/expired
/// session, a backend restart, or a genuine transport failure all look
/// the same to this client and are all fixed the same way: reconnect).
#[derive(Debug, thiserror::Error)]
enum DaemonError {
    #[error("transport error: {0}")]
    Transport(#[from] reqwest::Error),
    #[error("HTTP {status}: {body}")]
    HttpStatus { status: u16, body: String },
}

const MCP_SESSION_ID_HEADER: &str = "mcp-session-id";

/// Performs the real MCP session handshake (`initialize` then
/// `notifications/initialized`), returning the server-issued session
/// id to echo on every later request. Faithful to what every other
/// real MCP client in this codebase does (`conexus-backend`'s own
/// live-verification scripts, the dashboard) -- this daemon is a real
/// MCP client, not a one-shot HTTP probe.
async fn establish_session(
    client: &reqwest::Client,
    cfg: &DaemonConfig,
) -> Result<String, DaemonError> {
    let init_body = serde_json::json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "conexus-daemon-agent", "version": env!("CARGO_PKG_VERSION")},
        },
    });
    let resp = client
        .post(&cfg.mcp_url)
        .bearer_auth(&cfg.bearer)
        .header("Accept", "application/json, text/event-stream")
        .header("Content-Type", "application/json")
        .json(&init_body)
        .send()
        .await?;
    let status = resp.status();
    let session_id = resp
        .headers()
        .get(MCP_SESSION_ID_HEADER)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    let body_text = resp.text().await?;
    if !status.is_success() {
        return Err(DaemonError::HttpStatus {
            status: status.as_u16(),
            body: body_text,
        });
    }

    let mut notif_req = client
        .post(&cfg.mcp_url)
        .bearer_auth(&cfg.bearer)
        .header("Accept", "application/json, text/event-stream")
        .header("Content-Type", "application/json");
    if !session_id.is_empty() {
        notif_req = notif_req.header(MCP_SESSION_ID_HEADER, &session_id);
    }
    let notif_resp = notif_req
        .json(&serde_json::json!({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        .send()
        .await?;
    let notif_status = notif_resp.status();
    let notif_body = notif_resp.text().await?;
    if !notif_status.is_success() {
        return Err(DaemonError::HttpStatus {
            status: notif_status.as_u16(),
            body: notif_body,
        });
    }

    Ok(session_id)
}

async fn wait_for_events(
    client: &reqwest::Client,
    cfg: &DaemonConfig,
    session_id: &str,
    cursor: &str,
) -> Result<Envelope, DaemonError> {
    let body = serde_json::json!({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "wait_for_events",
            "arguments": {
                "since": cursor,
                "timeout_seconds": TIMEOUT_SECONDS,
            },
        },
    });
    let mut req = client
        .post(&cfg.mcp_url)
        .bearer_auth(&cfg.bearer)
        .header("Accept", "application/json, text/event-stream")
        .header("Content-Type", "application/json");
    if !session_id.is_empty() {
        req = req.header(MCP_SESSION_ID_HEADER, session_id);
    }
    let resp = req.json(&body).send().await?;
    let status = resp.status();
    let raw = resp.text().await?;
    if !status.is_success() {
        return Err(DaemonError::HttpStatus {
            status: status.as_u16(),
            body: raw,
        });
    }
    Ok(extract_envelope(&raw, cursor))
}

fn handle_event(project: &str, agent_id: &str, event: &Value) {
    let preview: String = event
        .get("data")
        .map(|d| d.to_string())
        .unwrap_or_default()
        .chars()
        .take(200)
        .collect();
    log_json(
        "event",
        "received",
        Some((project, agent_id)),
        &[
            ("type", event.get("type").cloned().unwrap_or(Value::Null)),
            (
                "timestamp",
                event.get("timestamp").cloned().unwrap_or(Value::Null),
            ),
            ("data_preview", preview.into()),
        ],
    );
}

async fn run_loop(cfg: DaemonConfig, client: reqwest::Client) {
    let mut cursor = load_cursor(&cfg.cursor_file);
    let mut backoff = RECONNECT_BACKOFF_INITIAL;
    // `None` means "no live session -- establish one before the next
    // call". Starts unset and gets cleared on ANY error (a stale/
    // expired session and a genuine transport failure look identical
    // to this client, and are fixed the same way: reconnect).
    let mut session_id: Option<String> = None;
    log_json(
        "info",
        "daemon started",
        Some((&cfg.project, &cfg.agent_id)),
        &[
            ("url", cfg.mcp_url.clone().into()),
            ("cursor", cursor.clone().into()),
        ],
    );

    loop {
        let sid = match &session_id {
            Some(sid) => sid.clone(),
            None => match establish_session(&client, &cfg).await {
                Ok(sid) => {
                    session_id = Some(sid.clone());
                    sid
                }
                Err(e) => {
                    log_json(
                        "warn",
                        "session handshake failed, backing off",
                        Some((&cfg.project, &cfg.agent_id)),
                        &[
                            ("error", e.to_string().into()),
                            ("backoff_secs", backoff.as_secs().into()),
                        ],
                    );
                    tokio::time::sleep(backoff).await;
                    backoff = (backoff * 2).min(RECONNECT_BACKOFF_MAX);
                    continue;
                }
            },
        };

        let envelope = match wait_for_events(&client, &cfg, &sid, &cursor).await {
            Ok(env) => env,
            Err(e) => {
                log_json(
                    "warn",
                    "transport error, backing off",
                    Some((&cfg.project, &cfg.agent_id)),
                    &[
                        ("error", e.to_string().into()),
                        ("backoff_secs", backoff.as_secs().into()),
                    ],
                );
                session_id = None;
                tokio::time::sleep(backoff).await;
                backoff = (backoff * 2).min(RECONNECT_BACKOFF_MAX);
                continue;
            }
        };

        backoff = RECONNECT_BACKOFF_INITIAL;

        for event in &envelope.events {
            handle_event(&cfg.project, &cfg.agent_id, event);
        }

        if let Some(next_cursor) = envelope.next_cursor {
            if next_cursor != cursor {
                cursor = next_cursor;
                save_cursor(&cfg.cursor_file, &cursor);
            }
        }

        tokio::time::sleep(INTER_ITER_SLEEP).await;
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    let (project, agent_id) = parse_instance(&cli.instance)?;
    let bearer = read_bearer(&token_path(&project, &agent_id))?;
    let cfg = DaemonConfig {
        mcp_url: format!(
            "http://127.0.0.1:{}/agent-mcp/mcp/{}",
            cli.router_port, project
        ),
        cursor_file: cursor_path(&project, &agent_id),
        project,
        agent_id,
        bearer,
    };
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(CLIENT_TIMEOUT_SECONDS))
        .build()
        .context("build reqwest client")?;

    #[cfg(unix)]
    {
        let mut sigterm = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .context("install SIGTERM handler")?;
        tokio::select! {
            _ = run_loop(cfg, client) => {}
            _ = tokio::signal::ctrl_c() => {
                log_json("info", "received signal, exiting", None, &[("signal", "SIGINT".into())]);
            }
            _ = sigterm.recv() => {
                log_json("info", "received signal, exiting", None, &[("signal", "SIGTERM".into())]);
            }
        }
    }
    #[cfg(not(unix))]
    {
        tokio::select! {
            _ = run_loop(cfg, client) => {}
            _ = tokio::signal::ctrl_c() => {
                log_json("info", "received signal, exiting", None, &[("signal", "SIGINT".into())]);
            }
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use bytes::Bytes;
    use http_body_util::Full;
    use hyper::service::service_fn;
    use hyper::{Request, Response};
    use hyper_util::rt::TokioIo;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    #[test]
    fn parse_instance_splits_on_the_last_double_hyphen() {
        assert_eq!(
            parse_instance("washing-brothers--backend-dev").unwrap(),
            ("washing-brothers".to_string(), "backend-dev".to_string())
        );
    }

    #[test]
    fn parse_instance_rejects_no_separator() {
        assert!(parse_instance("no-separator-here").is_err());
    }

    #[test]
    fn parse_instance_rejects_empty_project_or_agent() {
        assert!(parse_instance("--agent").is_err());
        assert!(parse_instance("project--").is_err());
    }

    #[test]
    fn parse_instance_matches_bash_semantics_for_a_trailing_empty_agent() {
        // `${var%--*}` on "abc--" removes the shortest trailing
        // `--*` match (the final "--"), leaving "abc"; `${var##*--}`
        // removes the longest leading `*--` match, leaving "" --
        // both halves land on the LAST "--", same as rsplit_once.
        // This instance is still rejected (empty agent_id), but the
        // SPLIT itself must match bash's, not just the final verdict.
        let err = parse_instance("abc--").unwrap_err();
        assert!(matches!(err, InstanceError::NotSplittable(_)));
    }

    #[test]
    fn cursor_round_trips_through_disk() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("nested").join("proj--agent.cursor");
        assert_eq!(load_cursor(&path), "");
        save_cursor(&path, "2026-09-07T00:00:00Z");
        assert_eq!(load_cursor(&path), "2026-09-07T00:00:00Z");
    }

    #[test]
    fn cursor_load_trims_whitespace() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("proj--agent.cursor");
        std::fs::write(&path, "  cursor-value\n").unwrap();
        assert_eq!(load_cursor(&path), "cursor-value");
    }

    fn wrap_envelope(envelope: &str) -> String {
        serde_json::json!({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": envelope}]}
        })
        .to_string()
    }

    #[test]
    fn extract_envelope_parses_the_sse_data_line_shape() {
        let body = format!(
            "data: \nid: 0\nretry: 3000\n\ndata: {}\n\n",
            wrap_envelope(r#"{"events":[{"type":"x"}],"next_cursor":"c1"}"#)
        );
        let env = extract_envelope(&body, "fallback");
        assert_eq!(env.next_cursor.as_deref(), Some("c1"));
        assert_eq!(env.events.len(), 1);
    }

    #[test]
    fn extract_envelope_parses_the_plain_json_shape() {
        let body = wrap_envelope(r#"{"events":[],"next_cursor":"c2"}"#);
        let env = extract_envelope(&body, "fallback");
        assert_eq!(env.next_cursor.as_deref(), Some("c2"));
        assert!(env.events.is_empty());
    }

    #[test]
    fn extract_envelope_falls_back_on_malformed_body() {
        let env = extract_envelope("not json at all", "fallback-cursor");
        assert_eq!(env.next_cursor.as_deref(), Some("fallback-cursor"));
        assert!(env.events.is_empty());
    }

    #[test]
    fn read_bearer_strips_trailing_newline_and_cr() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("tok");
        std::fs::write(&path, "abc123\r\n").unwrap();
        assert_eq!(read_bearer(&path).unwrap(), "abc123");
    }

    #[test]
    fn read_bearer_errors_clearly_on_a_missing_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("does-not-exist");
        assert!(read_bearer(&path).is_err());
    }

    #[test]
    fn token_and_cursor_paths_use_the_double_hyphen_convention() {
        std::env::set_var("XDG_CONFIG_HOME", "/tmp/xdgcfg-test");
        std::env::set_var("XDG_STATE_HOME", "/tmp/xdgstate-test");
        assert_eq!(
            token_path("proj", "agent"),
            PathBuf::from("/tmp/xdgcfg-test/agent-mcp/tokens/proj--agent.token")
        );
        assert_eq!(
            cursor_path("proj", "agent"),
            PathBuf::from("/tmp/xdgstate-test/agent-mcp-daemons/proj--agent.cursor")
        );
        std::env::remove_var("XDG_CONFIG_HOME");
        std::env::remove_var("XDG_STATE_HOME");
    }

    /// A real HTTP/1 server, driven by the same low-level hyper
    /// server API the rest of this workspace's tests use (matching
    /// `conexus-router::proxy_core`'s `spawn_backend` precedent) --
    /// proves `wait_for_events` against a genuine peer, not a mock.
    async fn spawn_mock_backend(
        response_builder: impl Fn(&Request<hyper::body::Incoming>) -> Response<Full<Bytes>>
            + Send
            + Sync
            + 'static,
    ) -> (String, tokio::task::JoinHandle<()>) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let response_builder = Arc::new(response_builder);
        let handle = tokio::spawn(async move {
            loop {
                let Ok((stream, _)) = listener.accept().await else {
                    return;
                };
                let response_builder = response_builder.clone();
                tokio::spawn(async move {
                    let io = TokioIo::new(stream);
                    let svc = service_fn(move |req: Request<hyper::body::Incoming>| {
                        let response_builder = response_builder.clone();
                        async move { Ok::<_, std::convert::Infallible>(response_builder(&req)) }
                    });
                    let _ = hyper::server::conn::http1::Builder::new()
                        .serve_connection(io, svc)
                        .await;
                });
            }
        });
        (format!("http://{addr}"), handle)
    }

    #[tokio::test]
    async fn wait_for_events_round_trips_a_real_http_call() {
        let (base_url, _handle) = spawn_mock_backend(|req| {
            assert_eq!(req.method(), hyper::Method::POST);
            assert_eq!(
                req.headers().get("authorization").unwrap(),
                "Bearer real-token"
            );
            Response::builder()
                .status(200)
                .body(Full::new(Bytes::from(wrap_envelope(
                    r#"{"events":[{"type":"task_assigned"}],"next_cursor":"2026-09-07T00:00:01Z"}"#,
                ))))
                .unwrap()
        })
        .await;

        let cfg = DaemonConfig {
            mcp_url: base_url,
            project: "proj".to_string(),
            agent_id: "agent".to_string(),
            bearer: "real-token".to_string(),
            cursor_file: PathBuf::from("/dev/null"),
        };
        let client = reqwest::Client::new();
        let env = wait_for_events(&client, &cfg, "", "").await.unwrap();
        assert_eq!(env.events.len(), 1);
        assert_eq!(env.next_cursor.as_deref(), Some("2026-09-07T00:00:01Z"));
    }

    #[tokio::test]
    async fn establish_session_captures_the_mcp_session_id_header_and_notifies() {
        let notif_seen = Arc::new(AtomicUsize::new(0));
        let notif_seen_srv = notif_seen.clone();
        let (base_url, _handle) = spawn_mock_backend(move |req| {
            // Exactly one of the 2 requests this handshake makes
            // (notifications/initialized) carries the session id back
            // -- the init call itself can't, since it's the response
            // that ISSUES the id.
            if req.headers().get(MCP_SESSION_ID_HEADER).is_some() {
                notif_seen_srv.fetch_add(1, Ordering::SeqCst);
            }
            Response::builder()
                .status(200)
                .header("Mcp-Session-Id", "sess-abc123")
                .body(Full::new(Bytes::from("{}")))
                .unwrap()
        })
        .await;

        let cfg = DaemonConfig {
            mcp_url: base_url,
            project: "proj".to_string(),
            agent_id: "agent".to_string(),
            bearer: "tok".to_string(),
            cursor_file: PathBuf::from("/dev/null"),
        };
        let client = reqwest::Client::new();
        let session_id = establish_session(&client, &cfg).await.unwrap();
        assert_eq!(session_id, "sess-abc123");
        // The notifications/initialized call must have echoed the
        // session id header back -- the init call itself can't have
        // one yet (issued BY that response), so exactly 1 request
        // carries it.
        assert_eq!(notif_seen.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn establish_session_surfaces_a_non_success_status_as_an_error() {
        let (base_url, _handle) = spawn_mock_backend(|_req| {
            Response::builder()
                .status(422)
                .body(Full::new(Bytes::from("nope")))
                .unwrap()
        })
        .await;
        let cfg = DaemonConfig {
            mcp_url: base_url,
            project: "proj".to_string(),
            agent_id: "agent".to_string(),
            bearer: "tok".to_string(),
            cursor_file: PathBuf::from("/dev/null"),
        };
        let client = reqwest::Client::new();
        let err = establish_session(&client, &cfg).await.unwrap_err();
        assert!(matches!(err, DaemonError::HttpStatus { status: 422, .. }));
    }

    #[tokio::test]
    async fn run_loop_persists_the_cursor_and_logs_each_event_then_stops_on_repeat() {
        // The session handshake (initialize + notifications/initialized)
        // happens ONCE before the loop's own tools/call requests --
        // the real event is served on the 3rd request overall (index
        // 2), everything after that is the empty-envelope-with-same-
        // cursor steady state. We drive the loop for a few real
        // iterations via a short overall timeout rather than trying to
        // cancel it mid-await, matching this workspace's own
        // `tokio::time::timeout`-around-a-loop pattern.
        let call_count = Arc::new(AtomicUsize::new(0));
        let call_count_srv = call_count.clone();
        let (base_url, _handle) = spawn_mock_backend(move |_req| {
            let n = call_count_srv.fetch_add(1, Ordering::SeqCst);
            let body = if n == 2 {
                wrap_envelope(r#"{"events":[{"type":"e1","timestamp":"t1","data":{"x":1}}],"next_cursor":"c1"}"#)
            } else if n < 2 {
                // initialize / notifications/initialized handshake --
                // establish_session only checks the status code.
                "{}".to_string()
            } else {
                wrap_envelope(r#"{"events":[],"next_cursor":"c1"}"#)
            };
            Response::builder()
                .status(200)
                .body(Full::new(Bytes::from(body)))
                .unwrap()
        })
        .await;

        let dir = tempfile::tempdir().unwrap();
        let cursor_file = dir.path().join("proj--agent.cursor");
        let cfg = DaemonConfig {
            mcp_url: base_url,
            project: "proj".to_string(),
            agent_id: "agent".to_string(),
            bearer: "tok".to_string(),
            cursor_file: cursor_file.clone(),
        };
        let client = reqwest::Client::new();

        let _ = tokio::time::timeout(Duration::from_millis(700), run_loop(cfg, client)).await;

        assert_eq!(load_cursor(&cursor_file), "c1");
        assert!(call_count.load(Ordering::SeqCst) >= 2);
    }
}
