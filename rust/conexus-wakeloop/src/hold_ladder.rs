//! Adaptive hold ladder for the `wait_for_events` long-poll. Port of
//! `agent_mcp/core/hold_ladder.py`.
//!
//! A heartbeat-capable client (Claude Code / OpenCode, or any client that
//! sends a `progressToken`) can have its connection parked open
//! indefinitely -- the server keeps it alive with heartbeats and returns
//! the instant a real event arrives, so an idle wait costs the agent
//! nothing. Some agents instead cap themselves with a short
//! `timeout_seconds` and re-poll, burning a model turn on every empty
//! return. The tool schema says to omit the timeout, but an agent may
//! ignore that.
//!
//! This module tracks, per agent, a run of *consecutive empty
//! short-polls* (a poll that passed a timeout SHORTER than the parked
//! hold and came back with nothing) and escalates:
//!
//! - below [`ADVISE_AFTER`] -- leave it alone. A one-off long monitor (a
//!   single big timeout) never trips this; only a run of short empty
//!   polls does.
//! - [`ADVISE_AFTER`]..[`OVERRIDE_AFTER`] -- ADVISE: return an escalating
//!   `hold_advisory` event telling the agent to drop the timeout.
//! - at/after [`OVERRIDE_AFTER`] -- OVERRIDE: ignore the agent's short
//!   cap and park the connection anyway, until a real event arrives.
//!
//! The run resets the moment a real event is delivered, or when the
//! agent stops capping itself (omits the timeout). State is in-memory
//! per `agent_id` -- a backend restart just resets the ladders, which is
//! harmless. Python backs this with an unlocked module-global `dict`,
//! safe there only because CPython's single-threaded event loop never
//! interleaves two calls into `note_empty_short_poll`/`reset` (see the
//! Phase D3 research report's concurrency-primitives section) -- Rust
//! has no such guarantee (a multi-threaded tokio runtime can genuinely
//! run two agents' calls on different OS threads at once), so this port
//! wraps the map in a real `Mutex`.
//!
//! Only ever applied by the caller when the client is heartbeat-capable
//! AND sent a progressToken: without heartbeats a long silent hold would
//! let the client's own idle watchdog kill the connection, which is
//! worse than the short cap.

use std::collections::HashMap;
use std::sync::Mutex;

/// Consecutive empty short-polls before each step. Tunable.
pub const ADVISE_AFTER: u32 = 20;
pub const OVERRIDE_AFTER: u32 = 30;

/// `agent_id -> current run length of consecutive empty short-polls`.
/// A real `Mutex`, not Python's unlocked dict -- see module doc.
static COUNTS: Mutex<Option<HashMap<String, u32>>> = Mutex::new(None);

fn with_counts<T>(f: impl FnOnce(&mut HashMap<String, u32>) -> T) -> T {
    let mut guard = COUNTS.lock().unwrap();
    f(guard.get_or_insert_with(HashMap::new))
}

pub fn get_count(agent_id: &str) -> u32 {
    with_counts(|counts| counts.get(agent_id).copied().unwrap_or(0))
}

/// Record one empty short-poll for `agent_id`; return the new run length.
pub fn note_empty_short_poll(agent_id: &str) -> u32 {
    with_counts(|counts| {
        let entry = counts.entry(agent_id.to_string()).or_insert(0);
        *entry += 1;
        *entry
    })
}

/// Clear the run (a real event landed, or the agent stopped capping).
pub fn reset(agent_id: &str) {
    with_counts(|counts| {
        counts.remove(agent_id);
    });
}

/// Drop all ladder state (test isolation helper).
pub fn clear() {
    with_counts(|counts| counts.clear());
}

/// What to do for one `wait_for_events` call given the run length.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LadderDecision {
    pub phase: LadderPhase,
    /// Ignore the caller's short timeout, park instead.
    pub override_hold: bool,
    /// Escalating text to return as a `hold_advisory` event.
    pub advisory: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LadderPhase {
    Normal,
    Advise,
    Override,
}

pub fn decide(count: u32) -> LadderDecision {
    if count >= OVERRIDE_AFTER {
        return LadderDecision {
            phase: LadderPhase::Override,
            override_hold: true,
            advisory: None,
        };
    }
    if count >= ADVISE_AFTER {
        return LadderDecision {
            phase: LadderPhase::Advise,
            override_hold: false,
            advisory: Some(advise_text(count)),
        };
    }
    LadderDecision {
        phase: LadderPhase::Normal,
        override_hold: false,
        advisory: None,
    }
}

const BASE_ADVICE: &str = "You keep calling wait_for_events with a short timeout_seconds while \
    nothing is waiting. You do NOT need to poll: this connection is held \
    open and kept alive with heartbeats, and it returns the instant a real \
    event (message, task, or directive) arrives. Drop timeout_seconds \
    entirely and just call wait_for_events() -- an idle wait then costs you \
    nothing.";

/// Escalating advisory: gentle at first, a countdown as it nears the
/// override, and a final notice on the last step before the server takes
/// over the hold.
fn advise_text(count: u32) -> String {
    let remaining = OVERRIDE_AFTER - count;
    if remaining <= 1 {
        return format!(
            "{BASE_ADVICE} FINAL NOTICE: from your next call on I will hold your \
             connection open regardless of the timeout you pass, until a real event \
             arrives."
        );
    }
    if count >= ADVISE_AFTER + (OVERRIDE_AFTER - ADVISE_AFTER) / 2 {
        return format!(
            "Reminder ({remaining} more short empty polls and I will override your \
             timeout and hold the connection open myself): {BASE_ADVICE}"
        );
    }
    BASE_ADVICE.to_string()
}

/// A synthetic `hold_advisory` event, shaped like every other event in the
/// `wait_for_events` envelope so the agent handles it uniformly. `now` is
/// an explicit ISO-8601 timestamp -- this crate's established "explicit
/// input over hidden state" convention (matches `conexus_auth::Tool::call`'s
/// own `now: &str`), rather than reading the wall clock internally the way
/// Python's `datetime.datetime.now()` does.
pub fn advisory_event(message: &str, now: &str) -> serde_json::Value {
    serde_json::json!({
        "type": "hold_advisory",
        "ref_id": null,
        "timestamp": now,
        "payload": {"message": message},
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex as StdMutex;

    // `COUNTS` is process-wide static state; serialize every test in this
    // module against it so parallel `cargo test` threads can't interleave
    // (the exact bug class this workspace has already hit twice --
    // conexus-vec's registration lock, conexus-auth::tool's `CALLED`
    // static -- see those modules' own comments).
    static TEST_LOCK: StdMutex<()> = StdMutex::new(());

    fn with_clean_state<T>(f: impl FnOnce() -> T) -> T {
        let _guard = TEST_LOCK.lock().unwrap();
        clear();
        let result = f();
        clear();
        result
    }

    #[test]
    fn counter_increments_and_resets() {
        with_clean_state(|| {
            assert_eq!(get_count("a"), 0);
            assert_eq!(note_empty_short_poll("a"), 1);
            assert_eq!(note_empty_short_poll("a"), 2);
            assert_eq!(get_count("a"), 2);
            reset("a");
            assert_eq!(get_count("a"), 0);
        });
    }

    #[test]
    fn counters_are_per_agent() {
        with_clean_state(|| {
            note_empty_short_poll("a");
            note_empty_short_poll("a");
            note_empty_short_poll("b");
            assert_eq!(get_count("a"), 2);
            assert_eq!(get_count("b"), 1);
            reset("a");
            assert_eq!(get_count("a"), 0);
            assert_eq!(get_count("b"), 1);
        });
    }

    #[test]
    fn below_threshold_is_normal() {
        for count in [0, 1, 5, ADVISE_AFTER - 1] {
            let d = decide(count);
            assert_eq!(d.phase, LadderPhase::Normal);
            assert!(!d.override_hold);
            assert_eq!(d.advisory, None);
        }
    }

    #[test]
    fn advise_band_advises_without_overriding() {
        for count in ADVISE_AFTER..OVERRIDE_AFTER {
            let d = decide(count);
            assert_eq!(d.phase, LadderPhase::Advise);
            assert!(!d.override_hold);
            assert!(d
                .advisory
                .as_deref()
                .is_some_and(|a| a.contains("timeout_seconds")));
        }
    }

    #[test]
    fn override_band_parks() {
        for count in [OVERRIDE_AFTER, OVERRIDE_AFTER + 50] {
            let d = decide(count);
            assert_eq!(d.phase, LadderPhase::Override);
            assert!(d.override_hold);
        }
    }

    #[test]
    fn escalation_gets_stronger() {
        let first = decide(ADVISE_AFTER).advisory.unwrap();
        let last = decide(OVERRIDE_AFTER - 1).advisory.unwrap();
        // The last advise step before override is a FINAL NOTICE; the
        // first is not.
        assert!(last.contains("FINAL NOTICE"));
        assert!(!first.contains("FINAL NOTICE"));
    }

    #[test]
    fn advisory_event_shape() {
        let ev = advisory_event("stop it", "2026-01-01T00:00:00Z");
        assert_eq!(ev["type"], "hold_advisory");
        assert_eq!(ev["payload"]["message"], "stop it");
        assert_eq!(ev["timestamp"], "2026-01-01T00:00:00Z");
    }
}
