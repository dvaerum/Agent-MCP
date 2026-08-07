//! The orchestrator: reconcile covered sessions, hold one SSE delivery stream
//! per session, report transport-status up, and inject rendered frames down.
//!
//! Each reconcile pass (every `status_interval_secs`, or immediately on a
//! `plugin.settings.changed` nudge):
//! 1. load settings via `config.get`;
//! 2. fetch per-session liveness via AoE's web REST `GET /api/sessions`
//!    (worker state, not just status — so a stopped session reads `dormant`);
//! 3. resolve the per-session routes;
//! 4. for each live route: POST its transport-status, and ensure exactly one
//!    SSE task is running (respawning it if its endpoint/token/mode/AoE creds
//!    changed);
//! 5. for a configured-but-not-live session: POST `dead` and drop its task;
//! 6. drop tasks whose session left the mapping;
//! 7. publish the observable state to the plugin's AoE settings page.
//!
//! A per-session SSE task reconnects with backoff on drop — a transient stream
//! drop is not a session end (ADR-0021); the policy re-fires on reconnect.
//!
//! ## Observability
//!
//! Every step above records into a shared [`Snapshot`] ([`crate::status`]) that
//! [`run_publisher`] pushes to the host as a `settings-page` UI entry, and logs
//! through [`crate::observe`] at a level chosen for volume: per-frame and
//! per-inject traffic is `debug`, state *transitions* (stream up/down, coverage
//! gained/lost) are `info`, degraded-but-retrying is `warn`, and a refused
//! injection is `error`. An inject failure also captures the AoE error BODY,
//! not just the status code — that body is what names the cause (`400
//! acp_mode_unsupported`), and dropping it is what made the original outage
//! invisible.

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{Context, Result};
use futures_util::StreamExt;
use reqwest::header::{ACCEPT, AUTHORIZATION};
use reqwest::Response;
use serde_json::json;
use tokio::sync::{mpsc, Notify};
use tokio::task::JoinHandle;

use crate::config::{self, Liveness, Route};
use crate::inject::{build_injection, build_mcp_set_params, delivery_url, extract_sse_data};
use crate::mode::classify_transport_status;
use crate::observe::{self, now_secs};
use crate::plugin::{PluginConn, PublishReason};
use crate::render::{parse_frame, render_skinny};
use crate::status::{
    clip_detail, render_page, summary_line, InjectOutcome, Notice, Snapshot, StreamState, PAGE_ID,
    PAGE_SLOT,
};

/// How often the settings page repaints even with nothing happening. The page
/// shows relative ages ("last inject 3m ago"), which go stale without a tick.
const PUBLISH_REFRESH: Duration = Duration::from_secs(30);

/// Shared observable state, written by the reconcile loop and every session
/// task, read by the publisher.
pub type SharedState = Arc<Mutex<Snapshot>>;

/// Shared per-task context: the HTTP client, the AoE injection creds, the host
/// connection (for failure toasts) and the observable state.
struct BridgeCtx {
    http: reqwest::Client,
    aoe_base: String,
    aoe_token: String,
    conn: Arc<PluginConn>,
    state: SharedState,
    publish: mpsc::UnboundedSender<PublishReason>,
}

impl BridgeCtx {
    /// Mutate one session's observation. The lock is held only for the closure
    /// (never across an `.await`), so a slow HTTP call can never block the
    /// publisher.
    fn observe<R>(
        &self,
        session_id: &str,
        project: &str,
        f: impl FnOnce(&mut crate::status::SessionObs) -> R,
    ) -> R {
        let mut snap = self.state.lock().unwrap();
        f(snap.session_mut(session_id, project))
    }

    fn request_publish(&self) {
        let _ = self.publish.send(PublishReason::Tick);
    }
}

/// A running per-session SSE task and the route fingerprint it was spawned for.
struct SessionTask {
    fingerprint: String,
    handle: JoinHandle<()>,
}

/// The reconcile loop. Runs until the process exits (stdin EOF aborts it).
pub async fn run_bridge(
    conn: Arc<PluginConn>,
    reconcile: Arc<Notify>,
    state: SharedState,
    publish: mpsc::UnboundedSender<PublishReason>,
) {
    let http = reqwest::Client::new();
    let mut tasks: HashMap<String, SessionTask> = HashMap::new();
    // Per-session fingerprint of the last MCP set we asserted (`url|token`), so
    // we skip the RPC when nothing changed. Correctness never depends on this
    // (the host makes an unchanged set a no-op respawn); it only avoids RPC
    // spam and, on a real change, forces a re-assert.
    let mut mcp_set: HashMap<String, String> = HashMap::new();
    // Per-session guard: sessions we've already POSTed acp/enable for this run.
    // The host call is idempotent, so this only avoids repeat view-swaps / log
    // spam; a stale/removed row is dropped from it so a re-add re-enables.
    let mut acp_enabled: HashSet<String> = HashSet::new();

    loop {
        let settings = config::load_settings(&conn).await;
        let interval = Duration::from_secs(settings.status_interval_secs.max(5));

        {
            let mut snap = state.lock().unwrap();
            snap.enabled = settings.enabled;
            snap.configured = !settings.agent_mcp_base.trim().is_empty();
            snap.configured_rows = settings.sessions.len();
            snap.last_reconcile_at = Some(now_secs());
        }

        if !settings.enabled {
            if !tasks.is_empty() {
                observe::info("bridge disabled; dropping all delivery streams");
            }
            for (_, t) in tasks.drain() {
                t.handle.abort();
            }
            mcp_set.clear();
            acp_enabled.clear();
            state.lock().unwrap().sessions.clear();
        } else {
            let live = fetch_liveness(&http, &settings.aoe_base, &settings.aoe_token).await;
            let ctx = Arc::new(BridgeCtx {
                http: http.clone(),
                aoe_base: settings.aoe_base.clone(),
                aoe_token: settings.aoe_token.clone(),
                conn: conn.clone(),
                state: state.clone(),
                publish: publish.clone(),
            });
            let routes = config::resolve_routes(&settings, &live);
            let mut wanted: HashSet<String> = HashSet::new();

            observe::debug(&format!(
                "reconcile: {} row(s) configured, {} route(s) resolved, {} session(s) live in AoE",
                settings.sessions.len(),
                routes.len(),
                live.len()
            ));

            for route in routes {
                let fingerprint = route.fingerprint(&settings.aoe_base, &settings.aoe_token);
                if route.live {
                    wanted.insert(route.session_id.clone());

                    // Report transport-status from the live record's worker
                    // liveness. A present record with no running worker maps to
                    // `dormant` (not the old, misleading `idle`). `route.live`
                    // is true here, so the record is present; the fallback is
                    // unreachable.
                    let status = live
                        .get(&route.session_id)
                        .map(|r| {
                            classify_transport_status(
                                &r.status,
                                &r.acp_worker_state,
                                r.has_terminal,
                                r.dormant,
                            )
                        })
                        .unwrap_or("idle");

                    ctx.observe(&route.session_id, &route.project, |o| {
                        if !o.live {
                            observe::info(&format!(
                                "session {} is covered and live (project {}, route {})",
                                route.session_id,
                                route.project,
                                route.mode.as_str()
                            ));
                        }
                        o.live = true;
                        o.project = route.project.clone();
                        o.transport_status = status.to_string();
                        o.mode = route.mode.as_str().to_string();
                        o.expose_mcp = route.mcp_url.is_some();
                    });

                    report_status(&ctx, &route, status).await;

                    // Ensure this session's per-session MCP layer matches config:
                    // set agent-mcp's tools when `expose_mcp` yields a url, or
                    // clear our entry when it was on and is now off.
                    ensure_session_mcp(&ctx, &mut mcp_set, &route).await;

                    // Set the MCP first (above), then flip the session to ACP so
                    // the ACP spawn picks up `session_mcp_servers`. Only covered
                    // sessions (expose_mcp on ⇒ mcp_url set) are switched, and
                    // only when the global ensure_acp setting is on.
                    if route.wants_acp(settings.ensure_acp) {
                        ensure_acp_mode(&ctx, &route, &mut acp_enabled).await;
                    }

                    // Ensure a fresh SSE task with the current fingerprint.
                    let needs_spawn = match tasks.get(&route.session_id) {
                        Some(t) => t.fingerprint != fingerprint,
                        None => true,
                    };
                    if needs_spawn {
                        if let Some(old) = tasks.remove(&route.session_id) {
                            old.handle.abort();
                        }
                        ctx.observe(&route.session_id, &route.project, |o| {
                            o.stream = StreamState::Connecting;
                        });
                        let ctx = ctx.clone();
                        let r = route.clone();
                        let sid = route.session_id.clone();
                        let handle = tokio::spawn(async move { run_session_stream(ctx, r).await });
                        tasks.insert(
                            sid,
                            SessionTask {
                                fingerprint,
                                handle,
                            },
                        );
                    }
                } else {
                    // Configured but not currently live: report dead and stop.
                    ctx.observe(&route.session_id, &route.project, |o| {
                        if o.live {
                            observe::info(&format!(
                                "session {} is configured but no longer live; reporting dead",
                                route.session_id
                            ));
                        }
                        o.live = false;
                        o.project = route.project.clone();
                        o.transport_status = "dead".to_string();
                        o.stream = StreamState::Stopped;
                    });
                    report_status(&ctx, &route, "dead").await;
                    if let Some(old) = tasks.remove(&route.session_id) {
                        old.handle.abort();
                    }
                }
            }

            // Tear down tasks for sessions no longer in the mapping.
            let stale: Vec<String> = tasks
                .keys()
                .filter(|k| !wanted.contains(*k))
                .cloned()
                .collect();
            for k in stale {
                if let Some(t) = tasks.remove(&k) {
                    t.handle.abort();
                }
                observe::info(&format!("session {k} left the mapping; coverage dropped"));
                // Drop the MCP guard so a re-added row re-asserts. We do NOT
                // actively clear the session's injected tools here: a removed
                // row stops delivery but leaves the session usable via MCP.
                mcp_set.remove(&k);
                // Drop the ACP guard too so a re-added row re-enables. We do NOT
                // revert the session to terminal: the ACP switch is one-way and
                // leaves the session usable.
                acp_enabled.remove(&k);
            }

            // Sessions that vanished from the settings also vanish from the page.
            state
                .lock()
                .unwrap()
                .sessions
                .retain(|k, _| wanted.contains(k) || tasks.contains_key(k));
        }

        let _ = publish.send(PublishReason::Tick);

        tokio::select! {
            _ = tokio::time::sleep(interval) => {}
            _ = reconcile.notified() => {}
        }
    }
}

/// Push the observable state to the host's `settings-page` slot.
///
/// Coalescing: a burst of state changes (a reconcile touching twenty sessions)
/// drains into a single push, so the host's UI revision moves once. A periodic
/// [`PUBLISH_REFRESH`] tick keeps the page's relative ages honest when nothing
/// is happening.
///
/// A `StatusCommand` reason additionally toasts a one-line summary: the host
/// answers a command invocation `202 {"ok": true}` and forwards it to the worker
/// as a reply-less notification, so there is no HTTP response body for the
/// worker to fill — the toast is the only way an invoked command can answer.
pub async fn run_publisher(
    conn: Arc<PluginConn>,
    state: SharedState,
    mut publish: mpsc::UnboundedReceiver<PublishReason>,
) {
    loop {
        let mut status_command = false;
        tokio::select! {
            reason = publish.recv() => match reason {
                Some(r) => status_command |= r == PublishReason::StatusCommand,
                None => return, // senders all dropped: worker is shutting down.
            },
            _ = tokio::time::sleep(PUBLISH_REFRESH) => {}
        }
        // Coalesce whatever else piled up while we were woken.
        while let Ok(r) = publish.try_recv() {
            status_command |= r == PublishReason::StatusCommand;
        }

        let (payload, summary) = {
            let snap = state.lock().unwrap();
            (
                render_page(
                    &snap,
                    now_secs(),
                    observe::log_path().as_deref(),
                    observe::level(),
                ),
                summary_line(&snap),
            )
        };

        if let Err(e) = conn.ui_state_set(PAGE_SLOT, PAGE_ID, payload).await {
            observe::warn(&format!("publishing the bridge status page failed: {e:#}"));
        }
        if status_command {
            let _ = conn
                .ui_notify("info", "agent-mcp Delivery Bridge", Some(&summary))
                .await;
        }
    }
}

/// AoE web REST `GET {aoe_base}/api/sessions` → id→liveness map. This is the
/// source of both session existence and worker liveness (replacing the plugin
/// `sessions.list`, which cannot tell a stopped session from an idle one).
///
/// Empty on ANY failure (transport, non-2xx, or decode): a failed fetch means
/// no liveness this tick, so no session is treated as live and the next tick
/// retries. The `Authorization` header is sent only when `aoe_token` is
/// non-empty (an `--auth=none` AoE instance needs no bearer).
async fn fetch_liveness(
    http: &reqwest::Client,
    aoe_base: &str,
    aoe_token: &str,
) -> HashMap<String, Liveness> {
    let url = format!("{}/api/sessions", aoe_base.trim_end_matches('/'));
    let mut req = http.get(&url).header(ACCEPT, "application/json");
    if !aoe_token.trim().is_empty() {
        req = req.header(AUTHORIZATION, format!("Bearer {aoe_token}"));
    }
    let value = match req.send().await.and_then(|r| r.error_for_status()) {
        Ok(resp) => match resp.json::<serde_json::Value>().await {
            Ok(v) => v,
            Err(e) => {
                observe::warn(&format!("GET {url} decode failed: {e}"));
                return HashMap::new();
            }
        },
        Err(e) => {
            observe::warn(&format!("GET {url} failed: {e}"));
            return HashMap::new();
        }
    };
    config::parse_liveness(&value)
}

/// Reconcile one live session's per-session MCP layer with its resolved route.
/// - `mcp_url = Some`: assert agent-mcp's tools are set (skipping the RPC when
///   unchanged since we last set them; the host makes an unchanged set a no-op
///   anyway, so a bridge restart re-asserts harmlessly).
/// - `mcp_url = None` but we had set it: clear our entry (the row's `expose_mcp`
///   was turned off) so the session's MCP layer tracks config.
async fn ensure_session_mcp(ctx: &BridgeCtx, guard: &mut HashMap<String, String>, route: &Route) {
    match route.mcp_url.as_deref() {
        Some(url) => {
            let fingerprint = format!("{url}|{}", route.token);
            if guard.get(&route.session_id).map(String::as_str) == Some(fingerprint.as_str()) {
                ctx.observe(&route.session_id, &route.project, |o| o.mcp_asserted = true);
                return; // already asserted with this url+token.
            }
            let params = build_mcp_set_params(&route.session_id, url, &route.token);
            match ctx.conn.session_mcp_set(params).await {
                Ok(_) => {
                    guard.insert(route.session_id.clone(), fingerprint);
                    observe::info(&format!(
                        "agent-mcp tools injected into session {}",
                        route.session_id
                    ));
                    ctx.observe(&route.session_id, &route.project, |o| o.mcp_asserted = true);
                }
                Err(e) => {
                    let msg = format!("session.mcp.set for {} failed: {e:#}", route.session_id);
                    observe::error(&msg);
                    ctx.observe(&route.session_id, &route.project, |o| {
                        o.mcp_asserted = false;
                        o.note_error(&msg, now_secs());
                    });
                }
            }
        }
        None => {
            ctx.observe(&route.session_id, &route.project, |o| {
                o.mcp_asserted = false
            });
            if guard.remove(&route.session_id).is_some() {
                // Clear our layer: empty `servers` replaces it with nothing.
                let params = json!({ "session_id": route.session_id, "servers": [] });
                match ctx.conn.session_mcp_set(params).await {
                    Ok(_) => observe::info(&format!(
                        "agent-mcp tools cleared from session {}",
                        route.session_id
                    )),
                    Err(e) => {
                        let msg =
                            format!("session.mcp clear for {} failed: {e:#}", route.session_id);
                        observe::warn(&msg);
                        ctx.observe(&route.session_id, &route.project, |o| {
                            o.note_error(&msg, now_secs())
                        });
                    }
                }
            }
        }
    }
}

/// Switch a covered session to ACP mode so AoE's per-session MCP
/// (`session_mcp_servers`) actually reaches the agent. Terminal
/// `claude`/`opencode` sessions never load per-session MCP; ACP delivers it
/// agent-agnostically. POSTs `{aoe_base}/api/sessions/{id}/acp/enable`, which is
/// idempotent host-side (a session already structured/ACP is a no-op) — the
/// `guard` set only avoids repeated view-swaps / log spam by calling it at most
/// once per session. Best-effort: a non-ACP-capable agent (or any transport
/// error) is recorded and skipped; it never fails the reconcile.
async fn ensure_acp_mode(ctx: &BridgeCtx, route: &Route, guard: &mut HashSet<String>) {
    let session_id = &route.session_id;
    if !guard.insert(session_id.to_string()) {
        return; // already attempted this session.
    }
    let url = format!("{}/api/sessions/{}/acp/enable", ctx.aoe_base, session_id);
    let mut req = ctx.http.post(&url).json(&json!({}));
    if !ctx.aoe_token.is_empty() {
        req = req.header(AUTHORIZATION, format!("Bearer {}", ctx.aoe_token));
    }
    let msg = match req.send().await {
        Ok(r) if r.status().is_success() => {
            observe::info(&format!("session {session_id} switched to ACP mode"));
            return;
        }
        Ok(r) => {
            let code = r.status().as_u16();
            format!(
                "acp/enable for {session_id} returned HTTP {code}: {}",
                error_body(r).await
            )
        }
        Err(e) => format!("acp/enable for {session_id} failed: {e}"),
    };
    observe::warn(&msg);
    ctx.observe(session_id, &route.project, |o| {
        o.note_error(&msg, now_secs())
    });
}

/// POST the session's transport-status up the delivery channel. Best-effort.
async fn report_status(ctx: &BridgeCtx, route: &Route, status: &str) {
    let url = delivery_url(&route.endpoint, "delivery/status");
    let res = ctx
        .http
        .post(url.as_str())
        .header(AUTHORIZATION, format!("Bearer {}", route.token))
        .json(&json!({ "status": status }))
        .send()
        .await;
    let msg = match res {
        Ok(r) if r.status().is_success() => {
            observe::debug(&format!(
                "reported transport-status {status} for {}",
                route.session_id
            ));
            return;
        }
        Ok(r) => {
            let code = r.status().as_u16();
            format!(
                "status POST for {} returned HTTP {code}: {}",
                route.session_id,
                error_body(r).await
            )
        }
        Err(e) => format!("status POST for {} failed: {e}", route.session_id),
    };
    observe::warn(&msg);
    ctx.observe(&route.session_id, &route.project, |o| {
        o.note_error(&msg, now_secs())
    });
}

/// Hold one SSE stream for a session, reconnecting with capped backoff. A
/// transient drop is not a session end, so we always reconnect.
async fn run_session_stream(ctx: Arc<BridgeCtx>, route: Route) {
    let mut backoff = Duration::from_secs(1);
    let mut attempt: u32 = 0;
    loop {
        let msg = match stream_once(&ctx, &route).await {
            Ok(()) => {
                backoff = Duration::from_secs(1);
                format!(
                    "delivery stream for {} ended; reconnecting",
                    route.session_id
                )
            }
            Err(e) => format!(
                "delivery stream for {} error: {e:#}; reconnecting",
                route.session_id
            ),
        };
        attempt = attempt.saturating_add(1);
        observe::warn(&msg);
        ctx.observe(&route.session_id, &route.project, |o| {
            o.stream = StreamState::Reconnecting { attempt };
            o.note_error(&msg, now_secs());
        });
        ctx.request_publish();
        tokio::time::sleep(backoff).await;
        backoff = (backoff * 2).min(Duration::from_secs(30));
    }
}

/// One SSE connection: subscribe, parse events, inject each frame. Returns
/// `Ok(())` when the stream cleanly ends (server closed), `Err` on transport
/// failure.
async fn stream_once(ctx: &BridgeCtx, route: &Route) -> Result<()> {
    let url = delivery_url(&route.endpoint, "delivery/stream");
    let resp = ctx
        .http
        .get(url.as_str())
        .header(AUTHORIZATION, format!("Bearer {}", route.token))
        .header(ACCEPT, "text/event-stream")
        .send()
        .await
        .context("connect delivery stream")?
        .error_for_status()
        .context("delivery stream status")?;

    observe::info(&format!(
        "delivery stream connected for {}",
        route.session_id
    ));
    ctx.observe(&route.session_id, &route.project, |o| {
        o.stream = StreamState::Connected;
    });
    ctx.request_publish();

    let mut stream = resp.bytes_stream();
    let mut buf = String::new();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.context("read delivery chunk")?;
        // Normalise CRLF to LF so the "\n\n" event boundary search is simple;
        // dropping bare '\r' is safe for SSE (CR is not significant in a field).
        buf.push_str(&String::from_utf8_lossy(&chunk).replace('\r', ""));

        while let Some(idx) = buf.find("\n\n") {
            let block: String = buf.drain(..idx + 2).collect();
            if let Some(data) = extract_sse_data(&block) {
                handle_frame(ctx, route, &data).await;
            }
        }
    }
    Ok(())
}

/// Render one delivery frame skinny and inject it into the session.
async fn handle_frame(ctx: &BridgeCtx, route: &Route, data: &str) {
    let frame = match parse_frame(data) {
        Ok(f) => f,
        Err(e) => {
            let msg = format!("bad delivery frame for {}: {e}", route.session_id);
            observe::warn(&msg);
            ctx.observe(&route.session_id, &route.project, |o| {
                o.note_error(&msg, now_secs())
            });
            return;
        }
    };
    if frame.kind != "delivery" {
        return; // ignore any non-delivery event (keepalives, future types).
    }

    // `reason` is a fixed enum (unread_messages / unfinished_tasks /
    // unassigned_tasks). Counts and reason are all that is recorded — never the
    // subjects, senders or task titles the frame also carries.
    let now = now_secs();
    ctx.observe(&route.session_id, &route.project, |o| {
        o.note_frame(&frame.reason, now)
    });
    observe::debug(&format!(
        "delivery frame for {} ({}, {} unread / {} tasks); injecting via {}",
        route.session_id,
        frame.reason,
        frame.unread_count,
        frame.task_count,
        route.mode.as_str()
    ));

    let text = render_skinny(&frame);
    let (url, body) = build_injection(route.mode, &ctx.aoe_base, &route.session_id, &text);
    let res = ctx
        .http
        .post(url.as_str())
        .header(AUTHORIZATION, format!("Bearer {}", ctx.aoe_token))
        .json(&body)
        .send()
        .await;

    let outcome = match res {
        Ok(r) if r.status().is_success() => {
            let code = r.status().as_u16();
            observe::debug(&format!(
                "injected into {} via {} (HTTP {code})",
                route.session_id,
                route.mode.as_str()
            ));
            InjectOutcome {
                ok: true,
                http: Some(code),
                detail: String::new(),
                at: now_secs(),
            }
        }
        Ok(r) => {
            // The BODY is the whole point: AoE answers `{"error": <code>,
            // "message": …}`, and it is that code (e.g. acp_mode_unsupported)
            // that says whether the route or the session is wrong.
            let code = r.status().as_u16();
            let detail = error_body(r).await;
            observe::error(&format!(
                "inject to {} via {} returned HTTP {code}: {detail}",
                route.session_id,
                route.mode.as_str()
            ));
            InjectOutcome {
                ok: false,
                http: Some(code),
                detail,
                at: now_secs(),
            }
        }
        Err(e) => {
            let detail = clip_detail(&e.to_string());
            observe::error(&format!("inject to {} failed: {detail}", route.session_id));
            InjectOutcome {
                ok: false,
                http: None,
                detail,
                at: now_secs(),
            }
        }
    };

    let notice = ctx.observe(&route.session_id, &route.project, |o| {
        o.note_inject(outcome.clone())
    });
    ctx.request_publish();
    announce(ctx, route, notice, &outcome).await;
}

/// Surface an inject failure (or its recovery) to the operator as a toast.
///
/// Rate-limited by [`crate::status::decide_notice`]: the first failure of a run
/// toasts immediately, then at most once per cooldown while it keeps failing.
/// Without that gate a session flapping on every frame would evict the host's
/// entire 200-entry notification ring.
async fn announce(ctx: &BridgeCtx, route: &Route, notice: Notice, outcome: &InjectOutcome) {
    let (tone, title, body) = match notice {
        Notice::None => return,
        Notice::Failing => (
            "danger",
            format!("Delivery inject failed: {}", route.session_id),
            format!(
                "agent-mcp nudges are being dropped for this session ({}). {}",
                match outcome.http {
                    Some(code) => format!("HTTP {code}"),
                    None => "no response".to_string(),
                },
                if outcome.detail.is_empty() {
                    "See the agent-mcp Delivery settings page.".to_string()
                } else {
                    outcome.detail.clone()
                }
            ),
        ),
        Notice::Recovered => (
            "success",
            format!("Delivery inject recovered: {}", route.session_id),
            "Nudges are reaching this session again.".to_string(),
        ),
    };
    if let Err(e) = ctx.conn.ui_notify(tone, &title, Some(&body)).await {
        observe::warn(&format!("ui.notify failed: {e:#}"));
    }
}

/// Read a non-2xx response body, clipped and redacted, for capture into the
/// page and the log. An unreadable body yields `"<no body>"` rather than an
/// empty string, so "we could not read it" is distinguishable from "it was
/// empty".
async fn error_body(resp: Response) -> String {
    match resp.text().await {
        Ok(t) if t.trim().is_empty() => "<no body>".to_string(),
        Ok(t) => clip_detail(&observe::redact(&t)),
        Err(e) => format!("<unreadable body: {e}>"),
    }
}
