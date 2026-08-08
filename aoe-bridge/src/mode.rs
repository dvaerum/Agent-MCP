//! Injection mode + the pure classification helpers.
//!
//! AoE runs a session either as a **terminal** (tmux) pane, injected via
//! `/api/sessions/{id}/send`, or as a **structured** (ACP / CityHall / composer)
//! view, injected via `/api/sessions/{id}/acp/prompt`. The bridge must pick the
//! right route per session.

/// How the bridge injects text into a session.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Terminal,
    Structured,
}

impl Mode {
    pub fn as_str(self) -> &'static str {
        match self {
            Mode::Terminal => "terminal",
            Mode::Structured => "structured",
        }
    }
}

/// Parse AoE's authoritative `view` field (`GET /api/sessions[].view`) into a
/// [`Mode`], or `None` when the payload does not state one.
///
/// This is THE routing signal: AoE's terminal `/api/sessions/{id}/send` refuses
/// a session whose view is structured (`EnsureReadyError::StructuredView` →
/// `400 acp_mode_unsupported`), and that check reads exactly this value. Every
/// other signal the bridge has (`acp_worker_state`, `has_terminal`, the
/// tool/status text) is a proxy that can disagree — a structured session whose
/// worker has not spawned yet reports `acp_worker_state = "absent"`.
///
/// AoE serialises the field only when it is `structured`
/// (`#[serde(skip_serializing_if = "View::is_terminal")]`), so an absent field
/// on a current AoE means terminal. It is still mapped to `None` rather than
/// `Terminal` here, because an AoE too old to have the field would be
/// indistinguishable — the caller falls back to the proxies, which is right in
/// both cases.
pub fn view_mode(view: &str) -> Option<Mode> {
    match view.trim().to_ascii_lowercase().as_str() {
        "structured" => Some(Mode::Structured),
        "terminal" => Some(Mode::Terminal),
        _ => None,
    }
}

/// What to do about an inject AoE refused.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MismatchAction {
    /// AoE named a mode mismatch on an `auto` row: retry once on `Mode`, and
    /// remember it for this session's later frames.
    Retry(Mode),
    /// The same mismatch on a row whose mode the operator PINNED. Their pin is
    /// wrong, but silently overriding it would hide the misconfiguration — say
    /// so loudly instead and let the nudge fail.
    TellOperator(Mode),
    /// Not a mode mismatch (a real outage, a transient, a vanished session).
    Nothing,
}

/// Classify a refused inject: did AoE tell us we used the wrong route?
///
/// Only these two responses are a mode statement, and both are exact (verified
/// against AoE's handlers):
/// - terminal `/send` on a structured session → `400 {"error":
///   "acp_mode_unsupported"}` (`SendKeysError::StructuredView`);
/// - `/acp/prompt` on a session with no structured view → `404 "session has no
///   running structured view"` (`SupervisorError::UnknownSession`).
///
/// Everything else — `503 worker_not_ready` (a structured worker still
/// starting), `404 session not found` (the session is gone), `409
/// session_transient`, any 5xx — is NOT a mismatch and must never flip the
/// route: retrying those on the other transport would inject into the wrong
/// surface or mask a real outage. Status AND body must both match, so a stray
/// substring cannot promote an unrelated failure into a route change.
pub fn on_refusal(attempted: Mode, auto_mode: bool, http: u16, body: &str) -> MismatchAction {
    let correct = match attempted {
        Mode::Terminal if http == 400 && body.contains("acp_mode_unsupported") => Mode::Structured,
        Mode::Structured if http == 404 && body.contains("no running structured view") => {
            Mode::Terminal
        }
        _ => return MismatchAction::Nothing,
    };
    if auto_mode {
        MismatchAction::Retry(correct)
    } else {
        MismatchAction::TellOperator(correct)
    }
}

/// Normalise a free-form mode signal to a [`Mode`]. Anything mentioning a
/// structured surface (`structured`, `acp`, `composer`, `cityhall`) is
/// structured; everything else defaults to terminal. Case-insensitive.
pub fn normalize_mode(signal: &str) -> Mode {
    let s = signal.to_lowercase();
    if s.contains("structured")
        || s.contains("acp")
        || s.contains("composer")
        || s.contains("cityhall")
    {
        Mode::Structured
    } else {
        Mode::Terminal
    }
}

/// Map an AoE session-status signal to a delivery `transport-status`
/// (`working` / `idle` / `dormant` / `dead`), the value posted to
/// `/delivery/status`.
///
/// `sessions.list` exposes the status as a debug-formatted enum, so this is a
/// best-effort keyword match rather than an exact enum mapping. Checked
/// dead → dormant → working → idle so that e.g. a "stopped" or "snoozed"
/// status is not misread as "working" by a stray substring.
///
/// Retained as a keyword-only fallback for the plugin `sessions.list` signal;
/// the reconcile loop now uses [`classify_transport_status`] against the richer
/// web-REST liveness instead.
#[allow(dead_code)]
pub fn map_transport_status(signal: &str) -> &'static str {
    let s = signal.to_lowercase();
    let has = |needles: &[&str]| needles.iter().any(|n| s.contains(n));
    // Order matters: a "stopped"/"snoozed" status must not be caught by the
    // "working" substrings, so dead and dormant are tested first.
    if has(&["dead", "exit", "fail", "stopped", "gone"]) {
        "dead"
    } else if has(&["dorm", "snooz", "sleep", "sunk"]) {
        "dormant"
    } else if has(&["run", "active", "busy", "working", "thinking", "prompt"]) {
        "working"
    } else {
        "idle"
    }
}

/// Classify a live session's delivery `transport-status` from AoE's web-REST
/// liveness (`GET /api/sessions`), which — unlike the plugin `sessions.list`
/// keyword signal — carries the actual worker liveness. This is the richer,
/// authoritative classifier; [`map_transport_status`] remains a keyword-only
/// fallback for the (id/title/tool/status)-only plugin RPC.
///
/// Decision order (checked dead → dormant → working → idle):
/// - **dead**: `status` contains dead/exit/fail/gone. (A session ABSENT from
///   the REST list — configured but not live — is reported `dead` by the
///   caller, which never reaches this function.)
/// - **dormant**: `dormant == true`, OR no running worker
///   (`acp_worker_state == "absent"` AND `has_terminal == false`). This is the
///   key case: a STOPPED-but-present session, which the plugin RPC misreports
///   as `idle`.
/// - **working**: a worker is running (we passed the dormant check) AND
///   `status` looks active (run/active/busy/working/thinking/prompt).
/// - **idle**: otherwise.
pub fn classify_transport_status(
    status: &str,
    acp_worker_state: &str,
    has_terminal: bool,
    dormant: bool,
) -> &'static str {
    let s = status.to_lowercase();
    let has = |needles: &[&str]| needles.iter().any(|n| s.contains(n));
    if has(&["dead", "exit", "fail", "gone"]) {
        return "dead";
    }
    // No running worker of either kind ⇒ dormant (the stopped-but-present case).
    let no_worker = acp_worker_state.eq_ignore_ascii_case("absent") && !has_terminal;
    if dormant || no_worker {
        return "dormant";
    }
    // A worker is running; report working only if the status looks active.
    if has(&["run", "active", "busy", "working", "thinking", "prompt"]) {
        "working"
    } else {
        "idle"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn view_field_is_authoritative_and_absence_means_terminal() {
        // AoE serialises `view` only when it is `structured`
        // (`skip_serializing_if = "View::is_terminal"`), so an absent field is a
        // positive statement that the session renders as a terminal — not a
        // missing signal.
        assert_eq!(view_mode("structured"), Some(Mode::Structured));
        assert_eq!(view_mode("terminal"), Some(Mode::Terminal));
        assert_eq!(view_mode(""), None);
        assert_eq!(view_mode("Structured"), Some(Mode::Structured));
        // An unknown future value is not guessed at; the caller falls back.
        assert_eq!(view_mode("holodeck"), None);
    }

    #[test]
    fn refused_terminal_inject_retries_once_as_structured() {
        // AoE's terminal /send answers a structured-view session with exactly
        // this, which is AoE stating the session's view — stronger than any
        // proxy we infer.
        assert_eq!(
            on_refusal(
                Mode::Terminal,
                true,
                400,
                r#"{"error":"acp_mode_unsupported"}"#
            ),
            MismatchAction::Retry(Mode::Structured)
        );
    }

    #[test]
    fn refused_structured_inject_retries_once_as_terminal() {
        // The mirror case: /acp/prompt on a session with no structured view
        // answers 404 "session has no running structured view"
        // (SupervisorError::UnknownSession).
        assert_eq!(
            on_refusal(
                Mode::Structured,
                true,
                404,
                "session has no running structured view"
            ),
            MismatchAction::Retry(Mode::Terminal)
        );
    }

    #[test]
    fn a_pinned_row_is_told_about_the_mismatch_never_silently_corrected() {
        // An explicit `mode = "terminal"` that AoE refuses is an operator
        // misconfiguration. Correcting it silently would hide the mistake; the
        // caller is told to say so loudly instead.
        assert_eq!(
            on_refusal(
                Mode::Terminal,
                false,
                400,
                r#"{"error":"acp_mode_unsupported"}"#
            ),
            MismatchAction::TellOperator(Mode::Structured)
        );
        assert_eq!(
            on_refusal(
                Mode::Structured,
                false,
                404,
                "session has no running structured view"
            ),
            MismatchAction::TellOperator(Mode::Terminal)
        );
    }

    #[test]
    fn unrelated_failures_never_trigger_a_mode_retry() {
        for (mode, http, body) in [
            // A genuinely-structured session whose worker is still starting.
            (Mode::Structured, 503, "worker_not_ready"),
            (Mode::Structured, 503, "worker_capacity_full (4/4)"),
            // The session is gone, not mis-routed.
            (Mode::Structured, 404, "session not found"),
            (Mode::Terminal, 404, "session not found"),
            // A different 400, and a 409 mid-lifecycle retry.
            (Mode::Terminal, 400, r#"{"error":"empty_message"}"#),
            (Mode::Terminal, 409, r#"{"error":"session_transient"}"#),
            (Mode::Terminal, 500, "tmux exploded"),
            // Right code, wrong status: do not guess.
            (Mode::Terminal, 500, r#"{"error":"acp_mode_unsupported"}"#),
        ] {
            assert_eq!(
                on_refusal(mode, true, http, body),
                MismatchAction::Nothing,
                "{mode:?} {http} {body}"
            );
        }
    }

    #[test]
    fn structured_signals_map_to_structured() {
        for s in [
            "Structured",
            "acp",
            "claude ACP running",
            "composer-action",
            "CityHall",
        ] {
            assert_eq!(normalize_mode(s), Mode::Structured, "{s}");
        }
    }

    #[test]
    fn everything_else_is_terminal() {
        for s in ["terminal", "tmux", "claude Running", ""] {
            assert_eq!(normalize_mode(s), Mode::Terminal, "{s}");
        }
    }

    #[test]
    fn transport_status_keywords() {
        assert_eq!(map_transport_status("Running"), "working");
        assert_eq!(map_transport_status("claude Active"), "working");
        assert_eq!(map_transport_status("Snoozed"), "dormant");
        assert_eq!(map_transport_status("Sunk"), "dormant");
        assert_eq!(map_transport_status("Exited"), "dead");
        assert_eq!(map_transport_status("Stopped"), "dead");
        assert_eq!(map_transport_status("Idle"), "idle");
        assert_eq!(map_transport_status("whatever"), "idle");
    }

    #[test]
    fn stopped_session_with_no_worker_is_dormant() {
        // The key new case: present in the REST list, status "Idle", but no ACP
        // worker and no terminal ⇒ no running worker ⇒ dormant (not idle).
        assert_eq!(
            classify_transport_status("Idle", "absent", false, false),
            "dormant"
        );
        // Case-insensitive on the worker state.
        assert_eq!(
            classify_transport_status("Idle", "Absent", false, false),
            "dormant"
        );
    }

    #[test]
    fn explicit_dormant_flag_wins() {
        // dormant==true ⇒ dormant even with a worker attached and an active-ish
        // status.
        assert_eq!(
            classify_transport_status("Running", "running", true, true),
            "dormant"
        );
    }

    #[test]
    fn running_worker_with_active_status_is_working() {
        // ACP worker running.
        assert_eq!(
            classify_transport_status("Running", "running", false, false),
            "working"
        );
        // Terminal worker present, absent ACP, but has_terminal keeps a worker.
        assert_eq!(
            classify_transport_status("thinking", "absent", true, false),
            "working"
        );
    }

    #[test]
    fn running_worker_with_quiet_status_is_idle() {
        // Worker present (terminal) but the status is not active ⇒ idle.
        assert_eq!(
            classify_transport_status("Idle", "absent", true, false),
            "idle"
        );
        // ACP worker present, quiet status.
        assert_eq!(
            classify_transport_status("waiting", "running", false, false),
            "idle"
        );
    }

    #[test]
    fn dead_status_keywords_win_over_worker_state() {
        // Even with a running worker, a dead-ish status is dead.
        assert_eq!(
            classify_transport_status("exited", "running", true, false),
            "dead"
        );
        assert_eq!(
            classify_transport_status("failed", "absent", false, false),
            "dead"
        );
    }
}
