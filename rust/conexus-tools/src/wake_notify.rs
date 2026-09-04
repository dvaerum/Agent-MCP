//! Classifies which post-write "wake" a `project_context`/
//! `project_settings` write on a given `context_key` requires.
//!
//! Port of the CLASSIFICATION half of `agent_mcp/tools/
//! project_context_tools.py::emit_context_write_wakes` (`_is_worker_
//! policy_toggle` / `_is_loop_toggle`) — deliberately NOT the delivery
//! half. Python's helper both classifies AND fires the wake in the
//! same function, because Python already has the live MCP session
//! registry (`_emit_tools_list_changed`) and the wake-loop's global
//! flag-recheck call (`g.wake_all_for_flag_recheck()`) sitting right
//! there to call into. Neither exists yet on the Rust side: the MCP
//! `Peer` registry is Phase D1 step 3 (the `conexus` binary, not yet
//! built) and the wake-loop actor system is Phase D3 (explicitly
//! deferred, XL-sized). Porting the delivery half now would mean
//! either inventing throwaway stand-ins for both, or silently
//! dropping the wake — this crate's tools instead surface WHICH
//! wake(s) a write requires as data
//! (`ToolResult::Ok.data["wakes"]`, see `project_settings_tools`), so
//! whichever future layer owns a live `Peer` registry and wake-loop
//! can read that array and actually fire them. This function is the
//! single source of truth both that future MCP call path and a future
//! `project_context_tools` port will consult — the same "one
//! classifier, every write surface uses it" design BL-R14-1 exists to
//! enforce in Python, just split across a phase boundary instead of a
//! module boundary.

use regex::Regex;
use std::sync::LazyLock;

static WORKER_POLICY_TOGGLE_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)^config_allow_worker_").unwrap());

const LOOP_TOGGLE_KEY: &str = "config_auto_event_loop_global";

/// A post-write wake a `context_key` write may require. `as_str()` is
/// the wire label embedded in a tool's `ToolResult::Ok.data["wakes"]`
/// array.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Wake {
    /// `config_allow_worker_*` (a worker-tool-visibility toggle) —
    /// push `notifications/tools/list_changed` so subscribed workers
    /// re-fetch `tools/list`. Python: `_emit_tools_list_changed`.
    ToolsListChanged,
    /// `config_auto_event_loop_global` (the global event-loop toggle)
    /// — wake in-flight `wait_for_events` waiters to re-evaluate.
    /// Python: `g.wake_all_for_flag_recheck()`.
    WakeAllForFlagRecheck,
}

impl Wake {
    pub fn as_str(self) -> &'static str {
        match self {
            Wake::ToolsListChanged => "tools_list_changed",
            Wake::WakeAllForFlagRecheck => "wake_all_for_flag_recheck",
        }
    }
}

/// Which wake(s), if any, a write to `context_key` requires. Pure,
/// zero-I/O, exhaustively testable without any live MCP session or
/// wake-loop — same shape as Python's two independent predicates, but
/// combined into one call since both this crate's write paths
/// (`update_project_settings`/`delete_project_settings`) need the
/// full set, not one predicate at a time.
pub fn wakes_for(context_key: &str) -> Vec<Wake> {
    let mut wakes = Vec::new();
    if WORKER_POLICY_TOGGLE_RE.is_match(context_key) {
        wakes.push(Wake::ToolsListChanged);
    }
    if context_key == LOOP_TOGGLE_KEY {
        wakes.push(Wake::WakeAllForFlagRecheck);
    }
    wakes
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn worker_policy_toggle_key_yields_tools_list_changed() {
        assert_eq!(
            wakes_for("config_allow_worker_to_worker"),
            vec![Wake::ToolsListChanged]
        );
    }

    #[test]
    fn worker_policy_toggle_match_is_case_insensitive() {
        assert_eq!(
            wakes_for("CONFIG_ALLOW_WORKER_ANYTHING"),
            vec![Wake::ToolsListChanged]
        );
    }

    #[test]
    fn loop_toggle_key_yields_wake_all_for_flag_recheck() {
        assert_eq!(
            wakes_for("config_auto_event_loop_global"),
            vec![Wake::WakeAllForFlagRecheck]
        );
    }

    #[test]
    fn an_unrelated_key_yields_no_wakes() {
        assert_eq!(wakes_for("config_max_agents"), Vec::<Wake>::new());
    }

    #[test]
    fn a_prefix_match_that_isnt_a_full_word_boundary_still_matches() {
        // Mirrors Python's `re.match` (prefix-anchored, not full-key)
        // semantics exactly -- any config_allow_worker_* suffix counts.
        assert_eq!(
            wakes_for("config_allow_worker_"),
            vec![Wake::ToolsListChanged]
        );
    }

    #[test]
    fn as_str_labels_are_stable_wire_values() {
        assert_eq!(Wake::ToolsListChanged.as_str(), "tools_list_changed");
        assert_eq!(
            Wake::WakeAllForFlagRecheck.as_str(),
            "wake_all_for_flag_recheck"
        );
    }
}
