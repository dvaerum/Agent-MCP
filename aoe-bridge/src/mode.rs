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
}
