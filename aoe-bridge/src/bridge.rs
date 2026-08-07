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
//! 6. drop tasks whose session left the mapping.
//!
//! A per-session SSE task reconnects with backoff on drop — a transient stream
//! drop is not a session end (ADR-0021); the policy re-fires on reconnect.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use futures_util::StreamExt;
use reqwest::header::{ACCEPT, AUTHORIZATION};
use serde_json::json;
use tokio::sync::Notify;
use tokio::task::JoinHandle;

use crate::config::{self, Liveness, Route};
use crate::inject::{build_injection, build_mcp_set_params, delivery_url, extract_sse_data};
use crate::mode::classify_transport_status;
use crate::plugin::PluginConn;
use crate::render::{parse_frame, render_skinny};

/// Shared per-task context: the HTTP client plus the AoE injection creds.
struct BridgeCtx {
    http: reqwest::Client,
    aoe_base: String,
    aoe_token: String,
}

/// A running per-session SSE task and the route fingerprint it was spawned for.
struct SessionTask {
    fingerprint: String,
    handle: JoinHandle<()>,
}

/// The reconcile loop. Runs until the process exits (stdin EOF aborts it).
pub async fn run_bridge(conn: Arc<PluginConn>, reconcile: Arc<Notify>) {
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

        if !settings.enabled {
            for (_, t) in tasks.drain() {
                t.handle.abort();
            }
            mcp_set.clear();
            acp_enabled.clear();
        } else {
            let live = fetch_liveness(&http, &settings.aoe_base, &settings.aoe_token).await;
            let ctx = Arc::new(BridgeCtx {
                http: http.clone(),
                aoe_base: settings.aoe_base.clone(),
                aoe_token: settings.aoe_token.clone(),
            });
            let routes = config::resolve_routes(&settings, &live);
            let mut wanted: HashSet<String> = HashSet::new();

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
                    report_status(&ctx, &route.endpoint, &route.token, status).await;

                    // Ensure this session's per-session MCP layer matches config:
                    // set agent-mcp's tools when `expose_mcp` yields a url, or
                    // clear our entry when it was on and is now off.
                    ensure_session_mcp(&conn, &mut mcp_set, &route).await;

                    // Set the MCP first (above), then flip the session to ACP so
                    // the ACP spawn picks up `session_mcp_servers`. Only covered
                    // sessions (expose_mcp on ⇒ mcp_url set) are switched, and
                    // only when the global ensure_acp setting is on.
                    if route.wants_acp(settings.ensure_acp) {
                        ensure_acp_mode(&ctx, &route.session_id, &mut acp_enabled).await;
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
                    report_status(&ctx, &route.endpoint, &route.token, "dead").await;
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
                // Drop the MCP guard so a re-added row re-asserts. We do NOT
                // actively clear the session's injected tools here: a removed
                // row stops delivery but leaves the session usable via MCP.
                mcp_set.remove(&k);
                // Drop the ACP guard too so a re-added row re-enables. We do NOT
                // revert the session to terminal: the ACP switch is one-way and
                // leaves the session usable.
                acp_enabled.remove(&k);
            }
        }

        tokio::select! {
            _ = tokio::time::sleep(interval) => {}
            _ = reconcile.notified() => {}
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
                crate::log(&format!("GET {url} decode failed: {e}"));
                return HashMap::new();
            }
        },
        Err(e) => {
            crate::log(&format!("GET {url} failed: {e}"));
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
async fn ensure_session_mcp(
    conn: &Arc<PluginConn>,
    guard: &mut HashMap<String, String>,
    route: &Route,
) {
    match route.mcp_url.as_deref() {
        Some(url) => {
            let fingerprint = format!("{url}|{}", route.token);
            if guard.get(&route.session_id).map(String::as_str) == Some(fingerprint.as_str()) {
                return; // already asserted with this url+token.
            }
            let params = build_mcp_set_params(&route.session_id, url, &route.token);
            match conn.session_mcp_set(params).await {
                Ok(_) => {
                    guard.insert(route.session_id.clone(), fingerprint);
                }
                Err(e) => crate::log(&format!(
                    "session.mcp.set for {} failed: {e:#}",
                    route.session_id
                )),
            }
        }
        None => {
            if guard.remove(&route.session_id).is_some() {
                // Clear our layer: empty `servers` replaces it with nothing.
                let params = json!({ "session_id": route.session_id, "servers": [] });
                if let Err(e) = conn.session_mcp_set(params).await {
                    crate::log(&format!(
                        "session.mcp clear for {} failed: {e:#}",
                        route.session_id
                    ));
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
/// error) is logged and skipped; it never fails the reconcile.
async fn ensure_acp_mode(ctx: &BridgeCtx, session_id: &str, guard: &mut HashSet<String>) {
    if !guard.insert(session_id.to_string()) {
        return; // already attempted this session.
    }
    let url = format!("{}/api/sessions/{}/acp/enable", ctx.aoe_base, session_id);
    let mut req = ctx.http.post(&url).json(&json!({}));
    if !ctx.aoe_token.is_empty() {
        req = req.header(AUTHORIZATION, format!("Bearer {}", ctx.aoe_token));
    }
    match req.send().await {
        Ok(r) if r.status().is_success() => {}
        Ok(r) => crate::log(&format!(
            "acp/enable for {session_id} returned HTTP {}",
            r.status()
        )),
        Err(e) => crate::log(&format!("acp/enable for {session_id} failed: {e}")),
    }
}

/// POST the session's transport-status up the delivery channel. Best-effort.
async fn report_status(ctx: &BridgeCtx, endpoint: &str, token: &str, status: &str) {
    let url = delivery_url(endpoint, "delivery/status");
    let res = ctx
        .http
        .post(url.as_str())
        .header(AUTHORIZATION, format!("Bearer {token}"))
        .json(&json!({ "status": status }))
        .send()
        .await;
    if let Err(e) = res {
        crate::log(&format!("status POST to {url} failed: {e}"));
    }
}

/// Hold one SSE stream for a session, reconnecting with capped backoff. A
/// transient drop is not a session end, so we always reconnect.
async fn run_session_stream(ctx: Arc<BridgeCtx>, route: Route) {
    let mut backoff = Duration::from_secs(1);
    loop {
        match stream_once(&ctx, &route).await {
            Ok(()) => {
                crate::log(&format!(
                    "delivery stream for {} ended; reconnecting",
                    route.session_id
                ));
                backoff = Duration::from_secs(1);
            }
            Err(e) => {
                crate::log(&format!(
                    "delivery stream for {} error: {e:#}; reconnecting",
                    route.session_id
                ));
            }
        }
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
            crate::log(&format!("bad delivery frame for {}: {e}", route.session_id));
            return;
        }
    };
    if frame.kind != "delivery" {
        return; // ignore any non-delivery event (keepalives, future types).
    }

    let text = render_skinny(&frame);
    let (url, body) = build_injection(route.mode, &ctx.aoe_base, &route.session_id, &text);
    let res = ctx
        .http
        .post(url.as_str())
        .header(AUTHORIZATION, format!("Bearer {}", ctx.aoe_token))
        .json(&body)
        .send()
        .await;
    match res {
        Ok(r) if r.status().is_success() => {}
        Ok(r) => crate::log(&format!(
            "inject to {} returned HTTP {}",
            route.session_id,
            r.status()
        )),
        Err(e) => crate::log(&format!("inject to {} failed: {e}", route.session_id)),
    }
}
