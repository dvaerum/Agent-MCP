//! Live bridge state, and the AoE settings page it renders into.
//!
//! ## Why a `settings-page`
//!
//! A runtime worker has exactly two channels to the operator, and the host owns
//! both (verified against the AoE host source):
//!
//! - **`ui.state.set`** into a slot the manifest declares (`src/plugin/host_api.rs`,
//!   `ui_state_set` → `require_declared_slot`). Gated on the `runtime.worker`
//!   capability, which the bridge already holds. Typed, host-validated, and it
//!   *persists* until the worker exits — the only inspectable surface a plugin has.
//! - **`ui.notify`** — a transient toast, gated on `notifications`. It lives in a
//!   200-entry in-memory ring that dies with the daemon, so it is an alert, not a
//!   record.
//!
//! Everything else is a dead end, and each was considered and rejected:
//!
//! - **The `status` command returning live state.** `POST /api/plugins/commands/
//!   {fqid}/invoke` answers `202 {"ok": true}` and forwards the command to the
//!   worker as a JSON-RPC *notification* with no `id`
//!   (`src/server/api/plugins.rs` → `PluginHost::notify_worker`). The worker is
//!   protocol-forbidden from replying, so a command can never return anything.
//!   Instead the command triggers an immediate repaint of this page plus a
//!   summary toast — the idiomatic host pattern (the same one pane actions use).
//! - **Worker stderr.** Lands in `<app_dir>/plugin-workers/<uuid>.log`, a file
//!   with an unguessable per-spawn name that nothing reads. See [`crate::observe`].
//! - **`events.publish`.** A plugin-to-plugin bus persisted to `plugin_events.db`
//!   with no HTTP endpoint and no UI reader — invisible to an operator.
//! - **The `[[status]]` manifest contribution.** Static metadata with zero
//!   consumers in the host.
//! - **Folding inject health into agent-mcp's `/delivery/status`.** That channel
//!   reports the *session's* transport-status over a closed four-value vocabulary
//!   (`working|idle|dormant|dead`, `delivery_transport.VALID_STATUSES`), and
//!   agent-mcp's delivery policy stops nudging a `dead` worker. Reporting a
//!   failing inject as `dead` would silence the very deliveries the operator is
//!   trying to repair, and any new value needs a cross-repo protocol change. The
//!   channel stays what it is; inject health is a *bridge* concern and belongs
//!   on the bridge's own page.
//!
//! `settings-page` is global (not per-session), so one entry covers every
//! session the bridge holds — which is exactly the cross-session question an
//! operator asks ("is anything broken?"). A per-session `pane` would need N
//! entries and would only be visible while sitting in that one session's view.
//!
//! ## What is never rendered here
//!
//! Tokens and message bodies. The page carries session ids, the agent-mcp
//! project name, counters, timestamps, HTTP status codes, and the delivery
//! frame's `reason` (a fixed enum: `unread_messages` / `unfinished_tasks` /
//! `unassigned_tasks`). It carries **no** message subjects, no senders, no task
//! titles, and no bearer — see [`SessionObs::note_frame`], which takes counts
//! and a reason rather than the frame itself, so there is no path for body
//! content to reach the page in the first place.

use std::collections::BTreeMap;

use serde_json::{json, Value};

use crate::observe::{format_age, Level};

/// The `[[ui]]` slot the manifest declares. Global (not per-session), so one
/// entry covers every session the bridge holds.
pub const PAGE_SLOT: &str = "settings-page";

/// The `[[ui]]` contribution id the manifest declares for the page.
pub const PAGE_ID: &str = "main";

/// Cap on how many sessions get a detail section. The host caps a
/// `settings-page` payload at 64 KiB and the `sessions` setting allows 200 rows,
/// so a full render could be refused — and a refused push means *no* page at
/// all, the worst possible failure for an observability surface. Failing
/// sessions sort first ([`Snapshot::ordered`]), so a truncated page still shows
/// the problems.
const MAX_DETAIL_SESSIONS: usize = 40;

/// Cap on a captured error detail. Long enough for AoE's `{"error": code,
/// "message": …}` body (the thing that would have named `acp_mode_unsupported`
/// on sight), short enough to keep the page inside its byte budget.
const MAX_DETAIL_CHARS: usize = 200;

/// How long a session must keep failing before a *second* toast is allowed.
/// A flapping endpoint would otherwise fire one per frame.
pub const NOTIFY_COOLDOWN_SECS: u64 = 900;

/// Where a session's delivery SSE stream stands.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StreamState {
    /// Spawned, first connect not yet completed.
    Connecting,
    /// Holding an open stream.
    Connected,
    /// Dropped; sleeping before retry `attempt`.
    Reconnecting { attempt: u32 },
    /// No task (session not live, or coverage removed).
    Stopped,
}

impl StreamState {
    fn label(&self) -> String {
        match self {
            StreamState::Connecting => "connecting".to_string(),
            StreamState::Connected => "connected".to_string(),
            StreamState::Reconnecting { attempt } => format!("reconnecting (attempt {attempt})"),
            StreamState::Stopped => "stopped".to_string(),
        }
    }

    fn tone(&self) -> &'static str {
        match self {
            StreamState::Connected => "success",
            StreamState::Connecting => "info",
            StreamState::Reconnecting { .. } => "warn",
            StreamState::Stopped => "neutral",
        }
    }
}

/// The outcome of one injection attempt.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InjectOutcome {
    pub ok: bool,
    /// The AoE HTTP status, or `None` when the request never got a response.
    pub http: Option<u16>,
    /// A short, redacted description — the AoE error body on a non-2xx, or the
    /// transport error. Empty on success.
    pub detail: String,
    pub at: u64,
}

impl InjectOutcome {
    fn label(&self) -> String {
        match (self.ok, self.http) {
            (true, Some(code)) => format!("ok (HTTP {code})"),
            (true, None) => "ok".to_string(),
            (false, Some(code)) => {
                if self.detail.is_empty() {
                    format!("HTTP {code}")
                } else {
                    format!("HTTP {code}: {}", self.detail)
                }
            }
            (false, None) => {
                if self.detail.is_empty() {
                    "failed".to_string()
                } else {
                    format!("failed: {}", self.detail)
                }
            }
        }
    }
}

/// Everything observable about one covered session.
#[derive(Debug, Clone)]
pub struct SessionObs {
    pub session_id: String,
    pub project: String,
    /// Present in AoE's session list this reconcile.
    pub live: bool,
    /// The `transport-status` last reported to agent-mcp.
    pub transport_status: String,
    /// The resolved injection route (`terminal` / `structured`).
    pub mode: String,
    /// Whether the row asked for agent-mcp's tools to be injected.
    pub expose_mcp: bool,
    /// Whether `session.mcp.set` has succeeded for the current url+token.
    pub mcp_asserted: bool,
    pub stream: StreamState,
    pub frames: u64,
    pub last_frame_at: Option<u64>,
    /// The last frame's `reason` — a fixed enum, never subject or body text.
    pub last_frame_reason: String,
    pub injects_ok: u64,
    pub injects_failed: u64,
    /// Failures since the last success. Drives the toast gate.
    pub consecutive_failures: u32,
    pub last_inject: Option<InjectOutcome>,
    /// Last non-inject problem (status POST, mcp set, acp enable), redacted.
    pub last_error: Option<(String, u64)>,
    /// When this session was last toasted about, for cooldown.
    pub last_notified_at: Option<u64>,
}

impl SessionObs {
    pub fn new(session_id: String, project: String) -> Self {
        Self {
            session_id,
            project,
            live: false,
            transport_status: String::new(),
            mode: String::new(),
            expose_mcp: false,
            mcp_asserted: false,
            stream: StreamState::Stopped,
            frames: 0,
            last_frame_at: None,
            last_frame_reason: String::new(),
            injects_ok: 0,
            injects_failed: 0,
            consecutive_failures: 0,
            last_inject: None,
            last_error: None,
            last_notified_at: None,
        }
    }

    /// Record that a delivery frame arrived. Takes the `reason` only — the frame
    /// itself never crosses into observable state, so no subject or body can.
    pub fn note_frame(&mut self, reason: &str, at: u64) {
        self.frames += 1;
        self.last_frame_at = Some(at);
        self.last_frame_reason = reason.to_string();
    }

    /// Record an injection attempt. Returns the toast decision — see
    /// [`decide_notice`].
    pub fn note_inject(&mut self, outcome: InjectOutcome) -> Notice {
        let at = outcome.at;
        if outcome.ok {
            self.injects_ok += 1;
            let was_failing = self.consecutive_failures > 0;
            self.consecutive_failures = 0;
            self.last_inject = Some(outcome);
            // Only sessions the operator was actually warned about get a
            // recovery toast; a silent blip stays silent both ways.
            if was_failing && self.last_notified_at.is_some() {
                self.last_notified_at = None;
                return Notice::Recovered;
            }
            self.last_notified_at = None;
            return Notice::None;
        }
        self.injects_failed += 1;
        self.consecutive_failures = self.consecutive_failures.saturating_add(1);
        self.last_inject = Some(outcome);
        let notice = decide_notice(self.consecutive_failures, self.last_notified_at, at);
        if notice != Notice::None {
            self.last_notified_at = Some(at);
        }
        notice
    }

    pub fn note_error(&mut self, what: &str, at: u64) {
        self.last_error = Some((truncate(what, MAX_DETAIL_CHARS), at));
    }

    /// Whether this session currently counts as broken for the page's verdict.
    pub fn failing(&self) -> bool {
        self.consecutive_failures > 0
    }
}

/// What, if anything, to tell the operator about an inject result.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Notice {
    None,
    /// First failure, or the cooldown lapsed while still failing.
    Failing,
    /// Injection started working again after the operator was warned.
    Recovered,
}

/// Rate-limit gate for failure toasts.
///
/// Notify on the **first** failure of a run (so a real outage surfaces within
/// one frame), then at most once per [`NOTIFY_COOLDOWN_SECS`] while it keeps
/// failing. A session flapping on every frame therefore produces one toast, not
/// one per frame — the host's notification ring is only 200 entries deep and
/// would otherwise evict everything else in the UI.
///
/// Pure over its inputs so the policy is testable without a clock.
pub fn decide_notice(consecutive_failures: u32, last_notified_at: Option<u64>, now: u64) -> Notice {
    if consecutive_failures == 0 {
        return Notice::None;
    }
    match last_notified_at {
        None => Notice::Failing,
        Some(prev) => {
            if now.saturating_sub(prev) >= NOTIFY_COOLDOWN_SECS {
                Notice::Failing
            } else {
                Notice::None
            }
        }
    }
}

/// The whole bridge's observable state.
#[derive(Debug, Clone)]
pub struct Snapshot {
    pub enabled: bool,
    /// Whether `agent_mcp_base` is set — without it the bridge covers nothing,
    /// which is otherwise indistinguishable from "no sessions configured".
    pub configured: bool,
    pub started_at: u64,
    pub last_reconcile_at: Option<u64>,
    /// Rows in the `sessions` setting, including ones dropped for a missing
    /// field — so "I added a row and nothing happened" is diagnosable.
    pub configured_rows: usize,
    pub sessions: BTreeMap<String, SessionObs>,
}

impl Snapshot {
    pub fn new(started_at: u64) -> Self {
        Self {
            enabled: false,
            configured: false,
            started_at,
            last_reconcile_at: None,
            configured_rows: 0,
            sessions: BTreeMap::new(),
        }
    }

    pub fn session_mut(&mut self, session_id: &str, project: &str) -> &mut SessionObs {
        self.sessions
            .entry(session_id.to_string())
            .or_insert_with(|| SessionObs::new(session_id.to_string(), project.to_string()))
    }

    pub fn failing_count(&self) -> usize {
        self.sessions.values().filter(|s| s.failing()).count()
    }

    pub fn live_count(&self) -> usize {
        self.sessions.values().filter(|s| s.live).count()
    }

    /// Sessions ordered for display: failing first, then not-live, then the rest
    /// alphabetically. Truncation cuts the tail, so the problems always survive.
    pub fn ordered(&self) -> Vec<&SessionObs> {
        let mut v: Vec<&SessionObs> = self.sessions.values().collect();
        v.sort_by_key(|s| {
            let rank = if s.failing() {
                0
            } else if !s.live {
                1
            } else {
                2
            };
            (rank, s.session_id.clone())
        });
        v
    }
}

/// The page's overall verdict.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Health {
    Disabled,
    Unconfigured,
    Idle,
    Healthy,
    Degraded,
}

impl Health {
    fn tone(self) -> &'static str {
        match self {
            Health::Degraded => "danger",
            Health::Healthy => "success",
            Health::Disabled | Health::Unconfigured => "warn",
            Health::Idle => "neutral",
        }
    }
}

/// Classify the bridge's overall health. Ordered most-actionable first: a
/// disabled or unconfigured bridge is not "healthy", it is doing nothing, and an
/// operator staring at an empty page needs to be told which.
pub fn classify_health(snap: &Snapshot) -> Health {
    if !snap.enabled {
        return Health::Disabled;
    }
    if !snap.configured {
        return Health::Unconfigured;
    }
    if snap.failing_count() > 0 {
        return Health::Degraded;
    }
    // No LIVE session is idle, not healthy — "Delivering to 0 sessions" must
    // never read green. Covers both "nothing configured" and "every covered
    // session has stopped".
    if snap.live_count() == 0 {
        return Health::Idle;
    }
    Health::Healthy
}

fn verdict(snap: &Snapshot, health: Health) -> (String, String) {
    match health {
        Health::Disabled => (
            "Delivery bridge is off".to_string(),
            "The 'Enable delivery bridge' setting is off: no streams are held and no status is reported.".to_string(),
        ),
        Health::Unconfigured => (
            "No agent-mcp base URL".to_string(),
            "Set 'agent-mcp base URL' in this plugin's settings. Until it is set the bridge covers nothing.".to_string(),
        ),
        Health::Idle => (
            "No sessions covered".to_string(),
            format!(
                "{} row(s) configured, none resolved to a live session. A row needs a session id, a token and a project, and the session must be running.",
                snap.configured_rows
            ),
        ),
        Health::Healthy => (
            format!("Delivering to {} session(s)", snap.live_count()),
            "Every covered session has a delivery stream and its last injection succeeded.".to_string(),
        ),
        Health::Degraded => {
            let failing = snap.failing_count();
            (
                format!("{failing} session(s) failing to inject"),
                "A nudge reached the bridge but AoE refused it. Open the session below for the HTTP status and message.".to_string(),
            )
        }
    }
}

/// Render the `ui.state.set` payload for the bridge's settings page.
///
/// Pure over `(snapshot, now, log_path, level)` so the whole surface is
/// testable without a host.
pub fn render_page(snap: &Snapshot, now: u64, log_path: Option<&str>, level: Level) -> Value {
    let health = classify_health(snap);
    let (title, detail) = verdict(snap, health);

    let mut blocks = vec![json!({
        "kind": "callout",
        "title": title,
        "detail": detail,
        "tone": health.tone(),
        "icon": "radio-tower",
    })];

    let ordered = snap.ordered();
    if !ordered.is_empty() {
        let shown = ordered.len().min(MAX_DETAIL_SESSIONS);
        let children: Vec<Value> = ordered
            .iter()
            .take(shown)
            .map(|s| session_section(s, now))
            .collect();
        blocks.push(json!({
            "kind": "section",
            "title": "Covered sessions",
            "value": format!("{} live / {} covered", snap.live_count(), snap.sessions.len()),
            "boxed": true,
            "children": children,
        }));
        if ordered.len() > shown {
            blocks.push(json!({
                "kind": "note",
                "tone": "neutral",
                "text": format!(
                    "… and {} more session(s) not shown (failing and offline sessions are listed first).",
                    ordered.len() - shown
                ),
            }));
        }
    }

    blocks.push(json!({ "kind": "divider" }));
    blocks.push(json!({
        "kind": "section",
        "title": "Diagnostics",
        "collapsible": true,
        "collapsed": true,
        "children": diagnostics_rows(snap, now, log_path, level),
    }));

    json!({
        "title": "agent-mcp Delivery",
        "icon": "radio-tower",
        "blocks": blocks,
    })
}

fn session_section(s: &SessionObs, now: u64) -> Value {
    let (value, value_tone) = if s.failing() {
        (
            format!("{} consecutive inject failure(s)", s.consecutive_failures),
            "danger",
        )
    } else if !s.live {
        ("not live".to_string(), "warn")
    } else {
        (s.transport_status.clone(), "success")
    };

    let mut rows = vec![
        row("project", &s.project, None),
        row("live", if s.live { "yes" } else { "no" }, None),
        row(
            "transport-status",
            if s.transport_status.is_empty() {
                "—"
            } else {
                &s.transport_status
            },
            None,
        ),
        row("route", if s.mode.is_empty() { "—" } else { &s.mode }, None),
        row("stream", &s.stream.label(), Some(s.stream.tone())),
        row("frames received", &frames_label(s, now), None),
        row("injects", &injects_label(s), None),
    ];

    if let Some(last) = s.last_inject.as_ref() {
        rows.push(row(
            "last inject",
            &format!(
                "{} · {}",
                last.label(),
                format_age(now.saturating_sub(last.at))
            ),
            Some(if last.ok { "success" } else { "danger" }),
        ));
    }
    if s.expose_mcp {
        rows.push(row(
            "agent-mcp tools",
            if s.mcp_asserted {
                "injected"
            } else {
                "pending"
            },
            Some(if s.mcp_asserted { "success" } else { "warn" }),
        ));
    }
    if let Some((err, at)) = s.last_error.as_ref() {
        rows.push(row(
            "last error",
            &format!("{err} · {}", format_age(now.saturating_sub(*at))),
            Some("warn"),
        ));
    }

    json!({
        "kind": "section",
        "title": s.session_id,
        "value": value,
        "value_tone": value_tone,
        "collapsible": true,
        // A failing session opens expanded: the operator should not have to
        // click to see why delivery broke.
        "collapsed": !s.failing(),
        "children": rows,
    })
}

fn frames_label(s: &SessionObs, now: u64) -> String {
    match s.last_frame_at {
        None => format!("{} (none yet)", s.frames),
        Some(at) => {
            let age = format_age(now.saturating_sub(at));
            if s.last_frame_reason.is_empty() {
                format!("{} · last {age}", s.frames)
            } else {
                format!("{} · last {age} ({})", s.frames, s.last_frame_reason)
            }
        }
    }
}

fn injects_label(s: &SessionObs) -> String {
    format!("{} ok / {} failed", s.injects_ok, s.injects_failed)
}

fn diagnostics_rows(snap: &Snapshot, now: u64, log_path: Option<&str>, level: Level) -> Vec<Value> {
    let mut rows = vec![
        row(
            "worker uptime",
            &format_uptime(now.saturating_sub(snap.started_at)),
            None,
        ),
        row(
            "last reconcile",
            &match snap.last_reconcile_at {
                Some(at) => format_age(now.saturating_sub(at)),
                None => "never".to_string(),
            },
            None,
        ),
        row("configured rows", &snap.configured_rows.to_string(), None),
        row("log level", level.as_str(), None),
    ];
    rows.push(match log_path {
        Some(p) => mono_row("log file", p),
        None => row(
            "log file",
            "disabled (set AOE_BRIDGE_LOG_FILE to enable)",
            Some("warn"),
        ),
    });
    rows
}

fn row(label: &str, value: &str, value_tone: Option<&str>) -> Value {
    let mut v = json!({ "kind": "row", "label": label, "value": value });
    if let Some(tone) = value_tone {
        v["value_tone"] = json!(tone);
    }
    v
}

fn mono_row(label: &str, value: &str) -> Value {
    json!({ "kind": "row", "label": label, "value": value, "mono": true })
}

fn format_uptime(secs: u64) -> String {
    match secs {
        0..=59 => format!("{secs}s"),
        60..=3599 => format!("{}m", secs / 60),
        3600..=86_399 => format!("{}h {}m", secs / 3600, (secs % 3600) / 60),
        _ => format!("{}d {}h", secs / 86_400, (secs % 86_400) / 3600),
    }
}

/// Clip a captured detail to `max` characters, on a char boundary, marking the
/// cut so a truncated AoE error is not mistaken for the whole message.
pub fn truncate(s: &str, max: usize) -> String {
    let s = s.trim();
    if s.chars().count() <= max {
        return s.to_string();
    }
    let kept: String = s.chars().take(max).collect();
    format!("{kept}…")
}

/// Clip an AoE error body for capture into [`InjectOutcome::detail`].
pub fn clip_detail(s: &str) -> String {
    truncate(s, MAX_DETAIL_CHARS)
}

/// The one-line summary the `status` command toasts, and the body of the
/// failure toast. Never carries a token or a message body.
pub fn summary_line(snap: &Snapshot) -> String {
    match classify_health(snap) {
        Health::Disabled => "Delivery bridge is disabled.".to_string(),
        Health::Unconfigured => "No agent-mcp base URL configured.".to_string(),
        Health::Idle => format!(
            "{} row(s) configured, no live covered session.",
            snap.configured_rows
        ),
        Health::Healthy => format!("{} session(s) live, all injecting.", snap.live_count()),
        Health::Degraded => format!(
            "{} of {} session(s) failing to inject.",
            snap.failing_count(),
            snap.sessions.len()
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn snap_with(sessions: Vec<SessionObs>) -> Snapshot {
        let mut s = Snapshot::new(1_000);
        s.enabled = true;
        s.configured = true;
        s.configured_rows = sessions.len();
        for obs in sessions {
            s.sessions.insert(obs.session_id.clone(), obs);
        }
        s
    }

    fn live_session(id: &str) -> SessionObs {
        let mut o = SessionObs::new(id.to_string(), "proj".to_string());
        o.live = true;
        o.transport_status = "idle".to_string();
        o.mode = "structured".to_string();
        o.stream = StreamState::Connected;
        o
    }

    fn failed(at: u64) -> InjectOutcome {
        InjectOutcome {
            ok: false,
            http: Some(400),
            detail: "acp_mode_unsupported".to_string(),
            at,
        }
    }

    fn succeeded(at: u64) -> InjectOutcome {
        InjectOutcome {
            ok: true,
            http: Some(200),
            detail: String::new(),
            at,
        }
    }

    // ---- health -----------------------------------------------------------

    #[test]
    fn health_reports_disabled_before_anything_else() {
        let mut s = snap_with(vec![live_session("a")]);
        s.enabled = false;
        assert_eq!(classify_health(&s), Health::Disabled);
    }

    #[test]
    fn health_distinguishes_unconfigured_from_idle() {
        let mut s = snap_with(vec![]);
        s.configured = false;
        assert_eq!(classify_health(&s), Health::Unconfigured);
        s.configured = true;
        assert_eq!(classify_health(&s), Health::Idle);
    }

    #[test]
    fn health_is_degraded_when_any_session_is_failing() {
        let mut failing = live_session("a");
        failing.note_inject(failed(10));
        let s = snap_with(vec![failing, live_session("b")]);
        assert_eq!(classify_health(&s), Health::Degraded);
        assert_eq!(s.failing_count(), 1);
    }

    #[test]
    fn health_is_healthy_when_every_session_last_injected_ok() {
        let mut ok = live_session("a");
        ok.note_inject(succeeded(10));
        assert_eq!(classify_health(&snap_with(vec![ok])), Health::Healthy);
    }

    #[test]
    fn covered_sessions_that_all_stopped_are_idle_not_healthy() {
        // "Delivering to 0 session(s)" must never render green: a covered
        // session that died is a state the operator has to notice.
        let mut stopped = live_session("a");
        stopped.live = false;
        let s = snap_with(vec![stopped]);
        assert_eq!(classify_health(&s), Health::Idle);
        let rendered = render_page(&s, 2_000, None, Level::Info).to_string();
        assert!(!rendered.contains("\"tone\":\"success\""), "{rendered}");
        assert!(
            rendered.contains("none resolved to a live session"),
            "{rendered}"
        );
    }

    #[test]
    fn a_stopped_session_still_appears_on_the_page() {
        let mut stopped = live_session("ghost");
        stopped.live = false;
        stopped.transport_status = "dead".to_string();
        let rendered = render_page(&snap_with(vec![stopped]), 2_000, None, Level::Info).to_string();
        assert!(rendered.contains("ghost"), "{rendered}");
        assert!(rendered.contains("not live"), "{rendered}");
    }

    // ---- notify gating ----------------------------------------------------

    #[test]
    fn first_inject_failure_notifies_immediately() {
        let mut s = live_session("a");
        assert_eq!(s.note_inject(failed(100)), Notice::Failing);
    }

    #[test]
    fn repeated_failures_inside_the_cooldown_stay_silent() {
        let mut s = live_session("a");
        assert_eq!(s.note_inject(failed(100)), Notice::Failing);
        for t in [101, 200, 500, 900] {
            assert_eq!(s.note_inject(failed(t)), Notice::None, "t={t}");
        }
        assert_eq!(s.injects_failed, 5);
        assert_eq!(s.consecutive_failures, 5);
    }

    #[test]
    fn a_still_failing_session_renotifies_once_the_cooldown_lapses() {
        let mut s = live_session("a");
        s.note_inject(failed(100));
        assert_eq!(
            s.note_inject(failed(100 + NOTIFY_COOLDOWN_SECS - 1)),
            Notice::None
        );
        assert_eq!(
            s.note_inject(failed(100 + NOTIFY_COOLDOWN_SECS)),
            Notice::Failing
        );
    }

    #[test]
    fn recovery_notifies_only_a_session_the_operator_was_warned_about() {
        let mut warned = live_session("a");
        warned.note_inject(failed(100));
        assert_eq!(warned.note_inject(succeeded(200)), Notice::Recovered);
        // ...and only once.
        assert_eq!(warned.note_inject(succeeded(300)), Notice::None);

        // A session that never failed never announces a recovery.
        let mut clean = live_session("b");
        assert_eq!(clean.note_inject(succeeded(100)), Notice::None);
    }

    #[test]
    fn decide_notice_is_pure_over_its_inputs() {
        assert_eq!(decide_notice(0, None, 10), Notice::None);
        assert_eq!(decide_notice(1, None, 10), Notice::Failing);
        assert_eq!(
            decide_notice(3, Some(10), 10 + NOTIFY_COOLDOWN_SECS),
            Notice::Failing
        );
        assert_eq!(decide_notice(3, Some(10), 11), Notice::None);
    }

    // ---- ordering / truncation -------------------------------------------

    #[test]
    fn failing_sessions_sort_ahead_of_offline_then_healthy() {
        let mut broken = live_session("z-broken");
        broken.note_inject(failed(10));
        let mut offline = live_session("a-offline");
        offline.live = false;
        let healthy = live_session("b-healthy");
        let s = snap_with(vec![healthy, offline, broken]);
        let ids: Vec<&str> = s.ordered().iter().map(|o| o.session_id.as_str()).collect();
        assert_eq!(ids, vec!["z-broken", "a-offline", "b-healthy"]);
    }

    #[test]
    fn page_truncates_past_the_detail_cap_and_says_so() {
        let sessions: Vec<SessionObs> = (0..MAX_DETAIL_SESSIONS + 5)
            .map(|i| live_session(&format!("sid-{i:03}")))
            .collect();
        let s = snap_with(sessions);
        let page = render_page(&s, 2_000, None, Level::Info);
        let blocks = page["blocks"].as_array().unwrap();
        let section = blocks
            .iter()
            .find(|b| b["kind"] == "section" && b["title"] == "Covered sessions")
            .unwrap();
        assert_eq!(
            section["children"].as_array().unwrap().len(),
            MAX_DETAIL_SESSIONS
        );
        let note = blocks.iter().find(|b| b["kind"] == "note").unwrap();
        assert!(note["text"].as_str().unwrap().contains("and 5 more"));
    }

    #[test]
    fn page_stays_within_the_host_64kib_payload_cap_at_max_rows() {
        // The `sessions` setting allows 200 rows; a payload the host refuses
        // would mean no page at all, so the cap must hold at the worst case.
        let sessions: Vec<SessionObs> = (0..200)
            .map(|i| {
                let mut o = live_session(&format!("session-{i:04}-with-a-long-identifier"));
                o.note_inject(failed(10));
                o.note_error("something went wrong talking to the host", 10);
                o
            })
            .collect();
        let s = snap_with(sessions);
        let page = render_page(
            &s,
            2_000,
            Some("/home/dennis/.local/state/aoe-bridge/worker.log"),
            Level::Debug,
        );
        assert!(
            page.to_string().len() < 64 * 1024,
            "payload was {} bytes",
            page.to_string().len()
        );
    }

    // ---- rendering --------------------------------------------------------

    #[test]
    fn page_surfaces_the_http_status_and_error_code_of_a_failed_inject() {
        let mut broken = live_session("sid-1");
        broken.note_inject(failed(1_990));
        let s = snap_with(vec![broken]);
        let rendered = render_page(&s, 2_000, None, Level::Info).to_string();
        // The exact signal that was invisible during the live outage.
        assert!(rendered.contains("HTTP 400"), "{rendered}");
        assert!(rendered.contains("acp_mode_unsupported"), "{rendered}");
        assert!(
            rendered.contains("1 session(s) failing to inject"),
            "{rendered}"
        );
    }

    #[test]
    fn page_shows_successful_injects_and_frame_counts() {
        let mut ok = live_session("sid-1");
        ok.note_frame("unread_messages", 1_950);
        ok.note_inject(succeeded(1_950));
        let s = snap_with(vec![ok]);
        let rendered = render_page(&s, 2_000, None, Level::Info).to_string();
        assert!(rendered.contains("1 ok / 0 failed"), "{rendered}");
        assert!(rendered.contains("unread_messages"), "{rendered}");
        assert!(rendered.contains("ok (HTTP 200)"), "{rendered}");
    }

    #[test]
    fn page_shows_stream_reconnect_state() {
        let mut s1 = live_session("sid-1");
        s1.stream = StreamState::Reconnecting { attempt: 4 };
        let s = snap_with(vec![s1]);
        let rendered = render_page(&s, 2_000, None, Level::Info).to_string();
        assert!(rendered.contains("reconnecting (attempt 4)"), "{rendered}");
    }

    #[test]
    fn page_publishes_the_log_path_so_the_file_is_discoverable() {
        let s = snap_with(vec![live_session("sid-1")]);
        let with_log = render_page(&s, 2_000, Some("/tmp/x/worker.log"), Level::Debug).to_string();
        assert!(with_log.contains("/tmp/x/worker.log"), "{with_log}");
        assert!(with_log.contains("debug"), "{with_log}");
        let without = render_page(&s, 2_000, None, Level::Info).to_string();
        assert!(without.contains("AOE_BRIDGE_LOG_FILE"), "{without}");
    }

    #[test]
    fn a_failing_session_renders_expanded_and_a_healthy_one_collapsed() {
        let mut broken = live_session("sid-bad");
        broken.note_inject(failed(10));
        let s = snap_with(vec![broken, live_session("sid-ok")]);
        let page = render_page(&s, 2_000, None, Level::Info);
        let children = page["blocks"]
            .as_array()
            .unwrap()
            .iter()
            .find(|b| b["title"] == "Covered sessions")
            .unwrap()["children"]
            .as_array()
            .unwrap()
            .clone();
        let bad = children.iter().find(|c| c["title"] == "sid-bad").unwrap();
        let good = children.iter().find(|c| c["title"] == "sid-ok").unwrap();
        assert_eq!(bad["collapsed"], json!(false));
        assert_eq!(good["collapsed"], json!(true));
    }

    #[test]
    fn page_never_carries_a_token_or_a_message_body() {
        // The state model has no field a subject/body/token could occupy; this
        // guards the renderer against a future field being wired in carelessly.
        let mut s1 = live_session("sid-1");
        s1.note_frame("unread_messages", 1_900);
        s1.note_inject(succeeded(1_900));
        let s = snap_with(vec![s1]);
        let rendered = render_page(&s, 2_000, None, Level::Info).to_string();
        for forbidden in ["Bearer", "token", "subject", "sender", "body"] {
            assert!(
                !rendered.to_lowercase().contains(&forbidden.to_lowercase()),
                "page leaked {forbidden:?}: {rendered}"
            );
        }
    }

    #[test]
    fn unconfigured_page_tells_the_operator_which_setting_is_missing() {
        let mut s = snap_with(vec![]);
        s.configured = false;
        let rendered = render_page(&s, 2_000, None, Level::Info).to_string();
        assert!(rendered.contains("agent-mcp base URL"), "{rendered}");
    }

    // ---- helpers ----------------------------------------------------------

    #[test]
    fn truncate_marks_the_cut_and_respects_char_boundaries() {
        assert_eq!(truncate("short", 10), "short");
        assert_eq!(truncate("abcdef", 3), "abc…");
        // Multi-byte input must not panic or split a char.
        assert_eq!(truncate("ééééé", 2), "éé…");
    }

    // ---- manifest ⇄ deploy contract ---------------------------------------

    /// The manifest as the nix deploy installs it.
    ///
    /// `users/dennis/aoe/bridge.nix` keeps everything BEFORE the first
    /// `\n[runtime]` and appends its own pre-built runtime, so anything declared
    /// after `[runtime]` in the crate manifest is silently dropped on the
    /// deployed host — the plugin would then be refused `ui.state.set` for an
    /// undeclared slot and the status page would never appear.
    fn deployed_manifest() -> String {
        let src = include_str!("../aoe-plugin.toml");
        src.split("\n[runtime]").next().unwrap().to_string()
    }

    #[test]
    fn manifest_declares_the_page_slot_before_the_runtime_section() {
        let deployed = deployed_manifest();
        assert!(deployed.contains("[[ui]]"), "no [[ui]] before [runtime]");
        assert!(
            deployed.contains(&format!("slot = \"{PAGE_SLOT}\"")),
            "the declared slot must match the one the worker pushes"
        );
        assert!(
            deployed.contains(&format!("id = \"{PAGE_ID}\"")),
            "the declared contribution id must match the one the worker pushes"
        );
    }

    #[test]
    fn manifest_capabilities_stay_within_the_nix_deploy_grant() {
        // `grant_covers`: the host refuses the plugin unless the deploy's
        // granted set is a SUPERSET of the manifest's. `ui.state.set` is gated
        // on `runtime.worker` plus the [[ui]] declaration — NOT on a capability
        // of its own — so the observability surface needs no new grant. If this
        // fails, users/dennis/aoe/bridge.nix must be updated in lockstep.
        const GRANTED: &[&str] = &[
            "runtime.worker",
            "session.read",
            "session.mcp",
            "net",
            "notifications",
        ];
        let line = deployed_manifest()
            .lines()
            .find(|l| l.starts_with("capabilities = "))
            .expect("manifest declares capabilities")
            .to_string();
        for cap in line
            .trim_start_matches("capabilities = [")
            .trim_end_matches(']')
            .split(',')
        {
            let cap = cap.trim().trim_matches('"');
            assert!(
                GRANTED.contains(&cap),
                "manifest declares {cap:?}, which the nix deploy does not grant"
            );
        }
    }

    #[test]
    fn summary_line_matches_the_health_verdict() {
        let mut broken = live_session("a");
        broken.note_inject(failed(10));
        assert_eq!(
            summary_line(&snap_with(vec![broken])),
            "1 of 1 session(s) failing to inject."
        );
        let mut ok = live_session("a");
        ok.note_inject(succeeded(10));
        assert_eq!(
            summary_line(&snap_with(vec![ok])),
            "1 session(s) live, all injecting."
        );
    }
}
