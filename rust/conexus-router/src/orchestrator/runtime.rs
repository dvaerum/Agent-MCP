//! Per-project runtime bookkeeping -- port of `project_orchestrator.py`'s
//! `ProjectRuntime` dataclass + the module-level `runtime: dict[str,
//! ProjectRuntime]` + `_rt`/`_gc`/`forget`/`_ensure_lock` (lines
//! 88-204, 584-590). Pure in-memory state, zero I/O, zero subprocess
//! -- everything here is a candidate for the `test_project_runtime_
//! forget.py` acceptance test ported almost verbatim below.
//!
//! **Concurrency, NOT a literal port**: Python's `runtime` dict and
//! its `ensure_locks` field are plain unguarded structures, safe only
//! because `asyncio`'s single-threaded cooperative scheduler never
//! preempts between a dict read and a dict write with no `await` in
//! between. A dedicated research pass flagged this explicitly (the
//! module's OWN `_streaming_proxies_lock` comment calls the identical
//! pattern elsewhere in that file "an UNENFORCED invariant... an
//! INCIDENTAL guarantee, not a structural one") -- under `tokio`'s
//! real multi-threaded runtime this needs a genuine lock, not a
//! literal `HashMap` port. [`RuntimeStore`] wraps the data map in a
//! `std::sync::Mutex` (never held across an `.await` -- every mutation
//! here is synchronous, matching Python's own no-await-inside-the-
//! critical-section design) and keeps the per-`(name, role)` ensure
//! lock in a SEPARATE `dashmap::DashMap` using `tokio::sync::Mutex`
//! (which DOES get held across `.await` points once `ensure()` lands
//! in PR 6c) with `DashMap::entry(...).or_insert_with(...)` for the
//! get-or-create step -- Python's lazy `if lock is None: lock =
//! asyncio.Lock()` is a genuine TOCTOU once creation itself isn't
//! serialized by a single event loop; `DashMap::entry` is atomic
//! w.r.t. that creation.
//!
//! **Deliberate structural divergence, not a silent gap**: Python's
//! `ProjectRuntime.is_empty()` includes `ensure_locks` in its
//! zero-value check, so `_gc()` never drops a row while an
//! outstanding lock object exists for it. Splitting the lock registry
//! out of the data map (above) means this crate's own GC-on-mutation
//! (`RuntimeStore::with_runtime_mut`) can't see lock state and won't
//! preserve that coupling -- but nothing observable depends on it:
//! the lock itself is a `DashMap` entry with its own independent
//! lifetime (any in-flight or future caller reaching
//! [`RuntimeStore::ensure_lock`] still finds the SAME lock instance
//! regardless of whether the data row was GC'd), so serialization
//! correctness is unaffected. Documented here rather than silently
//! diverging.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Instant, SystemTime};

use dashmap::DashMap;
use tokio::sync::Mutex as AsyncMutex;

/// The two fixed generic failure reasons `ensure()` (PR 6c) ever
/// caches -- kept HERE rather than in the `ensure` module since it's
/// stored directly in [`ProjectRuntime::ensure_failures`], and this
/// module shouldn't need to import the state-machine module just to
/// know its own field's value type.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EnsureFailureReason {
    /// SC-R8-2: the `systemctl start`/`restart` shell-out failed.
    SystemctlFailed,
    /// SC-R9-1: the unit came up but its socket never appeared within
    /// the poll budget.
    SocketTimeout,
}

impl EnsureFailureReason {
    /// The fixed, generic client-facing message -- SC-R8-2/SC-R9-1:
    /// never reflects the real systemctl stderr or the absolute
    /// on-disk socket path back to a caller, regardless of how
    /// detailed the server-side log for the same failure is.
    pub fn message(&self) -> &'static str {
        match self {
            EnsureFailureReason::SystemctlFailed => "backend failed to start",
            EnsureFailureReason::SocketTimeout => "backend not ready",
        }
    }
}

/// One project's runtime bookkeeping -- mirrors Python's
/// `ProjectRuntime` dataclass minus `ensure_locks` (see module doc for
/// why that field lives in [`RuntimeStore`] instead). Every field here
/// is role-keyed (role is always `"backend"` today; kept role-generic
/// to match Python's own forward-looking shape).
#[derive(Debug, Clone, Default)]
pub struct ProjectRuntime {
    /// Wall-clock last-activity timestamp per role -- Python's
    /// `time.time()` (NOT monotonic): the reaper compares this
    /// against `IDLE_SEC` as a real-world elapsed duration.
    pub last_active: HashMap<String, SystemTime>,
    pub active_conns: u32,
    /// Monotonic per role -- Python's `time.monotonic()`: robust to a
    /// system-clock adjustment mid-uptime, appropriate since these are
    /// only ever compared against another `Instant` captured within
    /// the same process lifetime.
    pub unit_start_times: HashMap<String, Instant>,
    pub ensure_failures: HashMap<String, (Instant, EnsureFailureReason)>,
    pub forwarding_hmac_key: Option<Vec<u8>>,
    pub warm_inflight: bool,
}

impl ProjectRuntime {
    /// True iff every DATA field is at its zero value (see the module
    /// doc for why this deliberately excludes lock state, unlike
    /// Python's own `is_empty()`).
    pub fn is_empty(&self) -> bool {
        self.last_active.is_empty()
            && self.active_conns == 0
            && self.unit_start_times.is_empty()
            && self.ensure_failures.is_empty()
            && self.forwarding_hmac_key.is_none()
            && !self.warm_inflight
    }
}

/// The one source of truth for per-project mutable runtime state --
/// port of the module-level `runtime` dict + `_ensure_lock`'s lock
/// map, unified behind one owning type instead of a bare global
/// (this crate has no module-level mutable globals anywhere else;
/// the eventual router binary owns one `RuntimeStore` and threads it
/// explicitly, matching every other piece of shared state in this
/// workspace -- `WaiterRegistry`, `FileMap`, `DeliveryTransportHub`).
pub struct RuntimeStore {
    data: Mutex<HashMap<String, ProjectRuntime>>,
    ensure_locks: DashMap<(String, String), Arc<AsyncMutex<()>>>,
}

impl Default for RuntimeStore {
    fn default() -> Self {
        Self::new()
    }
}

impl RuntimeStore {
    pub fn new() -> Self {
        Self {
            data: Mutex::new(HashMap::new()),
            ensure_locks: DashMap::new(),
        }
    }

    /// Run `f` against project `name`'s runtime row, creating an
    /// empty one first if absent (port of `_rt`), then drop the row
    /// if it's back to empty afterward (port of `_gc`). Folding the GC
    /// step into every mutation -- rather than requiring each caller
    /// to remember it, as Python's separate `_rt`/`_gc` call sites do
    /// -- removes a whole "forgot to GC" bug class a literal port
    /// would risk reintroducing at each new call site added in a
    /// later PR (`ensure`/`reaper_tick`/`_track_connection`).
    pub fn with_runtime_mut<R>(&self, name: &str, f: impl FnOnce(&mut ProjectRuntime) -> R) -> R {
        let mut data = self.data.lock().expect("runtime store mutex poisoned");
        let rt = data.entry(name.to_string()).or_default();
        let result = f(rt);
        if data.get(name).is_some_and(ProjectRuntime::is_empty) {
            data.remove(name);
        }
        result
    }

    /// A snapshot copy of `name`'s runtime row, or `None` if absent
    /// (never creates one -- unlike `with_runtime_mut`, a read must
    /// not conjure a row into existence just to answer "is anything
    /// here?").
    pub fn snapshot(&self, name: &str) -> Option<ProjectRuntime> {
        self.data
            .lock()
            .expect("runtime store mutex poisoned")
            .get(name)
            .cloned()
    }

    /// A snapshot copy of every tracked project's runtime row --
    /// backs `list_active`-shaped reads in a later PR.
    pub fn snapshot_all(&self) -> HashMap<String, ProjectRuntime> {
        self.data
            .lock()
            .expect("runtime store mutex poisoned")
            .clone()
    }

    /// Get-or-create the per-`(name, role)` ensure lock -- port of
    /// `_ensure_lock`. `DashMap::entry` makes the get-or-create step
    /// itself atomic (see the module doc for why Python's equivalent
    /// lazy-creation is a genuine TOCTOU once ported to a real
    /// multi-threaded runtime).
    pub fn ensure_lock(&self, name: &str, role: &str) -> Arc<AsyncMutex<()>> {
        self.ensure_locks
            .entry((name.to_string(), role.to_string()))
            .or_insert_with(|| Arc::new(AsyncMutex::new(())))
            .clone()
    }

    /// The single clear-on-lifecycle-end path -- port of `forget()`.
    /// Clears `last_active`/`active_conns`/`unit_start_times`/
    /// `ensure_failures`/`warm_inflight` unconditionally; clears
    /// `forwarding_hmac_key` unless `keep_hmac` (F015 v4: the on-disk
    /// key file survives a stop/restart, so the in-memory cache can
    /// too); clears every ensure lock for `name` (any role) unless
    /// `keep_lock`.
    pub fn forget(&self, name: &str, keep_hmac: bool, keep_lock: bool) {
        self.with_runtime_mut(name, |rt| {
            rt.last_active.clear();
            rt.active_conns = 0;
            rt.unit_start_times.clear();
            rt.ensure_failures.clear();
            rt.warm_inflight = false;
            if !keep_hmac {
                rt.forwarding_hmac_key = None;
            }
        });
        if !keep_lock {
            self.ensure_locks.retain(|(n, _role), _| n != name);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn seed_every_field(store: &RuntimeStore, name: &str) {
        store.with_runtime_mut(name, |rt| {
            rt.last_active.insert("backend".into(), SystemTime::now());
            rt.active_conns = 3;
            rt.unit_start_times.insert("backend".into(), Instant::now());
            rt.ensure_failures.insert(
                "backend".into(),
                (Instant::now(), EnsureFailureReason::SystemctlFailed),
            );
            rt.forwarding_hmac_key = Some(vec![1, 2, 3]);
            rt.warm_inflight = true;
        });
        // Also seed the ensure lock -- forget's own `keep_lock`
        // behavior spans BOTH structures.
        store.ensure_lock(name, "backend");
    }

    #[test]
    fn forget_clears_every_field() {
        let store = RuntimeStore::new();
        seed_every_field(&store, "proj-a");

        store.forget("proj-a", false, false);

        assert!(
            store.snapshot("proj-a").is_none(),
            "a fully-cleared row (locks also dropped) must be GC'd away entirely"
        );
        assert!(store.ensure_locks.is_empty());
    }

    #[test]
    fn forget_keep_hmac_retains_only_the_hmac_key() {
        let store = RuntimeStore::new();
        seed_every_field(&store, "proj-a");

        store.forget("proj-a", true, false);

        let rt = store
            .snapshot("proj-a")
            .expect("a row with a retained hmac key must survive GC");
        assert_eq!(rt.forwarding_hmac_key, Some(vec![1, 2, 3]));
        assert!(rt.last_active.is_empty());
        assert_eq!(rt.active_conns, 0);
        assert!(rt.unit_start_times.is_empty());
        assert!(rt.ensure_failures.is_empty());
        assert!(!rt.warm_inflight);
        assert!(
            store.ensure_locks.is_empty(),
            "keep_hmac alone must not also retain the lock"
        );
    }

    #[test]
    fn forget_keep_lock_retains_only_the_lock() {
        let store = RuntimeStore::new();
        seed_every_field(&store, "proj-a");

        store.forget("proj-a", false, true);

        assert!(
            store.snapshot("proj-a").is_none(),
            "the data row is still fully cleared and GC'd"
        );
        assert!(
            !store.ensure_locks.is_empty(),
            "keep_lock alone must retain the lock even though the data row is gone"
        );
    }

    #[test]
    fn forget_is_scoped_to_the_named_project_only() {
        let store = RuntimeStore::new();
        seed_every_field(&store, "proj-a");
        seed_every_field(&store, "proj-b");

        store.forget("proj-a", false, false);

        assert!(store.snapshot("proj-a").is_none());
        assert!(
            store.snapshot("proj-b").is_some(),
            "an unrelated project's runtime must be untouched"
        );
    }

    #[test]
    fn forget_of_an_unseen_project_is_a_noop() {
        let store = RuntimeStore::new();
        // Must not panic.
        store.forget("nope", false, false);
        assert!(store.snapshot("nope").is_none());
    }

    #[test]
    fn with_runtime_mut_creates_a_row_lazily_and_gcs_it_away_when_empty_again() {
        let store = RuntimeStore::new();
        assert!(store.snapshot("proj-a").is_none());

        store.with_runtime_mut("proj-a", |rt| rt.active_conns = 1);
        assert!(store.snapshot("proj-a").is_some());

        store.with_runtime_mut("proj-a", |rt| rt.active_conns = 0);
        assert!(
            store.snapshot("proj-a").is_none(),
            "a row back to all-zero fields must be GC'd automatically"
        );
    }

    #[test]
    fn ensure_lock_returns_the_same_instance_for_the_same_key() {
        let store = RuntimeStore::new();
        let a = store.ensure_lock("proj-a", "backend");
        let b = store.ensure_lock("proj-a", "backend");
        assert!(
            Arc::ptr_eq(&a, &b),
            "repeated calls must return the SAME lock instance"
        );
    }

    #[test]
    fn ensure_lock_is_distinct_per_project_and_per_role() {
        let store = RuntimeStore::new();
        let a = store.ensure_lock("proj-a", "backend");
        let b = store.ensure_lock("proj-b", "backend");
        let c = store.ensure_lock("proj-a", "other-role");
        assert!(!Arc::ptr_eq(&a, &b));
        assert!(!Arc::ptr_eq(&a, &c));
    }

    #[test]
    fn ensure_lock_actually_serializes_concurrent_holders() {
        // A real cross-task proof, not just "the type compiles": one
        // task holds the lock across a real await; a second task
        // racing for the SAME (name, role) must observe it as busy.
        let store = std::sync::Arc::new(RuntimeStore::new());
        let store2 = store.clone();

        tokio_test_block_on(async move {
            let lock = store.ensure_lock("proj-a", "backend");
            let _guard = lock.lock().await;

            let lock2 = store2.ensure_lock("proj-a", "backend");
            assert!(
                lock2.try_lock().is_err(),
                "a second caller for the same (name, role) must find the lock held"
            );
        });
    }

    /// Minimal same-thread async-block runner -- this module doesn't
    /// need a full `#[tokio::test]` runtime elsewhere, so pulling in
    /// `tokio`'s `rt`/`macros` test features just for one
    /// concurrency-proof test isn't worth it; `futures`-free single
    /// poll-to-completion is sufficient since neither task here
    /// actually yields to an I/O reactor.
    fn tokio_test_block_on<F: std::future::Future>(fut: F) -> F::Output {
        tokio::runtime::Builder::new_current_thread()
            .build()
            .expect("build a current-thread tokio runtime for a test")
            .block_on(fut)
    }
}
