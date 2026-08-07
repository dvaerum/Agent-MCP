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
