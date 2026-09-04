//! The per-agent `wait_for_events` waiter registry. Port of the waiter
//! half of `agent_mcp/core/state.py` (`register_waiter`/
//! `unregister_waiter`/`supersede_prior_waiters`/`notify_waiters`/
//! `dispatch_synthetic_event`).
//!
//! ## Newest-wins, not N-way fan-out (a deliberate simplification,
//! verified against the real behavior -- not guessed)
//!
//! Python's `agent_event_waiters` is a `Dict[str, List[asyncio.Queue]]`
//! -- a LIST per agent, a leftover from ADR-0012's original N-concurrent-
//! waiters design. But `register_waiter` immediately calls
//! `supersede_prior_waiters`, which evicts every OTHER queue in that
//! list the moment a new one registers -- so in practice the list holds
//! more than one live entry only for the instant between those two
//! calls. The Phase D3 research report's own conclusion: "treat 'at
//! most one live waiter per agent, newest wins' as the actual current
//! contract, and the N-way fan-out plumbing... as a leftover mechanism."
//! This registry implements that actual contract directly -- ONE sender
//! per agent, not a list -- rather than porting the vestigial list
//! shape.
//!
//! ## Why the channel only ever carries a bare `Wake` signal, never an
//! event payload
//!
//! Python has two producers: `notify_waiters` (DB-backed events --
//! messages, task changes) pushes a bare sentinel (`None`) purely to
//! release the waiter's `queue.get()`, and `dispatch_synthetic_event`
//! (events with no dedicated event-table row, e.g.
//! `unassigned_task_appeared`) pushes the actual event dict. It looks
//! like the channel needs to carry real payloads for the second case --
//! but `assemble_event_feed`'s own docstring pins BL-R31-2: the
//! synthetic-queue copy and a fresh DB re-query of the same underlying
//! state (`_collect_unassigned_task_events_for`, an unbounded query run
//! on EVERY wake regardless of what woke it) are explicitly DEDUPED
//! against each other, keeping the DB copy. That dedup only makes sense
//! if the DB query alone is already sufficient for correctness -- the
//! synthetic push is a LATENCY optimization (wake sooner than the next
//! poll interval), not a correctness requirement. So a Rust
//! `assemble_event_feed` port that re-derives every relevant event type
//! (DB-backed AND synthesized-from-DB-state) on every wake needs no
//! payload on the channel at all: waking is waking, regardless of which
//! producer caused it. [`WakeSignal::Wake`] intentionally carries no
//! data.

use std::collections::HashMap;
use std::sync::Mutex;

use tokio::sync::mpsc::{self, Receiver, Sender};

/// What a waiter's channel carries. Never an event payload -- see the
/// module doc for why that's safe, not a missing feature.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WakeSignal {
    /// A DB-backed write (or a synthetic, DB-state-derived event) may
    /// have happened -- re-derive the event feed from the DB.
    Wake,
    /// A newer `wait_for_events` call registered for this agent; this
    /// waiter must return a `connection_superseded` envelope and stop
    /// (NOT `stop_listening` -- the agent's own loop should keep going
    /// on the new connection, only this particular tool call ends).
    Superseded,
}

/// Bounded, not Python's unbounded `asyncio.Queue` -- Python's own
/// module doc argues the depth is "self-limiting" (newest-wins evicts
/// stale waiters immediately; a live waiter drains on every wake), so a
/// generous fixed capacity is behaviorally equivalent in every realistic
/// scenario without needing an unbounded-channel type `stream_gates`'s
/// `RevalidatingStream` doesn't already support.
const CHANNEL_CAPACITY: usize = 32;

/// Per-agent single-waiter registry. A real `Mutex<HashMap<...>>`, not
/// Python's unlocked dict -- same GIL-vs-real-threads treatment as
/// [`crate::hold_ladder`]/[`crate::idle_reminder`].
pub struct WaiterRegistry {
    waiters: Mutex<HashMap<String, Sender<WakeSignal>>>,
}

impl Default for WaiterRegistry {
    fn default() -> Self {
        Self::new()
    }
}

impl WaiterRegistry {
    pub fn new() -> Self {
        Self {
            waiters: Mutex::new(HashMap::new()),
        }
    }

    /// Register a fresh waiter for `agent_id`. Newest-wins: any prior
    /// waiter for this same agent is superseded (sent
    /// [`WakeSignal::Superseded`] on ITS OWN channel) before the new one
    /// is installed. Returns the sender (hand it back to
    /// [`Self::unregister`] on the way out) and the receiver to poll.
    ///
    /// Deliberately synchronous, matching Python's `register_waiter`/
    /// `supersede_prior_waiters` (both plain, non-`async def`
    /// functions) -- registration and eviction never await, so they can
    /// never race a concurrent notify/register the way an `await`-ing
    /// version could.
    pub fn register(&self, agent_id: &str) -> (Sender<WakeSignal>, Receiver<WakeSignal>) {
        let (tx, rx) = mpsc::channel(CHANNEL_CAPACITY);
        let mut waiters = self.waiters.lock().unwrap();
        if let Some(old) = waiters.insert(agent_id.to_string(), tx.clone()) {
            // Best-effort, matching Python's `except Exception: pass`
            // around `put_nowait` -- a full/closed channel here means
            // the old waiter is already gone or already about to notice
            // on its own; either way there is nothing more to do.
            let _ = old.try_send(WakeSignal::Superseded);
        }
        (tx, rx)
    }

    /// Remove this waiter's registration, but ONLY if it is STILL the
    /// currently registered one for this agent. Guards against a
    /// stale/late unregister from an already-superseded waiter (running
    /// its own cleanup after losing a race) wiping out a NEWER waiter's
    /// live registration. Idempotent, matching Python's
    /// `unregister_waiter` doc ("a double-unregister... is a no-op").
    pub fn unregister(&self, agent_id: &str, sender: &Sender<WakeSignal>) {
        let mut waiters = self.waiters.lock().unwrap();
        if let Some(current) = waiters.get(agent_id) {
            if current.same_channel(sender) {
                waiters.remove(agent_id);
            }
        }
    }

    /// Wake the registered waiter for `agent_id`, if any. A no-op when
    /// no waiter is parked (Python: "if not waiters: return") --
    /// deliberately synchronous and non-blocking (`try_send`, never
    /// `.send().await`), matching the structural contract the research
    /// report's concurrency section calls out (`notify_agent_inbox`/
    /// `EventBus.deliver` are pinned NEVER-async by their own regression
    /// test) -- Rust enforces the same property here via the type
    /// system rather than a test.
    pub fn notify(&self, agent_id: &str) {
        let waiters = self.waiters.lock().unwrap();
        if let Some(sender) = waiters.get(agent_id) {
            // Best-effort, matching Python's `except Exception: pass`.
            let _ = sender.try_send(WakeSignal::Wake);
        }
    }

    /// How many waiters are currently parked for this agent (0 or 1 --
    /// see the module doc on why this registry never holds more than
    /// one). Mirrors Python's `waiter_count` (used by the dashboard's
    /// `wait_for_events_in_flight` flag).
    pub fn waiter_count(&self, agent_id: &str) -> usize {
        usize::from(self.waiters.lock().unwrap().contains_key(agent_id))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn a_registered_waiter_receives_a_notify() {
        let registry = WaiterRegistry::new();
        let (_tx, mut rx) = registry.register("a1");
        registry.notify("a1");
        assert_eq!(rx.recv().await, Some(WakeSignal::Wake));
    }

    #[tokio::test]
    async fn notify_with_no_registered_waiter_is_a_silent_no_op() {
        let registry = WaiterRegistry::new();
        // Must not panic -- no waiter registered at all.
        registry.notify("nobody");
    }

    #[tokio::test]
    async fn registering_a_second_waiter_supersedes_the_first() {
        let registry = WaiterRegistry::new();
        let (_tx1, mut rx1) = registry.register("a1");
        let (_tx2, mut rx2) = registry.register("a1");

        assert_eq!(rx1.recv().await, Some(WakeSignal::Superseded));

        registry.notify("a1");
        assert_eq!(rx2.recv().await, Some(WakeSignal::Wake));
        // The superseded waiter's channel got exactly the supersede
        // signal, not the subsequent wake too.
        assert!(rx1.try_recv().is_err());
    }

    #[tokio::test]
    async fn waiters_are_independent_per_agent() {
        let registry = WaiterRegistry::new();
        let (_tx_a, mut rx_a) = registry.register("a1");
        let (_tx_b, mut rx_b) = registry.register("b1");

        registry.notify("a1");
        assert_eq!(rx_a.recv().await, Some(WakeSignal::Wake));
        assert!(rx_b.try_recv().is_err());

        registry.notify("b1");
        assert_eq!(rx_b.recv().await, Some(WakeSignal::Wake));
    }

    #[tokio::test]
    async fn unregister_removes_the_current_waiter() {
        let registry = WaiterRegistry::new();
        let (tx, _rx) = registry.register("a1");
        assert_eq!(registry.waiter_count("a1"), 1);
        registry.unregister("a1", &tx);
        assert_eq!(registry.waiter_count("a1"), 0);
    }

    #[tokio::test]
    async fn unregister_is_idempotent() {
        let registry = WaiterRegistry::new();
        let (tx, _rx) = registry.register("a1");
        registry.unregister("a1", &tx);
        // A second unregister of the same (already-removed) sender must
        // not panic and stays a no-op.
        registry.unregister("a1", &tx);
        assert_eq!(registry.waiter_count("a1"), 0);
    }

    #[tokio::test]
    async fn a_stale_unregister_from_a_superseded_waiter_does_not_evict_the_newer_one() {
        // The exact race this design guards against: an old waiter's
        // own cleanup path runs AFTER a newer waiter has already taken
        // over -- its unregister call must not remove the new one.
        let registry = WaiterRegistry::new();
        let (tx1, _rx1) = registry.register("a1");
        let (_tx2, _rx2) = registry.register("a1");
        assert_eq!(registry.waiter_count("a1"), 1);

        registry.unregister("a1", &tx1); // stale -- tx1 is no longer current

        assert_eq!(
            registry.waiter_count("a1"),
            1,
            "the newer waiter's registration must survive the stale unregister"
        );
    }

    #[tokio::test]
    async fn waiter_count_reflects_registration_state() {
        let registry = WaiterRegistry::new();
        assert_eq!(registry.waiter_count("a1"), 0);
        let (tx, _rx) = registry.register("a1");
        assert_eq!(registry.waiter_count("a1"), 1);
        registry.unregister("a1", &tx);
        assert_eq!(registry.waiter_count("a1"), 0);
    }
}
