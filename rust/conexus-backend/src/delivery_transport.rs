//! Per-worker delivery-transport registry (ADR-0021). Port of
//! `agent_mcp/features/delivery_transport.py`.
//!
//! A runtime (e.g. the AoE bridge) opens one delivery stream per
//! worker (`GET /api/delivery/stream`, worker-bearer authed) and
//! reports that worker's session status (`POST /api/delivery/status`).
//! This in-process hub holds, per `agent_id`: the live SSE
//! subscription(s) (a `Vec`, not a single slot -- a brief overlap
//! across a reconnect tolerates without dropping frames, matching
//! Python exactly) and the last-reported `transport-status`, a signal
//! SEPARATE from this backend's own connection-presence (the
//! `WaiterRegistry`-derived `online` field).
//!
//! Modelled on [`crate::operator_events::OperatorEventsHub`], but
//! keyed by `agent_id` (one worker, potentially several concurrent
//! streams) rather than an opaque subscriber id, and carrying status.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use serde_json::Value;
use tokio::sync::mpsc;

/// The statuses a runtime may report (ADR-0021).
pub const VALID_STATUSES: &[&str] = &["working", "idle", "dormant", "dead"];

/// Bound each subscriber queue exactly like `operator_events`'s own --
/// a stalled SSE reader must not accumulate frames without limit;
/// `push` drops the incoming frame past this depth rather than
/// blocking the producer (R14-F1).
const QUEUE_CAPACITY: usize = 256;

struct SubscriberRecord {
    id: u64,
    agent_id: String,
    sender: mpsc::Sender<Value>,
}

/// A live delivery-stream handle for one worker. Deliberately carries
/// no `agent_id` -- unlike Python's `Subscription` dataclass (whose
/// `agent_id` field keys its own removal from `_subs[agent_id]`), this
/// hub's `unsubscribe` takes the opaque `id` alone (see
/// [`OperatorEventsHub`](crate::operator_events::OperatorEventsHub)'s
/// own precedent for why), so the caller (`delivery_stream`'s handler,
/// which already has the `agent_id` from its own gate-resolved
/// identity) never needs it back out of this handle.
pub struct Subscription {
    pub id: u64,
    pub receiver: mpsc::Receiver<Value>,
}

#[derive(Default)]
pub struct DeliveryTransportHub {
    subs: Mutex<Vec<SubscriberRecord>>,
    status: Mutex<HashMap<String, String>>,
    next_id: AtomicU64,
}

impl DeliveryTransportHub {
    pub fn new() -> Self {
        Self {
            subs: Mutex::new(Vec::new()),
            status: Mutex::new(HashMap::new()),
            next_id: AtomicU64::new(1),
        }
    }

    /// Register a live delivery stream for `agent_id`.
    pub fn subscribe(&self, agent_id: &str) -> Subscription {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let (sender, receiver) = mpsc::channel(QUEUE_CAPACITY);
        let mut subs = self.subs.lock().expect("delivery_transport mutex poisoned");
        subs.push(SubscriberRecord {
            id,
            agent_id: agent_id.to_string(),
            sender,
        });
        Subscription { id, receiver }
    }

    /// Drop a stream (on disconnect). A disconnect is NOT a status
    /// change -- `transport-status` only changes via an explicit
    /// status report (ADR-0021), so a transient drop doesn't flip a
    /// worker to `dead`. Idempotent.
    pub fn unsubscribe(&self, id: u64) {
        let mut subs = self.subs.lock().expect("delivery_transport mutex poisoned");
        subs.retain(|s| s.id != id);
    }

    /// Enqueue `frame` onto every live stream for `agent_id`. Returns
    /// the number of streams it was enqueued onto (`0` = no live
    /// transport, or every live stream's queue was full -- the
    /// caller's own fallback policy re-fires next cycle, matching
    /// Python's documented contract exactly).
    pub fn push(&self, agent_id: &str, frame: Value) -> usize {
        let subs = self.subs.lock().expect("delivery_transport mutex poisoned");
        let mut delivered = 0usize;
        for sub in subs.iter().filter(|s| s.agent_id == agent_id) {
            if sub.sender.try_send(frame.clone()).is_ok() {
                delivered += 1;
            }
        }
        delivered
    }

    /// True iff `agent_id` has at least one live delivery stream.
    pub fn is_connected(&self, agent_id: &str) -> bool {
        let subs = self.subs.lock().expect("delivery_transport mutex poisoned");
        subs.iter().any(|s| s.agent_id == agent_id)
    }

    pub fn set_status(&self, agent_id: &str, status: &str) {
        let mut map = self
            .status
            .lock()
            .expect("delivery_transport mutex poisoned");
        map.insert(agent_id.to_string(), status.to_string());
    }

    pub fn get_status(&self, agent_id: &str) -> Option<String> {
        let map = self
            .status
            .lock()
            .expect("delivery_transport mutex poisoned");
        map.get(agent_id).cloned()
    }

    /// Observability: one row per worker with a live stream and/or a
    /// reported status, agent_id-sorted (matching Python's `sorted`).
    /// Ported for parity even though Python's own `snapshot()` has no
    /// real caller anywhere in `agent_mcp/` either (confirmed by grep)
    /// -- no REST route exposes a delivery-transport status summary,
    /// unlike `operator_events`'s `GET /api/events/status`. Kept, not
    /// dropped, since a future observability route is a plausible,
    /// low-cost addition once this hub has real production traffic to
    /// watch.
    #[allow(dead_code)]
    pub fn snapshot(&self) -> Vec<Value> {
        let subs = self.subs.lock().expect("delivery_transport mutex poisoned");
        let status = self
            .status
            .lock()
            .expect("delivery_transport mutex poisoned");

        let mut counts: HashMap<&str, usize> = HashMap::new();
        for s in subs.iter() {
            *counts.entry(s.agent_id.as_str()).or_insert(0) += 1;
        }
        let mut ids: Vec<&str> = counts.keys().copied().collect();
        for k in status.keys() {
            if !counts.contains_key(k.as_str()) {
                ids.push(k.as_str());
            }
        }
        ids.sort_unstable();

        ids.into_iter()
            .map(|agent_id| {
                let streams = counts.get(agent_id).copied().unwrap_or(0);
                serde_json::json!({
                    "agent_id": agent_id,
                    "connected": streams > 0,
                    "streams": streams,
                    "status": status.get(agent_id),
                })
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn subscribe_push_and_receive() {
        let hub = DeliveryTransportHub::new();
        let mut sub = hub.subscribe("alice");
        assert!(hub.is_connected("alice"));
        assert!(!hub.is_connected("bob"));

        let delivered = hub.push("alice", serde_json::json!({"reason": "poke_due"}));
        assert_eq!(delivered, 1);
        assert_eq!(
            sub.receiver.try_recv().unwrap(),
            serde_json::json!({"reason": "poke_due"})
        );

        // A push to a worker with no live stream delivers to nobody.
        assert_eq!(hub.push("bob", serde_json::json!({})), 0);
    }

    #[test]
    fn unsubscribe_is_idempotent_and_scoped_to_the_named_id() {
        let hub = DeliveryTransportHub::new();
        let a = hub.subscribe("alice");
        let _b = hub.subscribe("alice"); // a reconnect overlap
        assert_eq!(hub.push("alice", serde_json::json!({})), 2);

        hub.unsubscribe(a.id);
        assert!(
            hub.is_connected("alice"),
            "the second subscription is still live"
        );
        assert_eq!(hub.push("alice", serde_json::json!({})), 1);

        hub.unsubscribe(a.id); // already gone -- silent no-op
        assert!(hub.is_connected("alice"));
    }

    #[test]
    fn status_is_independent_of_connection_state() {
        let hub = DeliveryTransportHub::new();
        hub.set_status("alice", "working");
        assert_eq!(hub.get_status("alice").as_deref(), Some("working"));
        assert!(
            !hub.is_connected("alice"),
            "a status report is not a connection"
        );
    }

    #[test]
    fn snapshot_covers_the_union_of_connected_and_status_reported_agents() {
        let hub = DeliveryTransportHub::new();
        let _sub = hub.subscribe("alice"); // connected, no status
        hub.set_status("bob", "idle"); // status, not connected
        let snap = hub.snapshot();
        assert_eq!(snap.len(), 2);
        assert_eq!(snap[0]["agent_id"], "alice");
        assert_eq!(snap[0]["connected"], true);
        assert_eq!(snap[0]["streams"], 1);
        assert!(snap[0]["status"].is_null());
        assert_eq!(snap[1]["agent_id"], "bob");
        assert_eq!(snap[1]["connected"], false);
        assert_eq!(snap[1]["streams"], 0);
        assert_eq!(snap[1]["status"], "idle");
    }

    #[test]
    fn push_to_a_full_queue_drops_without_panicking() {
        let hub = DeliveryTransportHub::new();
        let sub = hub.subscribe("alice");
        for _ in 0..QUEUE_CAPACITY {
            hub.push("alice", serde_json::json!({}));
        }
        assert_eq!(hub.push("alice", serde_json::json!({})), 0);
        drop(sub);
    }
}
