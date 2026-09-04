//! Classifies which post-write "wake" a `project_context`/
//! `project_settings` write on a given `context_key` requires.
//!
//! Port of `agent_mcp/tools/project_context_tools.py::
//! emit_context_write_wakes` (`_is_worker_policy_toggle`/
//! `_is_loop_toggle` classification, plus real delivery for the half
//! that's now buildable).
//!
//! ## Classification vs. delivery — history and current split
//!
//! When this module was first written (Phase D1), NEITHER of Python's
//! two delivery targets existed on the Rust side yet: the MCP `Peer`
//! registry was still unbuilt, and the wake-loop actor system was
//! Phase D3's own explicitly-deferred, XL-sized scope. [`wakes_for`]
//! alone shipped as the classification half; every write tool
//! surfaced the result as data (`ToolResult::Ok.data["wakes"]`, see
//! `project_settings_tools`) for a future layer to act on.
//!
//! Phase D3/D4 have since built the `WaiterRegistry` (`conexus-
//! wakeloop`) and closed the `WaiterRegistry::notify()` gap
//! (`task_tools.rs`'s broadcast-to-every-live-agent pattern). That
//! means `Wake::WakeAllForFlagRecheck` is now REALLY deliverable —
//! [`deliver`] does so, matching Python's `g.wake_all_for_flag_
//! recheck()` exactly (every live agent's parked `wait_for_events`
//! waiter, if any, is woken to re-evaluate its flags immediately).
//! `Wake::ToolsListChanged` stays data-only: no live MCP `Peer`/
//! session registry exists yet to push
//! `notifications/tools/list_changed` through, so that half is
//! unchanged from the original design. [`deliver`] still returns the
//! full wake-label array either way, so a caller's `data["wakes"]`
//! response shape doesn't change based on which half got delivered.

use conexus_db::agent_repository::AgentRepository;
use conexus_wakeloop::waiter_registry::WaiterRegistry;
use regex::Regex;
use rusqlite::Connection;
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

/// Classifies `context_key`'s wake(s) via [`wakes_for`] and delivers
/// whichever half is real (see module doc): a `WakeAllForFlagRecheck`
/// broadcasts to every live agent's parked waiter; `ToolsListChanged`
/// stays classification-only. Returns the wake label array either
/// way, for the caller's own `data["wakes"]` response field. A DB
/// error listing live agents degrades to skipping the broadcast
/// (matching this crate's own "never let a best-effort side wake fail
/// the tool's real write" convention) rather than failing the call.
pub fn deliver(
    conn: &Connection,
    waiter_registry: &WaiterRegistry,
    context_key: &str,
) -> Vec<Wake> {
    let wakes = wakes_for(context_key);
    if wakes.contains(&Wake::WakeAllForFlagRecheck) {
        if let Ok(agents) = AgentRepository::list_active(conn) {
            for agent in agents {
                waiter_registry.notify(&agent.agent_id);
            }
        }
    }
    wakes
}

/// Bulk sibling of [`deliver`] (Python: `emit_context_write_wakes_bulk`).
/// A batch write touches N keys in one call; each wake fires AT MOST
/// ONCE for the whole batch if ANY key matches, using the same
/// [`wakes_for`] classification -- so a batch containing even one
/// wake-eligible key can't under-fire relative to the single-key seam.
pub fn deliver_bulk<'a>(
    conn: &Connection,
    waiter_registry: &WaiterRegistry,
    context_keys: impl IntoIterator<Item = &'a str>,
) -> Vec<Wake> {
    let mut wakes: Vec<Wake> = Vec::new();
    for key in context_keys {
        for wake in wakes_for(key) {
            if !wakes.contains(&wake) {
                wakes.push(wake);
            }
        }
    }
    if wakes.contains(&Wake::WakeAllForFlagRecheck) {
        if let Ok(agents) = AgentRepository::list_active(conn) {
            for agent in agents {
                waiter_registry.notify(&agent.agent_id);
            }
        }
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

    #[test]
    fn deliver_broadcasts_wake_all_for_flag_recheck_to_every_live_agent() {
        let conn = Connection::open_in_memory().unwrap();
        conexus_db::schema::init_schema(&conn).unwrap();
        conexus_db::agent_repository::AgentRepository::create(
            &conn,
            conexus_db::agent_repository::NewAgent {
                token: "tok",
                agent_id: "alice",
                created_at: "2026-06-01T00:00:00Z",
                status: "active",
                current_task: None,
                working_directory: "/tmp",
                color: None,
                agent_role: "worker",
            },
        )
        .unwrap();
        let registry = WaiterRegistry::new();
        let (_sender, mut receiver) = registry.register("alice");

        let wakes = deliver(&conn, &registry, "config_auto_event_loop_global");

        assert_eq!(wakes, vec![Wake::WakeAllForFlagRecheck]);
        assert!(receiver.try_recv().is_ok());
    }

    #[test]
    fn deliver_does_not_broadcast_for_a_tools_list_changed_only_key() {
        let conn = Connection::open_in_memory().unwrap();
        conexus_db::schema::init_schema(&conn).unwrap();
        let registry = WaiterRegistry::new();
        let wakes = deliver(&conn, &registry, "config_allow_worker_to_worker");
        assert_eq!(wakes, vec![Wake::ToolsListChanged]);
    }
}
