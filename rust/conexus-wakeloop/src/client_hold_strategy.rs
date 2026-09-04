//! Per-client connection-hold strategy for `wait_for_events`. Port of
//! `agent_mcp/core/client_hold_strategy.py`.
//!
//! The event-loop long-hold feature holds a single `wait_for_events`
//! connection open far longer than the legacy ~60s so an agent burns
//! fewer reconnect model-turns. HOW LONG a connection may hold -- and
//! whether the server emits MCP progress-notification heartbeats to keep
//! the client from timing out -- depends on the CLIENT, because only
//! some clients reset their idle timeout when they receive a
//! `notifications/progress` frame.
//!
//! Hybrid identity-first / feature-detect resolution:
//!
//! - **Identity table** ([`CLIENT_HOLD_STRATEGY`]) keyed by the exact
//!   `clientInfo.name` a client sends in its MCP `initialize` handshake
//!   (normalized case/spacing). Authoritative because pure
//!   feature-detection is UNSAFE: Cursor sends a `progressToken` (to
//!   render progress in its UI) but never resets its timeout on it, so
//!   keying only on "sent a token" would hand Cursor a long hold its own
//!   timeout then aborts.
//! - **Feature-detect fallback** for a client NOT in the table: if the
//!   tool call carried a `progressToken`, assume heartbeat-capable with
//!   no cap; otherwise the safe silent-hold default.

/// Cadence at which the wait loop emits a `notifications/progress`
/// heartbeat for a heartbeat-capable client. Must sit comfortably under
/// the tightest heartbeat-resettable idle timeout among heartbeat clients
/// (OpenCode's default is 60s, resets on progress) -- 25s leaves ample
/// margin for one dropped/slow frame.
pub const HEARTBEAT_INTERVAL_SECONDS: u64 = 25;

/// Silent hold for a no-heartbeat client. Sits just under the universal
/// 60s MCP SDK default so the hold returns cleanly (empty envelope ->
/// reconnect) BEFORE the client aborts the call. Applies to Cursor / Zed
/// / Cline / Continue and any unknown client that sent no progressToken.
pub const NO_HEARTBEAT_HOLD_SECONDS: u64 = 55;

/// Claude Code resets its ~5-min idle watchdog on each progress frame,
/// but a SEPARATE ~27.8h wall-clock cap is NOT progress-resettable.
/// Recycle the connection at 24h (comfortably under 27.8h) so the client
/// never hits that hard wall mid-hold.
pub const CLAUDE_CODE_HOLD_CAP_SECONDS: u64 = 24 * 60 * 60;

/// How long one `wait_for_events` connection may hold, and whether to
/// emit heartbeats while it does.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HoldStrategy {
    /// Emit `notifications/progress` every [`HEARTBEAT_INTERVAL_SECONDS`]
    /// to keep the client's idle timer from firing. `false` => silent
    /// hold, no progress frames.
    pub heartbeat: bool,
    /// Max seconds ONE connection may hold before it recycles (returns
    /// an empty envelope -> the agent reconnects). `None` means "no
    /// per-connection recycle" -- the connection holds until a real
    /// event (or the idle-stop window).
    pub hold_cap: Option<u64>,
}

// Heartbeat / capped -- Claude Code: resets idle watchdog on progress,
// but recycle at 24h to stay under its non-resettable ~27.8h wall-clock
// cap.
const CLAUDE_CODE: HoldStrategy = HoldStrategy {
    heartbeat: true,
    hold_cap: Some(CLAUDE_CODE_HOLD_CAP_SECONDS),
};

// Heartbeat / no cap -- OpenCode (and unknown-with-progressToken):
// resets on progress with no maxTotalTimeout, so one connection can span
// the whole idle-stop window.
const HEARTBEAT_NO_CAP: HoldStrategy = HoldStrategy {
    heartbeat: true,
    hold_cap: None,
};

// No heartbeat -- a single fixed client timeout == its hard cap. Silent
// ~55s hold, then reconnect.
const NO_HEARTBEAT: HoldStrategy = HoldStrategy {
    heartbeat: false,
    hold_cap: Some(NO_HEARTBEAT_HOLD_SECONDS),
};

/// Identity table keyed by NORMALIZED `clientInfo.name` (see
/// [`normalize_client_name`]). One row per researched client; add a
/// newly-researched client here in one line.
pub fn lookup(normalized_name: &str) -> Option<HoldStrategy> {
    match normalized_name {
        "claude-code" => Some(CLAUDE_CODE),
        "opencode" => Some(HEARTBEAT_NO_CAP),
        // Sends a progressToken but does NOT reset -- pin by identity.
        "cursor" => Some(NO_HEARTBEAT),
        "cline" => Some(NO_HEARTBEAT),
        "zed" => Some(NO_HEARTBEAT),
        "continue" => Some(NO_HEARTBEAT),
        _ => None,
    }
}

/// Normalize a raw `clientInfo.name` for table lookup.
///
/// Real handshakes vary in case/spacing (`"claude-code"` vs a
/// hypothetical `"Claude Code"`); lower-case, strip, collapse internal
/// whitespace, AND map spaces to hyphens so a space-separated display
/// name (`"Claude Code"`) matches the hyphenated table key
/// (`"claude-code"`). Returns `None` for an empty/absent name.
pub fn normalize_client_name(name: Option<&str>) -> Option<String> {
    let name = name?;
    let collapsed = name.split_whitespace().collect::<Vec<_>>().join(" ");
    let normalized = collapsed.trim().to_lowercase().replace(' ', "-");
    if normalized.is_empty() {
        None
    } else {
        Some(normalized)
    }
}

/// Resolve the connection-hold strategy for one `wait_for_events` call.
///
/// Hybrid resolution:
/// 1. Look up the normalized `client_name` in the identity table. A hit
///    is authoritative -- it wins even when the call carried a
///    `progressToken` (the Cursor false-positive guard).
/// 2. Miss => feature-detect: `has_progress_token` => heartbeat / no cap
///    (assume a well-behaved unknown client that will reset on
///    progress); else the safe silent-hold default.
pub fn resolve_hold_strategy(client_name: Option<&str>, has_progress_token: bool) -> HoldStrategy {
    if let Some(normalized) = normalize_client_name(client_name) {
        if let Some(known) = lookup(&normalized) {
            return known;
        }
    }
    if has_progress_token {
        HEARTBEAT_NO_CAP
    } else {
        NO_HEARTBEAT
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_clients_resolve_by_identity() {
        // Even with a progressToken present, the identity table wins.
        let cases: &[(&str, bool, Option<u64>)] = &[
            ("claude-code", true, Some(CLAUDE_CODE_HOLD_CAP_SECONDS)),
            ("opencode", true, None),
            ("cursor", false, Some(NO_HEARTBEAT_HOLD_SECONDS)),
            ("cline", false, Some(NO_HEARTBEAT_HOLD_SECONDS)),
            ("zed", false, Some(NO_HEARTBEAT_HOLD_SECONDS)),
            ("continue", false, Some(NO_HEARTBEAT_HOLD_SECONDS)),
        ];
        for (name, expected_heartbeat, expected_cap) in cases {
            let strat = resolve_hold_strategy(Some(name), true);
            assert_eq!(strat.heartbeat, *expected_heartbeat, "client={name}");
            assert_eq!(strat.hold_cap, *expected_cap, "client={name}");
        }
    }

    #[test]
    fn cursor_false_positive_guard() {
        let strat = resolve_hold_strategy(Some("cursor"), true);
        assert!(!strat.heartbeat);
        assert_eq!(strat.hold_cap, Some(NO_HEARTBEAT_HOLD_SECONDS));
    }

    #[test]
    fn unknown_client_with_token_gets_heartbeat_no_cap() {
        let strat = resolve_hold_strategy(Some("some-future-ide"), true);
        assert!(strat.heartbeat);
        assert_eq!(strat.hold_cap, None);
    }

    #[test]
    fn unknown_client_without_token_gets_silent_default() {
        let strat = resolve_hold_strategy(Some("some-future-ide"), false);
        assert!(!strat.heartbeat);
        assert_eq!(strat.hold_cap, Some(NO_HEARTBEAT_HOLD_SECONDS));
    }

    #[test]
    fn no_client_name_without_token_is_silent_default() {
        let strat = resolve_hold_strategy(None, false);
        assert!(!strat.heartbeat);
        assert_eq!(strat.hold_cap, Some(NO_HEARTBEAT_HOLD_SECONDS));
    }

    #[test]
    fn no_client_name_with_token_feature_detects() {
        let strat = resolve_hold_strategy(None, true);
        assert!(strat.heartbeat);
        assert_eq!(strat.hold_cap, None);
    }

    #[test]
    fn normalize_client_name_cases() {
        let cases: &[(Option<&str>, Option<&str>)] = &[
            (Some("claude-code"), Some("claude-code")),
            (Some("Claude-Code"), Some("claude-code")),
            (Some("  claude-code  "), Some("claude-code")),
            // Spaces collapse to hyphens so a space-separated display
            // name matches the hyphenated table key (`claude-code`).
            (Some("Claude   Code"), Some("claude-code")),
            (Some("Claude Code"), Some("claude-code")),
            (Some("OpenCode"), Some("opencode")),
            (Some(""), None),
            (None, None),
        ];
        for (raw, expected) in cases {
            assert_eq!(
                normalize_client_name(*raw).as_deref(),
                *expected,
                "raw={raw:?}"
            );
        }
    }

    #[test]
    fn normalization_applies_in_resolution() {
        // A client that sends mixed-case identity still hits the table.
        let strat = resolve_hold_strategy(Some("Claude-Code"), false);
        assert!(strat.heartbeat);
        assert_eq!(strat.hold_cap, Some(CLAUDE_CODE_HOLD_CAP_SECONDS));
    }

    #[test]
    fn space_separated_display_name_hits_table() {
        // A space-separated `clientInfo.name` ("Claude Code") must
        // resolve to the heartbeat hold -- spaces map to hyphens so it
        // matches the `claude-code` table key rather than falling to
        // no-heartbeat.
        let strat = resolve_hold_strategy(Some("Claude Code"), false);
        assert!(strat.heartbeat);
        assert_eq!(strat.hold_cap, Some(CLAUDE_CODE_HOLD_CAP_SECONDS));
    }
}
