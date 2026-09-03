//! `StableOrderCache` — a generic, DB-agnostic keyed cache of an
//! ordered id sequence, letting a multi-page sweep (`offset=0`, then
//! `offset=N`, `offset=2N`, ...) see a CONSISTENT ordering even if
//! rows are mutated between page requests.
//!
//! Port of `agent_mcp/utils/pagination_cache.py`'s `StableOrderCache`.
//! Used identically (in Python) by `AgentRepository`,
//! `MessageRepository`, and `TaskQueryEngine`'s `view_tasks`
//! pagination — each owns its OWN instance (this is why it lives here
//! as a shared, reusable primitive rather than being duplicated per
//! repository, but there is deliberately no single process-wide
//! singleton: a global cache would leak stale orderings across
//! unrelated call sites, and make test isolation impossible without a
//! `clear()`-everywhere convention).
//!
//! Caches ONLY the ordered id sequence — never row data, never a
//! count. Reconciling a stale id against "does this row still exist"
//! is the caller's job (see `AgentRepository::query`), matching the
//! Python source's separation of concerns exactly.

use std::collections::HashMap;
use std::hash::Hash;
use std::sync::Mutex;
use std::time::{Duration, Instant};

pub struct StableOrderCache<K, T> {
    store: Mutex<HashMap<K, (Instant, Vec<T>)>>,
    ttl: Duration,
    max_entries: usize,
}

impl<K: Eq + Hash + Clone, T: Clone> StableOrderCache<K, T> {
    pub fn new(ttl_seconds: f64, max_entries: usize) -> Self {
        Self {
            store: Mutex::new(HashMap::new()),
            ttl: Duration::from_secs_f64(ttl_seconds),
            max_entries,
        }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, HashMap<K, (Instant, Vec<T>)>> {
        self.store
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn replay_or_none(&self, key: &K) -> Option<Vec<T>> {
        let mut store = self.lock();
        match store.get(key) {
            Some((anchored_at, ids)) if anchored_at.elapsed() < self.ttl => Some(ids.clone()),
            Some(_) => {
                store.remove(key); // expired — evict on read, matching Python
                None
            }
            None => None,
        }
    }

    /// Unconditionally (re)populate `key`. Called on every sweep
    /// restart (`offset == 0`) and as the fallback anchor on any
    /// `offset > 0` cache miss.
    fn anchor(&self, key: K, ids: Vec<T>) {
        let mut store = self.lock();
        store.retain(|_, (anchored_at, _)| anchored_at.elapsed() < self.ttl);
        if !store.contains_key(&key) && store.len() >= self.max_entries {
            // Simple oldest-by-timestamp eviction (not a proper LRU
            // list) — matches Python: an O(n) scan is fine at this
            // cache's intended scale (a handful of filter shapes per
            // repository, not a hot per-request cache).
            if let Some(oldest) = store
                .iter()
                .min_by_key(|(_, (t, _))| *t)
                .map(|(k, _)| k.clone())
            {
                store.remove(&oldest);
            }
        }
        store.insert(key, (Instant::now(), ids));
    }

    /// The single entry point callers use.
    ///
    /// * `offset == 0` — ALWAYS calls `compute`, then anchors the
    ///   result. A fresh sweep never replays a stale ordering.
    /// * `offset > 0` — replays the anchored ordering from this
    ///   sweep's `offset == 0` call if present; on a miss (expired,
    ///   evicted, or the caller jumped straight to a mid-sweep
    ///   offset), falls back to `compute` and anchors it, gaining
    ///   consistency for pages after this one (not before).
    ///
    /// Generic over `compute`'s error type so a fallible DB read can
    /// propagate a real error without this cache needing to know
    /// anything about `rusqlite`.
    pub fn get_or_anchor<E, F: FnOnce() -> Result<Vec<T>, E>>(
        &self,
        key: K,
        offset: i64,
        compute: F,
    ) -> Result<Vec<T>, E> {
        if offset == 0 {
            let ids = compute()?;
            self.anchor(key, ids.clone());
            return Ok(ids);
        }
        if let Some(ids) = self.replay_or_none(&key) {
            return Ok(ids);
        }
        let ids = compute()?;
        self.anchor(key, ids.clone());
        Ok(ids)
    }

    /// Drop every entry. Test-isolation hook, matching
    /// `AgentRepository._pagination_cache.clear()` in Python's
    /// `conftest.py`.
    pub fn clear(&self) {
        self.lock().clear();
    }
}

impl<K: Eq + Hash + Clone, T: Clone> Default for StableOrderCache<K, T> {
    /// Matches Python's defaults: `ttl_seconds=60.0`, `max_entries=512`.
    fn default() -> Self {
        Self::new(60.0, 512)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::thread::sleep;

    fn ok(ids: &[&str]) -> Result<Vec<String>, std::convert::Infallible> {
        Ok(ids.iter().map(|s| s.to_string()).collect())
    }

    #[test]
    fn offset_zero_always_recomputes_even_with_a_live_anchor() {
        let cache: StableOrderCache<&str, String> = StableOrderCache::default();
        let first = cache.get_or_anchor("k", 0, || ok(&["a", "b"])).unwrap();
        assert_eq!(first, vec!["a", "b"]);

        let second = cache
            .get_or_anchor("k", 0, || ok(&["x", "y", "z"]))
            .unwrap();
        assert_eq!(
            second,
            vec!["x", "y", "z"],
            "offset=0 must never replay a stale anchor"
        );
    }

    #[test]
    fn offset_positive_replays_the_anchor_instead_of_recomputing() {
        let cache: StableOrderCache<&str, String> = StableOrderCache::default();
        cache
            .get_or_anchor("k", 0, || ok(&["a", "b", "c"]))
            .unwrap();

        // A later call with a DIFFERENT compute must be ignored —
        // proves the anchored ordering, not a fresh one, is returned.
        let replayed = cache
            .get_or_anchor("k", 2, || ok(&["should", "not", "run"]))
            .unwrap();
        assert_eq!(replayed, vec!["a", "b", "c"]);
    }

    #[test]
    fn offset_positive_with_no_anchor_computes_and_anchors() {
        let cache: StableOrderCache<&str, String> = StableOrderCache::default();
        let result = cache.get_or_anchor("k", 5, || ok(&["fresh"])).unwrap();
        assert_eq!(result, vec!["fresh"]);

        // Subsequent offset>0 calls now replay THIS anchor.
        let replayed = cache.get_or_anchor("k", 10, || ok(&["different"])).unwrap();
        assert_eq!(replayed, vec!["fresh"]);
    }

    #[test]
    fn distinct_keys_are_independent() {
        let cache: StableOrderCache<&str, String> = StableOrderCache::default();
        cache.get_or_anchor("shape-a", 0, || ok(&["a1"])).unwrap();
        cache.get_or_anchor("shape-b", 0, || ok(&["b1"])).unwrap();

        assert_eq!(
            cache.get_or_anchor("shape-a", 1, || ok(&["nope"])).unwrap(),
            vec!["a1"]
        );
        assert_eq!(
            cache.get_or_anchor("shape-b", 1, || ok(&["nope"])).unwrap(),
            vec!["b1"]
        );
    }

    #[test]
    fn expired_anchor_is_not_replayed() {
        let cache: StableOrderCache<&str, String> = StableOrderCache::new(0.05, 512);
        cache.get_or_anchor("k", 0, || ok(&["stale"])).unwrap();
        sleep(Duration::from_millis(150));

        let result = cache
            .get_or_anchor("k", 1, || ok(&["fresh-after-expiry"]))
            .unwrap();
        assert_eq!(result, vec!["fresh-after-expiry"]);
    }

    #[test]
    fn eviction_drops_the_oldest_entry_when_at_capacity() {
        let cache: StableOrderCache<&str, String> = StableOrderCache::new(60.0, 2);
        cache.get_or_anchor("k1", 0, || ok(&["v1"])).unwrap();
        sleep(Duration::from_millis(5));
        cache.get_or_anchor("k2", 0, || ok(&["v2"])).unwrap();
        sleep(Duration::from_millis(5));
        // Inserting a 3rd distinct key at capacity 2 evicts k1 (oldest).
        cache.get_or_anchor("k3", 0, || ok(&["v3"])).unwrap();

        // k2/k3 survive -- checked BEFORE touching k1 again: probing
        // an evicted key at offset>0 recomputes and re-anchors it,
        // which at full capacity evicts the (now oldest) survivor as
        // a side effect, so touch order matters for this assertion.
        assert_eq!(
            cache.get_or_anchor("k2", 1, || ok(&["nope"])).unwrap(),
            vec!["v2"]
        );
        assert_eq!(
            cache.get_or_anchor("k3", 1, || ok(&["nope"])).unwrap(),
            vec!["v3"]
        );

        // k1 is gone -> offset>0 recomputes rather than replaying "v1".
        let k1_after_eviction = cache
            .get_or_anchor("k1", 1, || ok(&["recomputed"]))
            .unwrap();
        assert_eq!(k1_after_eviction, vec!["recomputed"]);
    }

    #[test]
    fn clear_drops_every_entry() {
        let cache: StableOrderCache<&str, String> = StableOrderCache::default();
        cache.get_or_anchor("k", 0, || ok(&["a"])).unwrap();
        cache.clear();
        let result = cache
            .get_or_anchor("k", 1, || ok(&["recomputed-after-clear"]))
            .unwrap();
        assert_eq!(result, vec!["recomputed-after-clear"]);
    }

    #[test]
    fn compute_error_propagates_and_does_not_anchor() {
        let cache: StableOrderCache<&str, String> = StableOrderCache::default();
        let err: Result<Vec<String>, &str> = cache.get_or_anchor("k", 0, || Err("boom"));
        assert_eq!(err, Err("boom"));

        // Nothing was anchored, so a later call still needs to compute.
        let result: Result<Vec<String>, &str> =
            cache.get_or_anchor("k", 1, || Ok(vec!["computed".to_string()]));
        assert_eq!(result, Ok(vec!["computed".to_string()]));
    }
}
