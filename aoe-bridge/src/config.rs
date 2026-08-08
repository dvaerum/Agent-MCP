//! Settings + the `session → route` mapping resolver.
//!
//! ## The model: one base, everything derived
//!
//! There is exactly ONE agent-mcp URL to configure — `agent_mcp_base`, the
//! BARE router address you reach it at (on the same host `http://127.0.0.1:1337`,
//! no `/agent-mcp`). The `/agent-mcp` prefix is a reverse-proxy concern (a front
//! door may serve the router under it externally), not part of this base. Every
//! per-session URL is derived from it plus the row's `project`:
//!
//! ```text
//!   delivery = <agent_mcp_base>/api/<project>   (SSE /delivery/stream + status POST)
//!   mcp      = <agent_mcp_base>/mcp/<project>   (injected per-session MCP server)
//! ```
//!
//! So a covered-session row carries only identity — `session_id`, `token`,
//! `project` (+ optional `expose_mcp`, `mode`) — never a URL. The one token
//! drives both surfaces: an agent-mcp agent token authenticates the delivery
//! stream AND the MCP transport. The operator supplies it per session (minted
//! via agent-mcp's `register_agent`); the bridge wires delivery (always) and MCP
//! (when `expose_mcp`, over `session.mcp.set`) — no separate provisioning step.
//!
//! `aoe_base` is a SEPARATE concern: the AoE-side REST the bridge injects INTO
//! (`/api/sessions/<id>/send|acp/prompt`), not an agent-mcp url.
//!
//! The bridge sources all of this from **its own plugin settings**, read over
//! the `config.get` host RPC (which only ever returns this plugin's own table).
//!
//! ## Assumptions (see README)
//! - `session_id` matches `sessions.list[].id` (stable across respawn).
//! - `agent_mcp_base` is the bare router address (no reverse-proxy prefix); the
//!   bridge appends `/api/<project>` (+ `/delivery/stream|status`) and
//!   `/mcp/<project>`.
//! - `token` == the session's agent-mcp bearer; it authenticates BOTH the
//!   delivery stream and the injected MCP server.
//! - `sessions.list` exposes no definitive terminal/structured flag, so `auto`
//!   is best-effort (it inspects `tool` + `status`); set `mode` explicitly to
//!   `structured` for ACP / CityHall / composer sessions.

use std::collections::HashMap;

use serde::Deserialize;
use serde_json::Value;

use crate::mode::{normalize_mode, Mode};
use crate::plugin::PluginConn;

/// `expose_mcp` defaults to true: covering a session normally means you also
/// want it to hold agent-mcp's tools, not just receive the fallback push.
fn default_true() -> bool {
    true
}

/// Resolved plugin settings for one reconcile pass.
#[derive(Debug, Clone)]
pub struct Settings {
    pub enabled: bool,
    /// AoE serve REST base — where the bridge POSTs to INJECT nudges into a
    /// session (AoE's own `/api/sessions/<id>/send|acp/prompt`). Distinct from
    /// `agent_mcp_base`: this is the AoE side. E.g. `http://127.0.0.1:8080`.
    pub aoe_base: String,
    /// AoE serve bearer token, only if this AoE instance runs with auth. Empty
    /// for a `--auth=none` instance.
    pub aoe_token: String,
    /// The BARE agent-mcp router address shared by all covered sessions (e.g.
    /// `http://127.0.0.1:1337`, no `/agent-mcp` — that prefix is a reverse-proxy
    /// concern, not part of this base). The bridge derives BOTH per-session URLs
    /// from it + the row's `project`:
    ///   delivery = `<agent_mcp_base>/api/<project>`   (SSE + status POST)
    ///   mcp      = `<agent_mcp_base>/mcp/<project>`   (injected MCP server)
    /// Blank ⇒ no routes resolve (nothing to point at).
    pub agent_mcp_base: String,
    /// How often to re-resolve routes and re-post transport-status.
    pub status_interval_secs: u64,
    /// When true (default), each covered session that gets MCP injected is also
    /// switched to ACP mode (a one-time terminal→ACP view swap) so AoE's
    /// per-session MCP (`session_mcp_servers`) actually reaches the agent —
    /// terminal `claude`/`opencode` sessions never load per-session MCP, ACP
    /// delivers it agent-agnostically. Only sessions with `expose_mcp` on are
    /// switched; delivery-only rows are left untouched.
    pub ensure_acp: bool,
    /// Per-session mapping rows.
    pub sessions: Vec<SessionEntry>,
}

/// One row of the `sessions` object-list setting. A row is just the identity of
/// a covered session — everything URL-shaped is derived from `agent_mcp_base` +
/// `project`, so there is no per-row endpoint to keep in sync.
#[derive(Debug, Clone, Deserialize)]
pub struct SessionEntry {
    /// AoE session id (matches `sessions.list[].id`). Required.
    #[serde(default)]
    pub session_id: String,
    /// The session's agent-mcp bearer token. Authenticates BOTH the delivery
    /// stream and the injected MCP server. Required. Empty ⇒ the row is skipped.
    #[serde(default)]
    pub token: String,
    /// agent-mcp project this session acts as. Appended to `agent_mcp_base` for
    /// both the delivery (`/api/<project>`) and MCP (`/mcp/<project>`) urls.
    /// Required. Empty ⇒ the row is skipped.
    #[serde(default)]
    pub project: String,
    /// Whether to also inject agent-mcp's tools into this session as a
    /// per-session MCP server (over `session.mcp.set`). Delivery fires
    /// regardless; this only gates the MCP-tools half. Defaults to true.
    #[serde(default = "default_true")]
    pub expose_mcp: bool,
    /// `auto` | `terminal` | `structured`. How a nudge is injected.
    #[serde(default)]
    pub mode: String,
}

impl Default for SessionEntry {
    fn default() -> Self {
        Self {
            session_id: String::new(),
            token: String::new(),
            project: String::new(),
            expose_mcp: true,
            mode: String::new(),
        }
    }
}

/// A live session's liveness as seen by AoE's web REST `GET /api/sessions`
/// (only the fields we use). This is richer than the plugin `sessions.list`
/// (which exposes just id/title/tool/status): `acp_worker_state`,
/// `has_terminal`, and `dormant` let the bridge tell a STOPPED session (no
/// running worker) apart from a merely idle one — the plugin RPC reports both
/// as `status="Idle"`, so it cannot make that distinction.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct Liveness {
    #[serde(default)]
    pub id: String,
    #[serde(default)]
    pub tool: String,
    #[serde(default)]
    pub status: String,
    /// ACP worker liveness, e.g. `"absent"` when no ACP worker is running.
    #[serde(default)]
    pub acp_worker_state: String,
    /// Whether a terminal (tmux) worker is attached to the session.
    #[serde(default)]
    pub has_terminal: bool,
    /// AoE's own dormant flag (session parked / no running worker).
    #[serde(default)]
    pub dormant: bool,
}

/// Parse the `GET /api/sessions` body into an id→liveness map. Accepts either
/// the wrapped `{"sessions":[...]}` shape or a bare `[...]` array. Rows without
/// an `id` or that fail to deserialize are dropped (defensive: a bad row must
/// not sink the whole tick).
pub fn parse_liveness(value: &Value) -> HashMap<String, Liveness> {
    let arr = value
        .get("sessions")
        .and_then(Value::as_array)
        .or_else(|| value.as_array());
    let mut map = HashMap::new();
    if let Some(arr) = arr {
        for item in arr {
            if let Ok(rec) = serde_json::from_value::<Liveness>(item.clone()) {
                if !rec.id.is_empty() {
                    map.insert(rec.id.clone(), rec);
                }
            }
        }
    }
    map
}

/// A fully-resolved delivery route for one session.
#[derive(Debug, Clone)]
pub struct Route {
    pub session_id: String,
    /// The agent-mcp project this session acts as. Not used to build URLs (the
    /// endpoints below already embed it) — carried so the observability surface
    /// can name the project without re-deriving it from a URL.
    pub project: String,
    pub endpoint: String,
    pub token: String,
    pub mode: Mode,
    /// The agent-mcp MCP url to inject (`<agent_mcp_base>/mcp/<project>`), or
    /// `None` when MCP injection is off for this row (`expose_mcp` false).
    /// Delivery is independent of this.
    pub mcp_url: Option<String>,
    /// Whether the session is currently present in `sessions.list`.
    pub live: bool,
    /// Whether the row left the mode to `auto` (i.e. did NOT pin `terminal` or
    /// `structured`). Only an `auto` route may have its mode corrected mid-pass
    /// by [`Route::promote_structured_after_acp`]; an operator's explicit pin is
    /// never silently overridden.
    pub auto_mode: bool,
}

impl Route {
    /// Whether this route should be switched to ACP mode. Only covered sessions
    /// that actually get MCP injected (`mcp_url` set, i.e. `expose_mcp` on) are
    /// candidates, and only when the global `ensure_acp` setting is on —
    /// delivery-only rows are never switched.
    pub fn wants_acp(&self, ensure_acp: bool) -> bool {
        ensure_acp && self.mcp_url.is_some()
    }

    /// Correct an `auto` route to [`Mode::Structured`] after the bridge has just
    /// switched this session to ACP. Returns whether the mode changed.
    ///
    /// The reconcile pass resolves modes from a liveness snapshot taken BEFORE
    /// `acp/enable` runs, so on the pass that first switches a session the
    /// snapshot still reads `acp_worker_state = "absent"` and `auto` infers
    /// [`Mode::Terminal`]. Every frame arriving before the NEXT pass would then
    /// be POSTed to the terminal `/send`, which answers `400
    /// acp_mode_unsupported` — the nudge is dropped for up to
    /// `status_interval_secs`. Applying this the moment the flip succeeds closes
    /// that window, including for the SSE task spawned with the route.
    pub fn promote_structured_after_acp(&mut self) -> bool {
        if !self.auto_mode || self.mode == Mode::Structured {
            return false;
        }
        self.mode = Mode::Structured;
        true
    }

    /// A change in any of these means the running per-session task must be torn
    /// down and respawned (it captured the old values).
    pub fn fingerprint(&self, aoe_base: &str, aoe_token: &str) -> String {
        format!(
            "{}|{}|{}|{}|{}",
            self.endpoint,
            self.token,
            self.mode.as_str(),
            aoe_base,
            aoe_token
        )
    }
}

/// Join the agent-mcp base with a sub-path (`api/<project>` or `mcp/<project>`),
/// tolerant of a trailing slash on the base.
fn join_base(agent_mcp_base: &str, kind: &str, project: &str) -> String {
    format!(
        "{}/{}/{}",
        agent_mcp_base.trim().trim_end_matches('/'),
        kind,
        project.trim().trim_matches('/')
    )
}

/// Infer the injection [`Mode`] for a `mode = "auto"` row from its live
/// liveness record. A session with an active ACP worker must be injected via
/// `/acp/prompt`; its terminal `/send` returns 400 `acp_mode_unsupported`. So a
/// live ACP worker forces [`Mode::Structured`] regardless of the tool/status
/// text (a `claude` session switched to ACP by `ensure_acp` still reports
/// `tool="claude" status="Idle"`). Without an ACP worker we fall back to the
/// tool/status keyword heuristic ([`normalize_mode`]).
fn infer_auto_mode(r: &Liveness) -> Mode {
    let acp_active = !matches!(r.acp_worker_state.as_str(), "" | "absent");
    if acp_active {
        Mode::Structured
    } else {
        normalize_mode(&format!("{} {}", r.tool, r.status))
    }
}

/// Resolve every configured session entry into a [`Route`]. Both the delivery
/// endpoint and the MCP url are DERIVED from `agent_mcp_base` + the row's
/// `project`, so a row is self-contained given the one global base. A row is
/// dropped unless it has a `session_id`, a `token`, a `project`, and the global
/// `agent_mcp_base` is set. Mode + liveness come from AoE's web REST liveness
/// map (`GET /api/sessions`).
pub fn resolve_routes(settings: &Settings, live: &HashMap<String, Liveness>) -> Vec<Route> {
    let base = settings.agent_mcp_base.trim();
    if base.is_empty() {
        return Vec::new(); // nothing to point at.
    }
    let mut out = Vec::new();
    for entry in &settings.sessions {
        if entry.session_id.trim().is_empty()
            || entry.token.trim().is_empty()
            || entry.project.trim().is_empty()
        {
            continue;
        }
        let record = live.get(&entry.session_id);
        let raw_mode = entry.mode.trim().to_lowercase();
        let mode = match raw_mode.as_str() {
            "terminal" => Mode::Terminal,
            "structured" => Mode::Structured,
            // "auto" (or unset/unknown): infer from the live record. A session
            // AoE runs as an ACP worker MUST be injected via /acp/prompt — its
            // terminal /send returns 400 acp_mode_unsupported — so a live ACP
            // worker forces Structured even when tool/status read terminal-like
            // ("claude Idle"), which is exactly what ensure_acp produces. Only a
            // non-ACP live record falls back to the tool/status keyword
            // heuristic; a not-live session defaults terminal.
            _ => match record {
                Some(r) => infer_auto_mode(r),
                None => Mode::Terminal,
            },
        };
        out.push(Route {
            session_id: entry.session_id.clone(),
            project: entry.project.trim().trim_matches('/').to_string(),
            endpoint: join_base(base, "api", &entry.project),
            token: entry.token.clone(),
            mode,
            mcp_url: if entry.expose_mcp {
                Some(join_base(base, "mcp", &entry.project))
            } else {
                None
            },
            live: record.is_some(),
            auto_mode: !matches!(raw_mode.as_str(), "terminal" | "structured"),
        });
    }
    out
}

/// Parse the raw `config.get` value of the `sessions` object-list into entries.
/// Non-array values and malformed rows are dropped (defensive: a bad row must
/// not sink the whole mapping).
pub fn parse_session_entries(value: &Value) -> Vec<SessionEntry> {
    match value.as_array() {
        Some(arr) => arr
            .iter()
            .filter_map(|row| serde_json::from_value::<SessionEntry>(row.clone()).ok())
            .collect(),
        None => Vec::new(),
    }
}

// ---- config.get plumbing -------------------------------------------------

/// The `value` field of a `config.get` response, or `Null` on any RPC error
/// (so a transient host hiccup falls back to defaults rather than crashing).
async fn get_value(conn: &PluginConn, key: &str) -> Value {
    match conn.config_get(key).await {
        Ok(resp) => resp.get("value").cloned().unwrap_or(Value::Null),
        Err(e) => {
            crate::observe::warn(&format!("config.get {key} failed: {e}"));
            Value::Null
        }
    }
}

/// Load all settings for one reconcile pass.
pub async fn load_settings(conn: &PluginConn) -> Settings {
    let enabled = get_value(conn, "enabled").await.as_bool().unwrap_or(true);
    let aoe_base = get_value(conn, "aoe_base")
        .await
        .as_str()
        .map(str::to_string)
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "http://127.0.0.1:8080".to_string());
    let aoe_token = get_value(conn, "aoe_token")
        .await
        .as_str()
        .map(str::to_string)
        .unwrap_or_default();
    let agent_mcp_base = get_value(conn, "agent_mcp_base")
        .await
        .as_str()
        .map(str::to_string)
        .unwrap_or_default();
    let status_interval_secs = get_value(conn, "status_interval_secs")
        .await
        .as_u64()
        .unwrap_or(30);
    let ensure_acp = get_value(conn, "ensure_acp")
        .await
        .as_bool()
        .unwrap_or(true);
    let sessions = parse_session_entries(&get_value(conn, "sessions").await);

    Settings {
        enabled,
        aoe_base,
        aoe_token,
        agent_mcp_base,
        status_interval_secs,
        ensure_acp,
        sessions,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn live_map(records: &[(&str, &str, &str)]) -> HashMap<String, Liveness> {
        records
            .iter()
            .map(|(id, tool, status)| {
                (
                    id.to_string(),
                    Liveness {
                        id: id.to_string(),
                        tool: tool.to_string(),
                        status: status.to_string(),
                        ..Default::default()
                    },
                )
            })
            .collect()
    }

    fn settings(sessions: Vec<SessionEntry>, agent_mcp_base: &str) -> Settings {
        Settings {
            enabled: true,
            aoe_base: "http://127.0.0.1:8080".to_string(),
            aoe_token: "aoe-tok".to_string(),
            agent_mcp_base: agent_mcp_base.to_string(),
            status_interval_secs: 30,
            ensure_acp: true,
            sessions,
        }
    }

    fn entry(session_id: &str, token: &str, project: &str, mode: &str) -> SessionEntry {
        SessionEntry {
            session_id: session_id.into(),
            token: token.into(),
            project: project.into(),
            mode: mode.into(),
            ..Default::default()
        }
    }

    #[test]
    fn parse_entries_drops_non_array_and_bad_rows() {
        assert!(parse_session_entries(&Value::Null).is_empty());
        let v = json!([
            { "session_id": "s1", "token": "t1", "project": "p", "mode": "terminal" },
            { "session_id": "s2", "token": "t2", "project": "p" },
            42
        ]);
        let entries = parse_session_entries(&v);
        assert_eq!(entries.len(), 2);
        assert_eq!(entries[0].session_id, "s1");
        assert_eq!(entries[1].mode, "");
    }

    #[test]
    fn derives_both_urls_from_base_and_project() {
        // A trailing slash on the base is tolerated.
        let entries = vec![entry("s1", "t1", "washing", "auto")];
        let live = live_map(&[("s1", "claude", "Running")]);
        let routes = resolve_routes(&settings(entries, "https://host/agent-mcp/"), &live);
        assert_eq!(routes.len(), 1);
        assert_eq!(routes[0].endpoint, "https://host/agent-mcp/api/washing");
        assert_eq!(
            routes[0].mcp_url.as_deref(),
            Some("https://host/agent-mcp/mcp/washing")
        );
        assert!(routes[0].live);
    }

    #[test]
    fn auto_mode_inferred_from_live_record() {
        let entries = vec![entry("s1", "t1", "p", "auto"), entry("s2", "t2", "p", "")];
        let live = live_map(&[("s1", "claude", "acp running"), ("s2", "claude", "Running")]);
        let routes = resolve_routes(&settings(entries, "https://h/agent-mcp"), &live);
        // s1: status mentions acp -> structured; s2: terminal-ish -> terminal.
        assert_eq!(routes[0].mode, Mode::Structured);
        assert_eq!(routes[1].mode, Mode::Terminal);
    }

    #[test]
    fn auto_mode_infers_structured_for_acp_worker_despite_terminal_toolstatus() {
        // Regression (ensure_acp interaction): a covered session switched to ACP
        // reports tool="claude" status="Idle" (terminal-looking) but
        // acp_worker_state="running" + has_terminal=false. AoE's terminal /send
        // returns 400 acp_mode_unsupported for such a session, so auto-mode MUST
        // resolve Structured (/acp/prompt), not Terminal — otherwise every
        // delivery nudge is dropped.
        let mut live = HashMap::new();
        live.insert(
            "s1".to_string(),
            Liveness {
                id: "s1".into(),
                tool: "claude".into(),
                status: "Idle".into(),
                acp_worker_state: "running".into(),
                has_terminal: false,
                dormant: false,
            },
        );
        let routes = resolve_routes(
            &settings(vec![entry("s1", "t", "p", "auto")], "https://h"),
            &live,
        );
        assert_eq!(routes[0].mode, Mode::Structured);
    }

    /// One live record with full control over the liveness fields.
    fn liveness(id: &str, tool: &str, status: &str, acp: &str, term: bool) -> Liveness {
        Liveness {
            id: id.into(),
            tool: tool.into(),
            status: status.into(),
            acp_worker_state: acp.into(),
            has_terminal: term,
            dormant: false,
        }
    }

    #[test]
    fn acp_switch_promotes_auto_route_to_structured_same_pass() {
        // Regression for the live 400 acp_mode_unsupported window: the pass that
        // FIRST switches a session to ACP resolved its route from a liveness
        // snapshot taken before the switch (acp_worker_state="absent"), so an
        // `auto` row infers Terminal. Every frame arriving before the next
        // reconcile (up to status_interval_secs) would be POSTed to the terminal
        // /send, which answers 400 acp_mode_unsupported and drops the nudge. The
        // successful ACP flip must correct the route immediately.
        let mut live = HashMap::new();
        live.insert(
            "s1".to_string(),
            liveness("s1", "claude", "Idle", "absent", true),
        );
        let mut routes = resolve_routes(
            &settings(vec![entry("s1", "t", "p", "auto")], "https://h"),
            &live,
        );
        let route = &mut routes[0];
        // Pre-flip: the stale snapshot infers Terminal, and this row is an
        // ensure_acp candidate (expose_mcp defaults on).
        assert_eq!(route.mode, Mode::Terminal);
        assert!(route.wants_acp(true));

        // The flip succeeds -> the route is corrected for the rest of the pass.
        assert!(route.promote_structured_after_acp());
        assert_eq!(route.mode, Mode::Structured);

        // ...which is what the SSE task spawned with this route injects through.
        let (url, _) = crate::inject::build_injection(route.mode, "http://a:8080", "s1", "hi");
        assert_eq!(url, "http://a:8080/api/sessions/s1/acp/prompt");

        // Idempotent: a later pass re-asserting the known-ACP state is a no-op.
        assert!(!route.promote_structured_after_acp());
        assert_eq!(route.mode, Mode::Structured);
    }

    #[test]
    fn explicit_terminal_row_is_never_promoted_after_acp_switch() {
        // An operator who pinned mode="terminal" keeps it, even if the bridge
        // switched the session to ACP: an explicit choice is never silently
        // overridden (same principle as `explicit_mode_overrides_inference`).
        let mut live = HashMap::new();
        live.insert(
            "s1".to_string(),
            liveness("s1", "claude", "Idle", "absent", true),
        );
        let mut routes = resolve_routes(
            &settings(vec![entry("s1", "t", "p", "terminal")], "https://h"),
            &live,
        );
        assert!(!routes[0].promote_structured_after_acp());
        assert_eq!(routes[0].mode, Mode::Terminal);

        // An explicitly-structured row is likewise left alone (already correct).
        let mut routes = resolve_routes(
            &settings(vec![entry("s1", "t", "p", "structured")], "https://h"),
            &live,
        );
        assert!(!routes[0].promote_structured_after_acp());
        assert_eq!(routes[0].mode, Mode::Structured);
    }

    #[test]
    fn promoted_route_fingerprint_matches_the_next_pass_so_no_respawn() {
        // `mode` is part of the task fingerprint, so the corrected route must be
        // fingerprinted AFTER the promotion — otherwise the next pass (whose
        // liveness now reports the ACP worker) resolves Structured, sees a
        // different fingerprint, and pointlessly tears down and respawns the
        // stream it just opened.
        let cfg = settings(vec![entry("s1", "t", "p", "auto")], "https://h");

        let mut pre = HashMap::new();
        pre.insert(
            "s1".to_string(),
            liveness("s1", "claude", "Idle", "absent", true),
        );
        let mut this_pass = resolve_routes(&cfg, &pre);
        assert!(this_pass[0].promote_structured_after_acp());

        let mut post = HashMap::new();
        post.insert(
            "s1".to_string(),
            liveness("s1", "claude", "Idle", "running", false),
        );
        let next_pass = resolve_routes(&cfg, &post);
        assert_eq!(next_pass[0].mode, Mode::Structured);
        assert_eq!(
            this_pass[0].fingerprint(&cfg.aoe_base, &cfg.aoe_token),
            next_pass[0].fingerprint(&cfg.aoe_base, &cfg.aoe_token)
        );
    }

    #[test]
    fn explicit_mode_overrides_inference() {
        let entries = vec![entry("s1", "t1", "p", "structured")];
        // Live status looks terminal, but explicit "structured" wins.
        let live = live_map(&[("s1", "claude", "Running")]);
        let routes = resolve_routes(&settings(entries, "https://h/agent-mcp"), &live);
        assert_eq!(routes[0].mode, Mode::Structured);
    }

    #[test]
    fn rows_missing_id_token_or_project_are_dropped() {
        let entries = vec![
            entry("", "t", "p", ""),  // no id
            entry("s2", "", "p", ""), // no token
            entry("s3", "t", "", ""), // no project
        ];
        let routes = resolve_routes(&settings(entries, "https://h/agent-mcp"), &HashMap::new());
        assert!(routes.is_empty());
    }

    #[test]
    fn blank_base_resolves_nothing() {
        let entries = vec![entry("s1", "t1", "p", "auto")];
        let routes = resolve_routes(&settings(entries, ""), &HashMap::new());
        assert!(routes.is_empty());
    }

    #[test]
    fn configured_but_not_live_is_marked_not_live() {
        let entries = vec![entry("ghost", "t", "p", "auto")];
        let routes = resolve_routes(&settings(entries, "https://h/agent-mcp"), &HashMap::new());
        assert_eq!(routes.len(), 1);
        assert!(!routes[0].live);
        // No live signal -> auto defaults to terminal.
        assert_eq!(routes[0].mode, Mode::Terminal);
    }

    #[test]
    fn expose_mcp_off_yields_no_mcp_url_but_keeps_delivery() {
        let mut e = entry("s1", "t1", "p", "auto");
        e.expose_mcp = false;
        let live = live_map(&[("s1", "claude", "Running")]);
        let routes = resolve_routes(&settings(vec![e], "https://h/agent-mcp"), &live);
        assert!(routes[0].mcp_url.is_none());
        // Delivery endpoint is still derived (delivery is independent of MCP).
        assert_eq!(routes[0].endpoint, "https://h/agent-mcp/api/p");
    }

    #[test]
    fn parse_liveness_handles_wrapped_and_bare_shapes() {
        // Wrapped {"sessions":[...]} shape.
        let wrapped = json!({
            "sessions": [
                {
                    "id": "s1", "title": "t", "status": "Running", "tool": "claude",
                    "acp_worker_state": "running", "has_terminal": true, "dormant": false
                },
                { "id": "s2", "status": "Idle", "acp_worker_state": "absent",
                  "has_terminal": false, "dormant": false },
                { "status": "no id" }
            ]
        });
        let m = parse_liveness(&wrapped);
        assert_eq!(m.len(), 2); // the id-less row is dropped.
        assert!(m["s1"].has_terminal);
        assert_eq!(m["s1"].acp_worker_state, "running");
        assert_eq!(m["s2"].acp_worker_state, "absent");
        assert!(!m["s2"].has_terminal);

        // Bare [...] array shape.
        let bare = json!([{ "id": "b1", "status": "Running", "dormant": true }]);
        let m = parse_liveness(&bare);
        assert_eq!(m.len(), 1);
        assert!(m["b1"].dormant);

        // Non-array / unexpected shape yields empty.
        assert!(parse_liveness(&Value::Null).is_empty());
    }

    #[test]
    fn ensure_acp_defaults_true_when_key_absent() {
        // Mirrors how load_settings resolves the setting: a missing key comes
        // back from config.get as Null, which must fall back to true (the
        // "switch covered sessions to ACP so per-session MCP applies" default).
        assert!(Value::Null.as_bool().unwrap_or(true));
        // An explicit false is honoured.
        assert!(!json!(false).as_bool().unwrap_or(true));
    }

    #[test]
    fn wants_acp_gates_on_expose_mcp_and_setting() {
        let live = live_map(&[("s1", "claude", "Running")]);
        // expose_mcp on -> mcp_url set -> candidate when ensure_acp on.
        let covered = resolve_routes(
            &settings(vec![entry("s1", "t", "p", "auto")], "https://h"),
            &live,
        );
        assert!(covered[0].wants_acp(true));
        // ...but never when the global setting is off.
        assert!(!covered[0].wants_acp(false));

        // expose_mcp off -> no mcp_url -> never a candidate, even with ensure_acp on.
        let mut e = entry("s1", "t", "p", "auto");
        e.expose_mcp = false;
        let delivery_only = resolve_routes(&settings(vec![e], "https://h"), &live);
        assert!(delivery_only[0].mcp_url.is_none());
        assert!(!delivery_only[0].wants_acp(true));
    }

    #[test]
    fn expose_mcp_defaults_true_when_row_omits_it() {
        let v = json!([{ "session_id": "s1", "token": "t1", "project": "p" }]);
        let entries = parse_session_entries(&v);
        assert_eq!(entries.len(), 1);
        assert!(entries[0].expose_mcp);
        assert_eq!(entries[0].project, "p");
    }
}
