//! sqlite-vec extension loading — the "gracefully degrade to no-RAG"
//! contract.
//!
//! Faithful in *intent* to `agent_mcp/db/connection.py`'s
//! `check_vss_loadability()`/`is_vss_loadable()` (never panics, a
//! failure to load just means "no RAG on this host"), but the
//! *mechanism* is deliberately re-derived rather than ported at face
//! value, per the migration plan's "Things to explicitly re-derive"
//! guidance: Python dynamically `dlopen`s a shared-library file via
//! `sqlite_vec.load(conn)`, so "present / absent / corrupt" are
//! filesystem conditions there. The Rust `sqlite-vec` crate instead
//! compiles the extension's C source directly into this binary and
//! exposes only a raw `sqlite3_vec_init` symbol, registered
//! process-wide via SQLite's `sqlite3_auto_extension` hook (see that
//! crate's own upstream test) — there is no file on disk to be
//! absent or corrupt. To keep the same three-case contract
//! independently testable without needing a real broken `.so` on
//! disk, the actual entry point is swappable: production code
//! registers the real `sqlite3_vec_init`, tests register fakes that
//! simulate "never registers `vec_version()`" (absent) and "extension
//! init itself fails" (corrupt).

use rusqlite::ffi;
use rusqlite::Connection;
use std::os::raw::{c_char, c_int};
use std::sync::Mutex;

/// The C ABI signature SQLite calls for every new connection once an
/// entry point is registered via `sqlite3_auto_extension`.
type ExtEntryPoint = unsafe extern "C" fn(
    db: *mut ffi::sqlite3,
    pz_err_msg: *mut *mut c_char,
    p_api: *const ffi::sqlite3_api_routines,
) -> c_int;

/// `sqlite3_auto_extension`/`sqlite3_cancel_auto_extension` are
/// process-wide C globals — serialize register→probe→cancel so
/// concurrent test threads (or concurrent callers) can't interleave
/// registrations and see each other's entry point.
static REGISTRATION_LOCK: Mutex<()> = Mutex::new(());

/// Register `entry_point` process-wide, open a throwaway in-memory
/// connection, and confirm `vec_version()` is actually callable on
/// it. `cancel_after` controls whether the registration is torn down
/// again once probed (`true`, for a one-shot loadability check) or
/// left in place for the rest of the process (`false`, for actually
/// wanting a working `vec0` on connections opened afterward) — either
/// way, a FAILED probe always cancels, so a failing call never leaks
/// a dead registration.
///
/// # Safety
/// `entry_point` must be a valid SQLite extension entry point: safe
/// to invoke with a live `sqlite3*` for the lifetime of any
/// connection opened after registration and before cancellation.
unsafe fn register_impl(entry_point: ExtEntryPoint, cancel_after: bool) -> bool {
    let _guard = REGISTRATION_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());

    // SAFETY: caller's contract (see function doc) covers this call.
    unsafe { register_impl_locked(entry_point, cancel_after) }
}

/// Same as [`register_impl`] but assumes `REGISTRATION_LOCK` is
/// already held by the caller. Exists so a caller can extend the
/// critical section PAST registration — e.g. to also open and use a
/// second connection before releasing the lock — which plain
/// `register_impl` can't do without deadlocking on its own
/// non-reentrant lock. See
/// `register_sqlite_vec_leaves_the_extension_usable_on_later_connections`'s
/// test for why that extension matters: `sqlite3_auto_extension` runs
/// EVERY registered entry point on EVERY new connection, so a caller
/// that registers persistently, releases the lock, and only THEN
/// opens a follow-up connection has a gap where a concurrent
/// `register_and_probe` of a FAILING entry point (from an unrelated
/// caller/test) can register itself in that gap and break the
/// follow-up connection open — even though it has nothing to do with
/// this caller's extension. Reproduced empirically: this crate's test
/// suite flaked under parallel execution (the `cargo test` default)
/// before this split existed.
///
/// # Safety
/// Same contract as [`register_impl`], PLUS: `REGISTRATION_LOCK` must
/// already be held by the calling thread.
unsafe fn register_impl_locked(entry_point: ExtEntryPoint, cancel_after: bool) -> bool {
    let registered = unsafe { ffi::sqlite3_auto_extension(Some(entry_point)) } == ffi::SQLITE_OK;

    let loadable = registered
        && Connection::open_in_memory()
            .and_then(|conn| {
                conn.query_row("select vec_version()", [], |row| row.get::<_, String>(0))
            })
            .is_ok();

    if cancel_after || !loadable {
        // SAFETY: same entry point value passed to the matching
        // register call above; cancel is safe to call even if
        // register failed.
        unsafe {
            ffi::sqlite3_cancel_auto_extension(Some(entry_point));
        }
    }

    loadable
}

/// # Safety
/// See [`register_impl`] — same contract, always cancels afterward.
unsafe fn register_and_probe(entry_point: ExtEntryPoint) -> bool {
    unsafe { register_impl(entry_point, true) }
}

/// # Safety
/// See [`register_impl`] — same contract, only cancels on failure.
unsafe fn register_persistently(entry_point: ExtEntryPoint) -> bool {
    unsafe { register_impl(entry_point, false) }
}

/// The real `sqlite-vec` entry point. `sqlite-vec`'s own crate
/// declares `sqlite3_vec_init` with a simplified zero-argument
/// signature and transmutes it to the real extension-entry-point ABI
/// at the call site in its own upstream test — this mirrors that
/// exact pattern, which is the only usage this (alpha-quality) crate
/// documents.
fn real_entry_point() -> ExtEntryPoint {
    // SAFETY: `sqlite3_vec_init`'s actual C definition matches the
    // real SQLite extension-entry-point ABI; the Rust declaration's
    // empty argument list is a simplification on the crate's side,
    // not a different real signature. Matches `sqlite-vec`'s own
    // `test_rusqlite_auto_extension` test verbatim.
    unsafe { std::mem::transmute(sqlite_vec::sqlite3_vec_init as *const ()) }
}

/// Attempt to make sqlite-vec available and confirm a live connection
/// can actually use it. Returns `false` — never panics — on any
/// failure: this is the single call site downstream RAG code should
/// use to decide "vector search available or not", matching Python's
/// `is_vss_loadable()` contract.
pub fn check_vss_loadable() -> bool {
    unsafe { register_and_probe(real_entry_point()) }
}

/// Permanently register the real sqlite-vec extension process-wide,
/// so every connection opened AFTER this call (for the rest of the
/// process) has a genuinely working `vec0` module — unlike
/// [`check_vss_loadable`], which is a one-shot probe that always
/// cancels its own registration before returning. Call this once at
/// startup in any process that actually needs to read/write a real
/// `rag_embeddings` table; checking loadability alone does not make
/// the table usable afterward. Returns whether registration succeeded
/// and a live probe connection could actually call `vec_version()`;
/// on `false`, nothing is left registered — same never-leak-on-
/// failure guarantee as `check_vss_loadable`.
pub fn register_sqlite_vec() -> bool {
    unsafe { register_persistently(real_entry_point()) }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Simulates "extension absent": registration succeeds (SQLite
    /// accepts any entry point), but it registers nothing, so
    /// `vec_version()` is not a function `rusqlite` can call — the
    /// same downstream symptom Python's degrade path produces when
    /// `sqlite_vec` fails to `dlopen`.
    unsafe extern "C" fn fake_absent(
        _db: *mut ffi::sqlite3,
        _pz_err_msg: *mut *mut c_char,
        _p_api: *const ffi::sqlite3_api_routines,
    ) -> c_int {
        ffi::SQLITE_OK
    }

    /// Simulates "extension corrupt": the entry point itself reports
    /// failure, which SQLite propagates as a failed `sqlite3_open`
    /// for every connection opened afterward — the case Python
    /// catches as `sqlite3.Error` during `conn.load_extension()`.
    unsafe extern "C" fn fake_corrupt(
        _db: *mut ffi::sqlite3,
        _pz_err_msg: *mut *mut c_char,
        _p_api: *const ffi::sqlite3_api_routines,
    ) -> c_int {
        ffi::SQLITE_ERROR
    }

    #[test]
    fn extension_present_is_loadable() {
        assert!(check_vss_loadable());
    }

    #[test]
    fn extension_absent_degrades_gracefully_to_false() {
        assert!(!unsafe { register_and_probe(fake_absent) });
    }

    #[test]
    fn extension_corrupt_degrades_gracefully_to_false() {
        assert!(!unsafe { register_and_probe(fake_corrupt) });
    }

    #[test]
    fn register_sqlite_vec_leaves_the_extension_usable_on_later_connections() {
        // Hold REGISTRATION_LOCK across this test's ENTIRE body, not
        // just the internal register call `register_sqlite_vec()`
        // makes -- otherwise a concurrent test thread's
        // `register_and_probe` of a FAILING fake entry point can land
        // in the gap between `register_sqlite_vec()` returning (which
        // only holds the lock for its own register+probe) and the
        // `Connection::open_in_memory()` below, breaking that
        // follow-up open. See `register_impl_locked`'s doc for the
        // full mechanism; `holding_the_lock_blocks_a_concurrent_
        // failing_probe` below proves the lock itself actually
        // prevents that interleaving.
        let _guard = REGISTRATION_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let entry_point = real_entry_point();
        assert!(unsafe { register_impl_locked(entry_point, false) });

        // Unlike check_vss_loadable (which always cancels its own
        // registration), a FRESH connection opened well after this
        // call must still be able to use vec0 -- that's the entire
        // point of this function existing.
        let conn = Connection::open_in_memory().unwrap();
        let version: String = conn
            .query_row("select vec_version()", [], |row| row.get(0))
            .unwrap();
        assert!(version.starts_with('v'));

        // Clean up so this test doesn't leak process-wide state into
        // whatever test happens to run after it in the same binary.
        unsafe {
            ffi::sqlite3_cancel_auto_extension(Some(entry_point));
        }
    }

    #[test]
    fn probing_never_leaks_registration_state() {
        // Same class of race as
        // `register_sqlite_vec_leaves_the_extension_usable_on_later_connections`:
        // `register_and_probe` itself is fully lock-protected, but the
        // follow-up `Connection::open_in_memory()` check below is a
        // raw open with no lock of its own — a CONCURRENT test's own
        // register_impl call briefly registers (and then cancels) its
        // entry point while holding the SAME lock, and
        // `sqlite3_auto_extension` runs every registered entry point
        // on every new connection. An unprotected open landing inside
        // that other call's tiny registered-but-not-yet-cancelled
        // window can fail even though it has nothing to do with THIS
        // test's own fake_corrupt (which is already gone by the time
        // `register_and_probe` returns). Hold the lock across this
        // test's own follow-up check too, for the same reason.
        let _guard = REGISTRATION_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        assert!(!unsafe { register_impl_locked(fake_corrupt, true) });
        assert!(Connection::open_in_memory().is_ok());
    }

    #[test]
    fn holding_the_lock_blocks_a_concurrent_failing_probe() {
        // Regression test for a real flake reproduced against `main`
        // (verified: `cargo test -p conexus-vec --lib` failed ~1 run
        // in 5, always inside
        // `register_sqlite_vec_leaves_the_extension_usable_on_later_connections`'s
        // follow-up `Connection::open_in_memory()`, with error
        // "automatic extension loading failed"). Root cause:
        // `sqlite3_auto_extension` runs EVERY registered entry point
        // on EVERY new connection, and the old `register_impl` only
        // held `REGISTRATION_LOCK` for its own register+probe+cancel
        // -- a caller that registers persistently and only opens its
        // follow-up connection AFTER releasing the lock has a gap
        // where a concurrent `register_and_probe(fake_corrupt)` (from
        // an unrelated test thread) can register itself and break
        // that follow-up open.
        //
        // The fix (`register_impl_locked` + holding the guard across
        // the whole register-then-use sequence, see the test above)
        // relies on `REGISTRATION_LOCK` actually blocking a concurrent
        // registration attempt for as long as it's held. This test
        // proves that mechanism directly.
        let _guard = REGISTRATION_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());

        let handle = std::thread::spawn(|| unsafe { register_and_probe(fake_corrupt) });

        // Give the spawned thread every chance to run first if the
        // lock weren't actually serializing against it.
        std::thread::sleep(std::time::Duration::from_millis(50));
        assert!(
            !handle.is_finished(),
            "a concurrent registration attempt must block while REGISTRATION_LOCK is held"
        );

        drop(_guard);
        assert!(!handle.join().unwrap());
    }
}
