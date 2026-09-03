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
/// it — then always unregister, so this function leaves no global
/// state behind regardless of outcome.
///
/// # Safety
/// `entry_point` must be a valid SQLite extension entry point: safe
/// to invoke with a live `sqlite3*` for the lifetime of any
/// connection opened after registration and before cancellation.
unsafe fn register_and_probe(entry_point: ExtEntryPoint) -> bool {
    let _guard = REGISTRATION_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());

    // SAFETY: caller's contract (see function doc) covers this call.
    let registered = unsafe { ffi::sqlite3_auto_extension(Some(entry_point)) } == ffi::SQLITE_OK;

    let loadable = registered
        && Connection::open_in_memory()
            .and_then(|conn| {
                conn.query_row("select vec_version()", [], |row| row.get::<_, String>(0))
            })
            .is_ok();

    // SAFETY: same entry point value passed to the matching register
    // call above; cancel is safe to call even if register failed.
    unsafe {
        ffi::sqlite3_cancel_auto_extension(Some(entry_point));
    }

    loadable
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
    fn probing_never_leaks_registration_state() {
        // A corrupt probe must not leave its failing entry point
        // registered — otherwise every connection opened by a LATER
        // caller (including the next test, or production code after
        // a failed probe) would start failing too.
        assert!(!unsafe { register_and_probe(fake_corrupt) });
        assert!(Connection::open_in_memory().is_ok());
    }
}
