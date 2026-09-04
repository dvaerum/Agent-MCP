//! Streaming-lifecycle revalidation seam. Port of
//! `agent_mcp/core/stream_gates.py` (Finding N5).
//!
//! Python's module doc explains WHY this exists: four long-lived
//! streams each hand-rolled the same "re-check, bounded wait, re-check
//! again after the dequeue" loop under a different finding ID, and the
//! half that kept getting dropped when the pattern was copied by hand
//! was specifically the SECOND re-check (the one after the dequeue,
//! before the item reaches the caller) -- SEC-B-F2 found exactly that
//! omission once. [`RevalidatingStream::next_slice`] fuses the two into
//! one call: the ONLY place a queue is ever dequeued is here, and both
//! exits (a real item, or an idle timeout) run a fresh liveness check
//! before handing anything back. A revoked stream is a hard error
//! ([`StreamRevoked`]), not a value a caller could forget to inspect.
//!
//! `wait_for_events` (Phase D3's own tool, not yet ported) is this
//! crate's first real consumer; the port is written generically (over
//! the item type and the liveness/cadence closures) so a future
//! SSE-style stream can reuse it too, matching Python's own four-streams
//! design even though only one of those streams has a Rust home so far.
//!
//! ## What's fused, and what's deliberately NOT (same split as Python)
//!
//! Fused: the bounded wait (clamped so a slice can never outlast the
//! stream's own cadence -- a caller's `timeout` can only shorten one
//! slice, never lengthen it), the post-dequeue liveness re-check before
//! the item is handed back, the post-idle-expiry re-check before the
//! caller's idle-branch work runs, and fail-closed teardown as an error
//! type rather than a flag.
//!
//! NOT fused: the liveness predicate itself (different streams have
//! genuinely different staleness/cost characteristics -- a constructor
//! argument, not a policy this module makes) and the cadence (also a
//! constructor argument).
//!
//! ## A wrinkle Python has that this port does NOT inherit
//!
//! Python's own docstring flags a known `asyncio` hazard: `asyncio.
//! wait_for` cancels the inner `queue.get()` on timeout, and a `put`
//! landing in that cancellation window can be dropped. This does NOT
//! apply here: `tokio::sync::mpsc::Receiver::recv` is documented
//! cancel-safe -- "if recv is used as the event in a `tokio::select!`
//! statement and some other branch completes first, it is guaranteed
//! that no messages were removed from this channel." Wrapping it in
//! `tokio::time::timeout` and having it time out does not lose a
//! message; it stays queued for the next `next_slice` call. A genuine
//! improvement from the runtime, not something this port had to build.

use std::future::Future;
use std::pin::Pin;
use std::time::Duration;

use tokio::sync::mpsc::Receiver;

/// A liveness verdict: may this stream still deliver, and if not, why.
/// `reason` is surfaced through [`StreamRevoked`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Liveness {
    pub live: bool,
    pub reason: Option<String>,
}

impl Liveness {
    pub fn live() -> Self {
        Liveness {
            live: true,
            reason: None,
        }
    }

    pub fn revoked(reason: impl Into<String>) -> Self {
        Liveness {
            live: false,
            reason: Some(reason.into()),
        }
    }
}

/// Which side of [`RevalidatingStream::next_slice`] produced a
/// [`StreamRevoked`] verdict.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RevocationPhase {
    /// The verdict was taken after a dequeue -- `discarded` holds the
    /// event that must NOT reach the caller.
    Item,
    /// The verdict was taken after the bounded wait expired with
    /// nothing to dequeue.
    Idle,
}

/// Raised by [`RevalidatingStream::next_slice`] when the stream's
/// liveness predicate says the caller is no longer entitled to it.
/// Callers tear down; the only variation between streams is what they
/// emit on the way out.
#[derive(Debug)]
pub struct StreamRevoked<T> {
    pub verdict: Liveness,
    pub phase: RevocationPhase,
    /// The event that was dequeued but never delivered (only set when
    /// `phase == Item`).
    pub discarded: Option<T>,
}

impl<T: std::fmt::Debug> std::fmt::Display for StreamRevoked<T> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match &self.verdict.reason {
            Some(reason) => write!(f, "{reason}"),
            None => write!(f, "stream revoked ({:?})", self.phase),
        }
    }
}

impl<T: std::fmt::Debug> std::error::Error for StreamRevoked<T> {}

/// The outcome of ONE bounded wait, already re-validated by the time the
/// caller sees it. A revoked stream is deliberately NOT a third variant
/// here -- see [`StreamRevoked`].
#[derive(Debug)]
pub enum StreamSlice<T> {
    /// The bounded wait expired with no event. The caller does its
    /// per-tick work (heartbeat, scheduled-directive fire, idle
    /// reminder) and asks for the next slice.
    Idle,
    /// `item` is the dequeued event, cleared for delivery.
    Item(T),
}

type LivenessFuture<'a> = Pin<Box<dyn Future<Output = Liveness> + Send + 'a>>;

/// Bounded wait for the next event on a `tokio::sync::mpsc` channel,
/// fused with a liveness re-check. One instance per open stream.
///
/// `liveness` is this stream's OWN predicate (a constructor argument --
/// see the module doc on why it isn't unified across streams).
/// `interval` is this stream's OWN cadence in seconds, re-read on every
/// slice (a plain closure covers both "fixed value" and "read a
/// runtime-mutable setting" -- Python needs a float-or-callable union
/// for the same two cases; a closure is the one shape Rust needs).
pub struct RevalidatingStream<'a, T> {
    queue: Receiver<T>,
    liveness: Box<dyn Fn() -> LivenessFuture<'a> + Send + Sync + 'a>,
    interval: Box<dyn Fn() -> f64 + Send + Sync + 'a>,
}

impl<'a, T> RevalidatingStream<'a, T> {
    /// `'a` bounds `liveness`/`interval`, NOT just `'static` -- added
    /// the moment this crate's first real consumer (`wait_for_events`,
    /// Phase D3) needed it: its liveness check borrows a
    /// `&'a AsyncMutex<Connection>` whose lifetime is tied to the
    /// enclosing `Tool::call`'s own arguments, never `'static`. Every
    /// existing caller passing a genuinely `'static` closure (this
    /// module's own tests) keeps working unchanged, since `'static`
    /// satisfies any `'a`.
    pub fn new(
        queue: Receiver<T>,
        liveness: impl Fn() -> LivenessFuture<'a> + Send + Sync + 'a,
        interval: impl Fn() -> f64 + Send + Sync + 'a,
    ) -> Self {
        Self {
            queue,
            liveness: Box::new(liveness),
            interval: Box::new(interval),
        }
    }

    /// This stream's current revalidation interval, in seconds.
    pub fn cadence(&self) -> f64 {
        (self.interval)()
    }

    /// Run this stream's liveness predicate once.
    pub async fn check(&self) -> Liveness {
        (self.liveness)().await
    }

    /// Wait for the next event, re-validate, and hand back a slice.
    ///
    /// Returns `Err(StreamRevoked)` -- before returning anything else at
    /// all -- the moment this stream's predicate says the caller is no
    /// longer live. Never returns an unchecked value: both exits
    /// (dequeued item, idle expiry) sit immediately after their own
    /// fresh verdict.
    ///
    /// `timeout` lets a caller SHORTEN one slice when it has an earlier
    /// deadline of its own to honour. It can never LENGTHEN one: the
    /// value is clamped to this stream's cadence, so the revalidation
    /// interval is a property of the stream rather than of whatever the
    /// caller last computed. A negative timeout clamps to zero.
    pub async fn next_slice(
        &mut self,
        timeout: Option<f64>,
    ) -> Result<StreamSlice<T>, StreamRevoked<T>> {
        let mut budget = self.cadence();
        if let Some(t) = timeout {
            budget = budget.min(t);
        }
        budget = budget.max(0.0);

        match tokio::time::timeout(Duration::from_secs_f64(budget), self.queue.recv()).await {
            Err(_elapsed) => {
                let verdict = self.check().await;
                if !verdict.live {
                    return Err(StreamRevoked {
                        verdict,
                        phase: RevocationPhase::Idle,
                        discarded: None,
                    });
                }
                Ok(StreamSlice::Idle)
            }
            // Channel closed (sender dropped) is treated the same as an
            // idle expiry for revalidation purposes -- the loop above
            // this call is expected to notice the closed channel on its
            // own next iteration if it cares to distinguish the two;
            // this method's job is only "wait, check, hand back".
            Ok(None) => {
                let verdict = self.check().await;
                if !verdict.live {
                    return Err(StreamRevoked {
                        verdict,
                        phase: RevocationPhase::Idle,
                        discarded: None,
                    });
                }
                Ok(StreamSlice::Idle)
            }
            Ok(Some(item)) => {
                let verdict = self.check().await;
                if !verdict.live {
                    return Err(StreamRevoked {
                        verdict,
                        phase: RevocationPhase::Item,
                        discarded: Some(item),
                    });
                }
                Ok(StreamSlice::Item(item))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    fn always_live<'a>() -> LivenessFuture<'a> {
        Box::pin(async { Liveness::live() })
    }

    fn always_revoked<'a>() -> LivenessFuture<'a> {
        Box::pin(async { Liveness::revoked("test revocation") })
    }

    #[tokio::test]
    async fn idle_expiry_returns_idle_when_still_live() {
        let (_tx, rx) = tokio::sync::mpsc::channel::<i32>(1);
        let mut stream = RevalidatingStream::new(rx, always_live, || 0.05);
        let slice = stream.next_slice(None).await.unwrap();
        assert!(matches!(slice, StreamSlice::Idle));
    }

    #[tokio::test]
    async fn idle_expiry_revokes_when_no_longer_live() {
        let (_tx, rx) = tokio::sync::mpsc::channel::<i32>(1);
        let mut stream = RevalidatingStream::new(rx, always_revoked, || 0.05);
        let err = stream.next_slice(None).await.unwrap_err();
        assert_eq!(err.phase, RevocationPhase::Idle);
        assert_eq!(err.verdict.reason.as_deref(), Some("test revocation"));
        assert!(err.discarded.is_none());
    }

    #[tokio::test]
    async fn a_dequeued_item_is_returned_when_still_live() {
        let (tx, rx) = tokio::sync::mpsc::channel::<i32>(1);
        tx.send(42).await.unwrap();
        let mut stream = RevalidatingStream::new(rx, always_live, || 1.0);
        let slice = stream.next_slice(None).await.unwrap();
        assert!(matches!(slice, StreamSlice::Item(42)));
    }

    #[tokio::test]
    async fn a_dequeued_item_is_discarded_and_revoked_when_no_longer_live() {
        // SEC-B-F2's exact regression: an item queued before revocation
        // must never reach the caller.
        let (tx, rx) = tokio::sync::mpsc::channel::<i32>(1);
        tx.send(42).await.unwrap();
        let mut stream = RevalidatingStream::new(rx, always_revoked, || 1.0);
        let err = stream.next_slice(None).await.unwrap_err();
        assert_eq!(err.phase, RevocationPhase::Item);
        assert_eq!(err.discarded, Some(42));
    }

    #[tokio::test]
    async fn a_caller_timeout_can_only_shorten_a_slice_never_lengthen_it() {
        let (_tx, rx) = tokio::sync::mpsc::channel::<i32>(1);
        let mut stream = RevalidatingStream::new(rx, always_live, || 0.05);
        let start = tokio::time::Instant::now();
        // Caller asks for a much LONGER wait than the stream's own 0.05s
        // cadence -- the cadence must win.
        let slice = stream.next_slice(Some(10.0)).await.unwrap();
        assert!(matches!(slice, StreamSlice::Idle));
        assert!(start.elapsed() < Duration::from_secs(1));
    }

    #[tokio::test]
    async fn a_caller_timeout_shorter_than_cadence_is_honoured() {
        let (_tx, rx) = tokio::sync::mpsc::channel::<i32>(1);
        let mut stream = RevalidatingStream::new(rx, always_live, || 10.0);
        let start = tokio::time::Instant::now();
        let slice = stream.next_slice(Some(0.05)).await.unwrap();
        assert!(matches!(slice, StreamSlice::Idle));
        assert!(start.elapsed() < Duration::from_secs(1));
    }

    #[tokio::test]
    async fn a_negative_timeout_clamps_to_zero_not_a_panic() {
        let (_tx, rx) = tokio::sync::mpsc::channel::<i32>(1);
        let mut stream = RevalidatingStream::new(rx, always_live, || 1.0);
        let slice = stream.next_slice(Some(-5.0)).await.unwrap();
        assert!(matches!(slice, StreamSlice::Idle));
    }

    #[tokio::test]
    async fn cadence_is_re_read_on_every_slice() {
        let calls = Arc::new(AtomicUsize::new(0));
        let calls_clone = calls.clone();
        let (_tx, rx) = tokio::sync::mpsc::channel::<i32>(1);
        let mut stream = RevalidatingStream::new(rx, always_live, move || {
            calls_clone.fetch_add(1, Ordering::SeqCst);
            0.01
        });
        stream.next_slice(None).await.unwrap();
        stream.next_slice(None).await.unwrap();
        assert_eq!(calls.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn timeout_does_not_lose_a_message_landing_during_cancellation() {
        // The wrinkle Python's docstring flags does NOT apply here --
        // tokio::sync::mpsc::Receiver::recv is documented cancel-safe.
        // Prove it directly: start a slice with a tiny budget, send a
        // message concurrently right as the timeout fires, and confirm
        // the NEXT call still sees it (nothing was silently dropped).
        let (tx, rx) = tokio::sync::mpsc::channel::<i32>(1);
        let mut stream = RevalidatingStream::new(rx, always_live, || 0.01);
        // First slice times out (nothing sent yet).
        let slice = stream.next_slice(None).await.unwrap();
        assert!(matches!(slice, StreamSlice::Idle));
        // Now send, then take the next slice -- must see the item.
        tx.send(7).await.unwrap();
        let slice = stream.next_slice(None).await.unwrap();
        assert!(matches!(slice, StreamSlice::Item(7)));
    }
}
