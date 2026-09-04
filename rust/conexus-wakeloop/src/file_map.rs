//! `FileMap` — port of `agent_mcp/core/globals.py`'s `g.file_map`, the
//! in-memory advisory file-claim map `file_management_tools.py`'s two
//! tools (`check_file_status`/`update_file_status`) read and mutate.
//! Process-wide, keyed on the resolved absolute filepath — no DB
//! table backs this in Python, and none is added here either; a
//! restart clears every claim, matching Python's exact "advisory,
//! not durable" contract.
//!
//! Lives here (not in `conexus-core`, which stays zero-I/O-and-no-
//! shared-mutable-state, and not in `conexus-tools`, since it holds
//! real process state rather than tool logic) for the same reason
//! `WaiterRegistry` does — see this crate's own module doc. Shares
//! that type's `std::sync::Mutex`-around-a-plain-map shape: every
//! operation here is a quick, non-async map read/write, never held
//! across an `.await`.

use std::collections::HashMap;
use std::sync::Mutex;

/// One claim record. Mirrors Python's `g.file_map[path]` dict shape
/// (`{"agent_id", "timestamp", "status"}`) as a real struct.
#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct FileMapEntry {
    pub agent_id: String,
    pub timestamp: String,
    pub status: String,
}

pub struct FileMap {
    entries: Mutex<HashMap<String, FileMapEntry>>,
}

impl Default for FileMap {
    fn default() -> Self {
        Self::new()
    }
}

impl FileMap {
    pub fn new() -> Self {
        Self {
            entries: Mutex::new(HashMap::new()),
        }
    }

    /// The current claim on `resolved_path`, if any. Returns an owned
    /// clone so the lock is released before the caller does anything
    /// else with it (never held across a DB call or an `.await`).
    pub fn get(&self, resolved_path: &str) -> Option<FileMapEntry> {
        self.entries.lock().unwrap().get(resolved_path).cloned()
    }

    /// Claim (or re-claim) `resolved_path` for `agent_id` with
    /// `status`. Overwrites any prior entry unconditionally — the
    /// caller (the tool) is responsible for the ownership gate
    /// (SEC-R20/AZ-R20-1: a foreign holder must never be silently
    /// overwritten) before calling this.
    pub fn claim(&self, resolved_path: &str, agent_id: &str, status: &str, now: &str) {
        self.entries.lock().unwrap().insert(
            resolved_path.to_string(),
            FileMapEntry {
                agent_id: agent_id.to_string(),
                timestamp: now.to_string(),
                status: status.to_string(),
            },
        );
    }

    /// Remove any claim on `resolved_path`. Returns `true` iff an
    /// entry was actually present (Python: `if resolved_abs_filepath
    /// in g.file_map: del ...` vs. the untracked-path idempotent-Ok
    /// branch — the caller uses this to pick between those two
    /// response shapes).
    pub fn release(&self, resolved_path: &str) -> bool {
        self.entries.lock().unwrap().remove(resolved_path).is_some()
    }

    /// The current number of claimed paths. Port of Python's
    /// `len(g.file_map)` (`view_status`'s `file_map_size` field).
    pub fn len(&self) -> usize {
        self.entries.lock().unwrap().len()
    }

    /// True iff no path is currently claimed.
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// The first `n` claimed paths (arbitrary order -- `HashMap`
    /// carries no ordering guarantee, matching Python's own
    /// insertion-order-agnostic `dict.items()` slice in practice
    /// since CPython's ordering isn't a documented contract this
    /// preview relies on either). Port of `view_status`'s
    /// `file_map_preview` field.
    pub fn preview(&self, n: usize) -> Vec<(String, FileMapEntry)> {
        self.entries
            .lock()
            .unwrap()
            .iter()
            .take(n)
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_fresh_map_has_no_claim_on_any_path() {
        let map = FileMap::new();
        assert_eq!(map.get("/tmp/a.txt"), None);
    }

    #[test]
    fn claim_then_get_returns_the_recorded_entry() {
        let map = FileMap::new();
        map.claim("/tmp/a.txt", "worker-1", "editing", "2026-06-01T00:00:00Z");
        assert_eq!(
            map.get("/tmp/a.txt"),
            Some(FileMapEntry {
                agent_id: "worker-1".to_string(),
                timestamp: "2026-06-01T00:00:00Z".to_string(),
                status: "editing".to_string(),
            })
        );
    }

    #[test]
    fn a_second_claim_overwrites_the_first() {
        let map = FileMap::new();
        map.claim("/tmp/a.txt", "worker-1", "editing", "2026-06-01T00:00:00Z");
        map.claim("/tmp/a.txt", "worker-2", "reading", "2026-06-01T00:01:00Z");
        assert_eq!(map.get("/tmp/a.txt").unwrap().agent_id, "worker-2");
    }

    #[test]
    fn release_removes_a_present_entry_and_reports_it_was_present() {
        let map = FileMap::new();
        map.claim("/tmp/a.txt", "worker-1", "editing", "2026-06-01T00:00:00Z");
        assert!(map.release("/tmp/a.txt"));
        assert_eq!(map.get("/tmp/a.txt"), None);
    }

    #[test]
    fn releasing_an_untracked_path_reports_it_was_not_present() {
        let map = FileMap::new();
        assert!(!map.release("/tmp/never-claimed.txt"));
    }

    #[test]
    fn len_and_is_empty_reflect_the_current_claim_count() {
        let map = FileMap::new();
        assert_eq!(map.len(), 0);
        assert!(map.is_empty());
        map.claim("/tmp/a.txt", "worker-1", "editing", "2026-06-01T00:00:00Z");
        assert_eq!(map.len(), 1);
        assert!(!map.is_empty());
    }

    #[test]
    fn preview_caps_at_n_entries() {
        let map = FileMap::new();
        for i in 0..10 {
            map.claim(
                &format!("/tmp/{i}.txt"),
                "worker-1",
                "editing",
                "2026-06-01T00:00:00Z",
            );
        }
        assert_eq!(map.preview(5).len(), 5);
        assert_eq!(map.preview(100).len(), 10);
    }
}
