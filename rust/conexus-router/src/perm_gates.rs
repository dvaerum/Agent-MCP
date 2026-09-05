//! Real fusion wrappers for the router's revalidation pattern. Port
//! of `agent_mcp/router/perm_gates.py`'s `read_body_and_revalidate`/
//! `revalidated_lock`/`revalidate_after`. Phase E2,
//! `conexus-router-revalidation-fusion` (PR23 step 5 of the 10-PR
//! app-wiring breakdown).
//!
//! **The genuine yield points these fuse around already exist as
//! real, tested, production async code** -- confirmed by reading
//! `orchestrator::runtime::RuntimeStore::ensure_lock` and
//! `orchestrator::primitives::{systemctl, is_active}` directly, both
//! already exercised today by `orchestrator::ensure::ensure`. This
//! module's whole job is pure composition (wrap an existing await
//! with an existing synchronous decision call, in the right order),
//! not inventing new async machinery -- confirms the plan's own
//! "designing against imagined yield points would be guessing"
//! rationale for deferring this file until real axum handlers exist.
//!
//! **`read_body_and_revalidate` is a plain SYNC function here**,
//! unlike Python's `async def`: in aiohttp there's no automatic
//! extractor, so Python's own version performs the body-read await
//! INSIDE itself. In axum, a `Bytes` extractor already performs that
//! same await, in the handler's own function signature, BEFORE any
//! handler code runs -- by the time this function is called
//! (immediately, with the already-extracted body), the real yield
//! point has already happened. The fusion still matters (closing the
//! TOCTOU gap between the session-gate middleware's entry-time
//! resolution and the extractor's own await), it just doesn't need to
//! BE async to close it.
//!
//! **`revalidated_lock`/`revalidate_after` take `db: &AsyncMutex<
//! Connection>`, never a bare `&Connection`, across their own
//! `.await`** -- a bare `&Connection` held across an await makes the
//! enclosing future `!Send` (`Connection: Send` but not `Sync`,
//! confirmed the same root cause as Phase D2's async-`Tool`-trait
//! fix); the DB lock is acquired FRESH, after the real yield point,
//! never held across it.
//!
//! Not yet wired to a real axum handler -- that's the
//! lifecycle-rest/users-groups-rest PRs (steps 6-7), matching every
//! prior PR1-shaped module in this migration (`state.rs`, `boot.rs`,
//! `json_sanitize.rs` itself).
#![allow(dead_code)]

use std::future::Future;

use conexus_core::capability::Capability;
use conexus_core::principal::Principal;
use rusqlite::Connection;
use tokio::sync::{Mutex as AsyncMutex, OwnedMutexGuard};

use crate::json_sanitize;
use crate::mcp_handler::{HandlerBody, HandlerResponse};
use crate::orchestrator::runtime::RuntimeStore;
use crate::project_gate::{self, RevalidateCapabilityOutcome, RevalidateOutcome};

/// The project-scoped half of a revalidation call -- present iff the
/// gated capability is tied to a specific project (matches
/// `read_body_and_revalidate(req, parse_body, cap, project_name=...,
/// min_role=...)`'s own optional pair).
#[derive(Debug, Clone, Copy)]
pub struct RevalidationProject<'a> {
    pub project_name: &'a str,
    pub min_role: Option<&'a str>,
}

/// Every input `revalidate` needs, bundled so the three public
/// wrappers below share one parameter instead of six positional ones
/// apiece.
#[derive(Debug, Clone, Copy)]
pub struct RevalidationSpec<'a> {
    pub stale_user_id: &'a str,
    pub cookie_header: Option<&'a str>,
    pub now: &'a str,
    pub cap: Capability,
    pub project: Option<RevalidationProject<'a>>,
}

fn forbidden(message: &str) -> HandlerResponse {
    HandlerResponse {
        status: 403,
        headers: Vec::new(),
        body: HandlerBody::Json(serde_json::json!({"success": false, "message": message})),
    }
}

fn internal_error(message: &str) -> HandlerResponse {
    HandlerResponse {
        status: 500,
        headers: Vec::new(),
        body: HandlerBody::Json(serde_json::json!({"success": false, "message": message})),
    }
}

fn validation_error(message: &str) -> HandlerResponse {
    HandlerResponse {
        status: 400,
        headers: Vec::new(),
        body: HandlerBody::Json(serde_json::json!({"success": false, "message": message})),
    }
}

/// Composes [`project_gate::revalidate_capability`] (no project) or
/// [`project_gate::revalidate_capability_and_membership`] (a project
/// is named), depending on `spec.project` -- the ONE place a caller
/// needs to know which of the two underlying primitives applies.
fn revalidate(conn: &Connection, spec: &RevalidationSpec) -> Result<Box<Principal>, HandlerResponse> {
    match spec.project {
        None => {
            match project_gate::revalidate_capability(
                conn,
                spec.stale_user_id,
                spec.cookie_header,
                spec.now,
                spec.cap,
            ) {
                Ok(RevalidateCapabilityOutcome::Allow(principal)) => Ok(principal),
                Ok(RevalidateCapabilityOutcome::DeniedSessionInvalid) => {
                    Err(forbidden("session no longer valid"))
                }
                Ok(RevalidateCapabilityOutcome::DeniedCapability) => {
                    Err(forbidden("capability revoked"))
                }
                Err(e) => Err(internal_error(&e.to_string())),
            }
        }
        Some(project) => {
            match project_gate::revalidate_capability_and_membership(
                conn,
                spec.stale_user_id,
                spec.cookie_header,
                spec.now,
                spec.cap,
                project.project_name,
                project.min_role,
            ) {
                Ok(RevalidateOutcome::Allow(principal)) => Ok(principal),
                Ok(RevalidateOutcome::DeniedSessionInvalid) => {
                    Err(forbidden("session no longer valid"))
                }
                Ok(RevalidateOutcome::DeniedCapability) => Err(forbidden("capability revoked")),
                Ok(RevalidateOutcome::DeniedMembership) => {
                    Err(forbidden("project membership revoked"))
                }
                Ok(RevalidateOutcome::DeniedRank { role, min_role }) => Err(forbidden(&format!(
                    "role {role:?} no longer meets the required {min_role:?}"
                ))),
                Err(e) => Err(internal_error(&e.to_string())),
            }
        }
    }
}

/// Port of `read_body_and_revalidate`. Decodes `raw_body` through the
/// shared sanitizer chokepoint AND revalidates in one call -- see the
/// module doc for why this is synchronous.
pub fn read_body_and_revalidate(
    conn: &Connection,
    raw_body: &[u8],
    spec: &RevalidationSpec,
) -> Result<(serde_json::Map<String, serde_json::Value>, Box<Principal>), HandlerResponse> {
    let body = json_sanitize::decode_untrusted_body(raw_body)
        .map_err(|e| validation_error(&e.to_string()))?;
    let principal = revalidate(conn, spec)?;
    Ok((body, principal))
}

/// Port of `revalidated_lock`. Acquires the per-`(name, role)`
/// `ensure_lock` AND revalidates as one atomic unit before the
/// caller's own protected block runs -- the lock-contention sibling
/// of [`read_body_and_revalidate`], for handlers whose genuine yield
/// point is lock acquisition (`delete_project_handler`/
/// `stop_project_handler`) rather than a body-read.
pub async fn revalidated_lock(
    store: &RuntimeStore,
    db: &AsyncMutex<Connection>,
    name: &str,
    role: &str,
    spec: &RevalidationSpec<'_>,
) -> Result<(OwnedMutexGuard<()>, Box<Principal>), HandlerResponse> {
    let mutex = store.ensure_lock(name, role);
    let guard = mutex.lock_owned().await;
    let conn = db.lock().await;
    let principal = revalidate(&conn, spec)?;
    Ok((guard, principal))
}

/// Port of `revalidate_after`. Awaits `awaitable` AND revalidates
/// immediately after it resolves -- for the in-lock systemctl-stop/
/// is-active await inside `revalidated_lock`'s own protected block
/// (R14-F2: a held lock only blocks OTHER coroutines racing for the
/// SAME lock; it does nothing to stop an unrelated capability/
/// membership revocation from committing while this task is
/// suspended mid-await).
pub async fn revalidate_after<T>(
    awaitable: impl Future<Output = T>,
    db: &AsyncMutex<Connection>,
    spec: &RevalidationSpec<'_>,
) -> (T, Result<Box<Principal>, HandlerResponse>) {
    let result = awaitable.await;
    let conn = db.lock().await;
    let outcome = revalidate(&conn, spec);
    (result, outcome)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::orchestrator::primitives::{self, SystemctlMode};
    use conexus_db::schema::init_router_schema;

    fn conn() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch("PRAGMA foreign_keys = ON;").unwrap();
        init_router_schema(&c).unwrap();
        c
    }
    const NOW: &str = "2026-01-01T00:00:00.000+00:00";

    fn seed_sysadmin(c: &mut Connection, username: &str) -> String {
        crate::identity::create_user(
            c,
            username,
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap()
    }

    // -- read_body_and_revalidate ------------------------------------

    #[test]
    fn read_body_and_revalidate_admits_a_well_formed_body_for_a_capable_caller() {
        let mut c = conn();
        let uid = seed_sysadmin(&mut c, "alice");
        let spec = RevalidationSpec {
            stale_user_id: &uid,
            cookie_header: None,
            now: NOW,
            cap: Capability::SystemUsersManage,
            project: None,
        };
        let (body, _principal) =
            read_body_and_revalidate(&c, br#"{"username": "bob"}"#, &spec).unwrap();
        assert_eq!(body["username"], "bob");
    }

    #[test]
    fn read_body_and_revalidate_rejects_malformed_json_before_ever_revalidating() {
        let mut c = conn();
        let uid = seed_sysadmin(&mut c, "alice");
        let spec = RevalidationSpec {
            stale_user_id: &uid,
            cookie_header: None,
            now: NOW,
            cap: Capability::SystemUsersManage,
            project: None,
        };
        let resp = read_body_and_revalidate(&c, b"{not json", &spec).unwrap_err();
        assert_eq!(resp.status, 400);
    }

    #[test]
    fn read_body_and_revalidate_denies_a_capability_revoked_mid_flight() {
        let mut c = conn();
        seed_sysadmin(&mut c, "alice"); // sysadmin, irrelevant here
        let bob = crate::identity::create_user(
            &mut c,
            "bob",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        let spec = RevalidationSpec {
            stale_user_id: &bob,
            cookie_header: None,
            now: NOW,
            cap: Capability::SystemUsersManage,
            project: None,
        };
        let resp = read_body_and_revalidate(&c, br#"{}"#, &spec).unwrap_err();
        assert_eq!(resp.status, 403);
    }

    // -- revalidated_lock / revalidate_after ---------------------------

    #[tokio::test]
    async fn revalidated_lock_admits_a_capable_caller_and_holds_the_named_lock() {
        let mut c = conn();
        let uid = seed_sysadmin(&mut c, "alice");
        let db = AsyncMutex::new(c);
        let store = RuntimeStore::new();
        let spec = RevalidationSpec {
            stale_user_id: &uid,
            cookie_header: None,
            now: NOW,
            cap: Capability::SystemProjectsManage,
            project: Some(RevalidationProject {
                project_name: "proj-a",
                min_role: None,
            }),
        };
        let (_guard, principal) = revalidated_lock(&store, &db, "proj-a", "backend", &spec)
            .await
            .unwrap();
        assert!(principal.has_capability(Capability::SystemProjectsManage));
    }

    #[tokio::test]
    async fn revalidated_lock_denies_a_non_member_even_while_holding_the_lock() {
        let mut c = conn();
        seed_sysadmin(&mut c, "alice");
        let bob = crate::identity::create_user(
            &mut c,
            "bob",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        let db = AsyncMutex::new(c);
        let store = RuntimeStore::new();
        let spec = RevalidationSpec {
            stale_user_id: &bob,
            cookie_header: None,
            now: NOW,
            cap: Capability::SystemProjectsManage,
            project: Some(RevalidationProject {
                project_name: "proj-a",
                min_role: Some("operator"),
            }),
        };
        let resp = revalidated_lock(&store, &db, "proj-a", "backend", &spec)
            .await
            .unwrap_err();
        assert_eq!(resp.status, 403);
    }

    #[tokio::test]
    async fn revalidate_after_runs_the_real_awaitable_then_revalidates() {
        let mut c = conn();
        let uid = seed_sysadmin(&mut c, "alice");
        let db = AsyncMutex::new(c);
        let spec = RevalidationSpec {
            stale_user_id: &uid,
            cookie_header: None,
            now: NOW,
            cap: Capability::SystemUsersManage,
            project: None,
        };
        // A real async awaitable -- the same primitive
        // orchestrator::ensure uses for its own in-lock systemctl
        // await, proving this composes with genuine async I/O, not a
        // toy future.
        let awaitable = primitives::is_active(
            SystemctlMode::User,
            "definitely-not-a-real-unit.service",
            std::time::Duration::from_millis(200),
        );
        let (is_active_result, revalidate_result) = revalidate_after(awaitable, &db, &spec).await;
        assert!(!is_active_result);
        assert!(revalidate_result.is_ok());
    }
}
