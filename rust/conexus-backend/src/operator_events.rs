//! In-process pub/sub hub for operator dashboard SSE subscribers.
//! Port of `agent_mcp/features/operator_events.py`.
//!
//! Operators are NOT agents -- the agent-scoped `WaiterRegistry`/
//! `session_registry` machinery keys on `agent_id`, which a dashboard
//! user has none of. This hub is the operator-side equivalent: a
//! dependency-light, in-memory-only fan-out `GET /api/events`
//! subscribes to and every mutation publishes onto.
//!
//! Delivery model -- deliberately fire-and-forget, matching Python
//! exactly: events are idempotent "something changed, refetch" HINTS,
//! not exactly-once messages. A hint dropped (bounded queue full) or
//! missed (published while no stream was open) is reconciled by the
//! client's own reconnect catch-up refetch and slow-poll backstop --
//! there is no per-subscriber delivery ledger to build.
//!
//! Runtime-only on purpose -- a backend restart drops every subscriber
//! and the browser's SSE reconnect rebuilds from scratch; nothing here
//! is worth persisting.
//!
//! A deliberate, documented departure from Python's `List<Subscriber>`
//! plus `list.remove()`-by-identity shape: this port keys each
//! subscriber by a monotonic `u64` id instead. Rust values don't have
//! Python's `is`-identity comparison, and `Subscriber` here holds no
//! `PartialEq` impl worth deriving just to satisfy `Vec::remove` -- an
//! integer id is the simplest thing that gives `unsubscribe` the same
//! "drop exactly this one, unknown id is a no-op" guarantee.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use serde_json::Value;
use tokio::sync::mpsc;

/// Bound each subscriber queue -- a subscriber more than this many
/// notifications behind gets dropped payloads (logged), never a
/// blocked publisher. Matches Python's `_QUEUE_MAXSIZE`.
const QUEUE_CAPACITY: usize = 256;

struct SubscriberRecord {
    id: u64,
    user_id: Option<String>,
    /// ISO-8601 UTC wall clock, for the status snapshot -- observability
    /// metadata only, never a stored/replayed value, so reading it once
    /// at subscribe time (an explicit `now` the caller supplies, this
    /// crate's usual convention) is enough.
    connected_at: String,
    sender: mpsc::Sender<Value>,
}

/// A live subscription handle. `id` is threaded back through to
/// [`OperatorEventsHub::unsubscribe`] by the caller (`GET /api/events`'s
/// handler) -- see that handler's own `Drop` guard for why cleanup is
/// RAII rather than a hand-written `finally`.
pub struct Subscription {
    pub id: u64,
    pub receiver: mpsc::Receiver<Value>,
}

/// The subscriber set. `Mutex`, not `RwLock` -- every operation
/// (subscribe/unsubscribe/publish/snapshot) either mutates or needs a
/// consistent point-in-time read, so a plain mutex is the right tool
/// (matching `WaiterRegistry`/`FileMap`'s own precedent).
#[derive(Default)]
pub struct OperatorEventsHub {
    subscribers: Mutex<Vec<SubscriberRecord>>,
    next_id: AtomicU64,
}

impl OperatorEventsHub {
    pub fn new() -> Self {
        Self {
            subscribers: Mutex::new(Vec::new()),
            next_id: AtomicU64::new(1),
        }
    }

    /// Register a new operator SSE subscriber. `GET /api/events`'s
    /// handler calls this on connect and drains the returned receiver
    /// onto the wire.
    pub fn subscribe(&self, user_id: Option<String>, now: &str) -> Subscription {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let (sender, receiver) = mpsc::channel(QUEUE_CAPACITY);
        let mut subs = self
            .subscribers
            .lock()
            .expect("operator_events mutex poisoned");
        subs.push(SubscriberRecord {
            id,
            user_id,
            connected_at: now.to_string(),
            sender,
        });
        Subscription { id, receiver }
    }

    /// Drop `id` from the subscriber set. Idempotent -- an id that was
    /// already removed (or never registered) is a silent no-op.
    pub fn unsubscribe(&self, id: u64) {
        let mut subs = self
            .subscribers
            .lock()
            .expect("operator_events mutex poisoned");
        subs.retain(|s| s.id != id);
    }

    /// Fan `payload` out to every current subscriber. Never panics or
    /// propagates an error -- a subscriber whose bounded queue is full
    /// gets the payload dropped (mirrors Python's `QueueFull` handling);
    /// a subscriber whose receiver has already been dropped (its
    /// `Subscription` went out of scope, `unsubscribe` just hasn't run
    /// yet) is silently skipped the same way Python's bare `except
    /// Exception: pass` skips it -- telemetry-grade delivery must never
    /// disrupt the mutation that published it.
    pub fn publish(&self, payload: Value) {
        let subs = self
            .subscribers
            .lock()
            .expect("operator_events mutex poisoned");
        for sub in subs.iter() {
            let _ = sub.sender.try_send(payload.clone());
        }
    }

    /// Number of live subscribers -- `GET /api/events/status`'s
    /// `connected` field.
    pub fn subscriber_count(&self) -> usize {
        self.subscribers
            .lock()
            .expect("operator_events mutex poisoned")
            .len()
    }

    /// One row per live subscriber for `GET /api/events/status`:
    /// `{user_id, connected_at, age_seconds, queue_depth}`. `now` is
    /// the CURRENT wall clock (unlike every DB-write timestamp
    /// elsewhere in this crate, this is a live status read, not
    /// persisted data -- there is nothing to replay or test
    /// deterministically against, matching Python's own
    /// `datetime.datetime.now()` call directly inside `snapshot()`).
    pub fn snapshot(&self, now: chrono::DateTime<chrono::Utc>) -> Vec<Value> {
        let subs = self
            .subscribers
            .lock()
            .expect("operator_events mutex poisoned");
        subs.iter()
            .map(|s| {
                let age_seconds = chrono::DateTime::parse_from_rfc3339(&s.connected_at)
                    .ok()
                    .map(|connected| {
                        let secs = (now - connected.with_timezone(&chrono::Utc)).num_milliseconds()
                            as f64
                            / 1000.0;
                        (secs * 10.0).round() / 10.0
                    });
                serde_json::json!({
                    "user_id": s.user_id,
                    "connected_at": s.connected_at,
                    "age_seconds": age_seconds,
                    "queue_depth": QUEUE_CAPACITY - s.sender.capacity(),
                })
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn subscribe_registers_and_snapshot_reflects_it() {
        let hub = OperatorEventsHub::new();
        let now = "2026-01-01T00:00:00+00:00";
        let sub = hub.subscribe(Some("alice".to_string()), now);
        assert_eq!(hub.subscriber_count(), 1);
        let snap = hub.snapshot(chrono::Utc::now());
        assert_eq!(snap.len(), 1);
        assert_eq!(snap[0]["user_id"], "alice");
        assert_eq!(snap[0]["connected_at"], now);
        assert_eq!(snap[0]["queue_depth"], 0);
        drop(sub);
    }

    #[test]
    fn unsubscribe_is_idempotent_and_drops_exactly_the_named_id() {
        let hub = OperatorEventsHub::new();
        let now = "2026-01-01T00:00:00+00:00";
        let a = hub.subscribe(Some("a".to_string()), now);
        let b = hub.subscribe(Some("b".to_string()), now);
        assert_eq!(hub.subscriber_count(), 2);

        hub.unsubscribe(a.id);
        assert_eq!(hub.subscriber_count(), 1);
        let snap = hub.snapshot(chrono::Utc::now());
        assert_eq!(snap[0]["user_id"], "b");

        // Re-unsubscribing an already-gone id is a silent no-op.
        hub.unsubscribe(a.id);
        assert_eq!(hub.subscriber_count(), 1);
        drop(b);
    }

    #[test]
    fn publish_fans_out_to_every_subscriber() {
        let hub = OperatorEventsHub::new();
        let now = "2026-01-01T00:00:00+00:00";
        let mut a = hub.subscribe(None, now);
        let mut b = hub.subscribe(None, now);

        hub.publish(serde_json::json!({"hello": "world"}));

        assert_eq!(
            a.receiver.try_recv().unwrap(),
            serde_json::json!({"hello": "world"})
        );
        assert_eq!(
            b.receiver.try_recv().unwrap(),
            serde_json::json!({"hello": "world"})
        );
    }

    #[test]
    fn publish_to_a_full_queue_drops_the_payload_without_panicking() {
        let hub = OperatorEventsHub::new();
        let sub = hub.subscribe(None, "2026-01-01T00:00:00+00:00");
        for i in 0..QUEUE_CAPACITY {
            hub.publish(serde_json::json!({"i": i}));
        }
        // The queue is now full; one more publish must be silently
        // dropped, not panic or block.
        hub.publish(serde_json::json!({"i": "overflow"}));
        let snap = hub.snapshot(chrono::Utc::now());
        assert_eq!(snap[0]["queue_depth"], QUEUE_CAPACITY);
        drop(sub);
    }

    #[test]
    fn publish_to_a_dropped_receiver_does_not_panic() {
        let hub = OperatorEventsHub::new();
        let sub = hub.subscribe(None, "2026-01-01T00:00:00+00:00");
        drop(sub); // receiver gone, but unsubscribe() was never called
        hub.publish(serde_json::json!({"hello": "world"}));
        assert_eq!(
            hub.subscriber_count(),
            1,
            "still registered until unsubscribe runs"
        );
    }
}
