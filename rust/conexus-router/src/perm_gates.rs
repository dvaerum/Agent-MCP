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

/// Port of `_deny_cross_tenant_project_read`'s `role is None` branch
/// (`_error(error=_ERROR_NOT_FOUND, ..., status=404)`) -- the SAME
/// uniform 404 `not_found` envelope a nonexistent project produces.
/// **Found-and-fixed bug (this PR)**: [`RevalidateOutcome::
/// DeniedMembership`] used to map to [`forbidden`] (403) below, which
/// reopens the exact project-existence oracle
/// `deny_cross_tenant_project_read` exists to close -- a non-member
/// delegate's response for an EXISTING (hidden) project must be
/// indistinguishable from a NONEXISTENT one, not a distinct status
/// that confirms the project is real. Python's own
/// `_revalidate_capability_and_membership_or_403` composes
/// `revalidate_capability_or_403` (genuinely 403 on a stale/revoked
/// capability) with `_deny_cross_tenant_project_read` (404 when the
/// caller resolves NO role at all; 403 only when a role resolves but
/// its rank is below `min_role` -- see that function's own docstring)
/// -- never a bespoke 403 for "no role".
fn not_found(message: &str) -> HandlerResponse {
    HandlerResponse {
        status: 404,
        headers: Vec::new(),
        body: HandlerBody::Json(serde_json::json!({
            "success": false,
            "error": "not_found",
            "message": message,
        })),
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
fn revalidate(
    conn: &Connection,
    spec: &RevalidationSpec,
) -> Result<Box<Principal>, HandlerResponse> {
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
                Ok(RevalidateOutcome::DeniedMembership) => Err(not_found(&format!(
                    "unknown project: {:?}",
                    project.project_name
                ))),
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

    /// A file-backed router DB opened as BOTH a `rusqlite::Connection`
    /// (for `revalidate`/`project_gate`/`group_membership_repository`,
    /// all still sync in this file) and a sea-orm `DatabaseConnection`
    /// (for `identity::create_user`/friends, now async) -- same
    /// dual-connection recipe `identity.rs`'s own tests use, since an
    /// in-memory `:memory:` DB can't be shared across two separate
    /// connection handles the way a real file can.
    async fn conn_with_sea_orm() -> (tempfile::TempDir, Connection, sea_orm::DatabaseConnection) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("perm_gates_test.db");
        let c = Connection::open(&path).unwrap();
        c.execute_batch("PRAGMA foreign_keys = ON;").unwrap();
        init_router_schema(&c).unwrap();
        let db = sea_orm::Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        (dir, c, db)
    }

    const NOW: &str = "2026-01-01T00:00:00.000+00:00";

    async fn seed_sysadmin(db: &sea_orm::DatabaseConnection, username: &str) -> String {
        crate::identity::create_user(
            db,
            username,
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap()
    }

    // -- read_body_and_revalidate ------------------------------------

    #[tokio::test]
    async fn read_body_and_revalidate_admits_a_well_formed_body_for_a_capable_caller() {
        let (_dir, c, db) = conn_with_sea_orm().await;
        let uid = seed_sysadmin(&db, "alice").await;
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

    #[tokio::test]
    async fn read_body_and_revalidate_rejects_malformed_json_before_ever_revalidating() {
        let (_dir, c, db) = conn_with_sea_orm().await;
        let uid = seed_sysadmin(&db, "alice").await;
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

    #[tokio::test]
    async fn read_body_and_revalidate_denies_a_capability_revoked_mid_flight() {
        let (_dir, c, db) = conn_with_sea_orm().await;
        seed_sysadmin(&db, "alice").await; // sysadmin, irrelevant here
        let bob = crate::identity::create_user(
            &db,
            "bob",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
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
        let (_dir, c, db) = conn_with_sea_orm().await;
        let uid = seed_sysadmin(&db, "alice").await;
        let lock = AsyncMutex::new(c);
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
        let (_guard, principal) = revalidated_lock(&store, &lock, "proj-a", "backend", &spec)
            .await
            .unwrap();
        assert!(principal.has_capability(Capability::SystemProjectsManage));
    }

    /// Grants `user_id` `cap` via a fresh, dedicated group -- the
    /// Wave-9 delegation shape every pentest fixture uses, so the
    /// capability check the revalidator runs FIRST always passes and
    /// the membership/rank branch under test is what actually decides
    /// the outcome.
    fn grant_capability_via_group(c: &Connection, user_id: &str, cap: Capability) {
        let group =
            conexus_db::group_membership_repository::create_group(c, "g-delegated", false, NOW)
                .unwrap();
        conexus_db::group_capability_repository::replace(c, &group.group_id, [cap.as_str()])
            .unwrap();
        conexus_db::group_membership_repository::add_group_member(
            c,
            &group.group_id,
            Some(user_id),
            None,
            NOW,
        )
        .unwrap();
    }

    /// Bob holds the capability (via a delegated group) but resolves
    /// NO role at all on `proj-a` -- Python's
    /// `_deny_cross_tenant_project_read` (`role is None`) closes the
    /// project-existence oracle with a uniform 404 `not_found` here,
    /// reserving 403 for a resolved-but-under-ranked role (see the
    /// sibling test below). Found-and-fixed bug (this PR): this used
    /// to assert 403, matching `DeniedMembership`'s old (wrong)
    /// mapping -- see `perm_gates::not_found`'s own doc.
    #[tokio::test]
    async fn revalidated_lock_denies_a_non_member_even_while_holding_the_lock() {
        let (_dir, c, db) = conn_with_sea_orm().await;
        seed_sysadmin(&db, "alice").await;
        let bob = crate::identity::create_user(
            &db,
            "bob",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        grant_capability_via_group(&c, &bob, Capability::SystemProjectsManage);
        let lock = AsyncMutex::new(c);
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
        let resp = revalidated_lock(&store, &lock, "proj-a", "backend", &spec)
            .await
            .unwrap_err();
        assert_eq!(resp.status, 404);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON");
        };
        assert_eq!(body["error"], "not_found");
    }

    /// The sibling of the test above: bob DOES resolve a role
    /// (`viewer`), but it's below `min_role` ("operator") -- this is
    /// the genuine 403 branch (`DeniedRank`), since bob is a real,
    /// already-disclosed member and there's no existence oracle to
    /// close for him.
    #[tokio::test]
    async fn revalidated_lock_denies_an_under_ranked_member_with_403_not_404() {
        let (_dir, c, db) = conn_with_sea_orm().await;
        seed_sysadmin(&db, "alice").await;
        let bob = crate::identity::create_user(
            &db,
            "bob",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        grant_capability_via_group(&c, &bob, Capability::SystemProjectsManage);
        c.execute(
            "INSERT INTO project_membership (project_name, user_id, role) VALUES ('proj-a', ?1, 'viewer')",
            [&bob],
        )
        .unwrap();
        let lock = AsyncMutex::new(c);
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
        let resp = revalidated_lock(&store, &lock, "proj-a", "backend", &spec)
            .await
            .unwrap_err();
        assert_eq!(resp.status, 403);
    }

    #[tokio::test]
    async fn revalidate_after_runs_the_real_awaitable_then_revalidates() {
        let (_dir, c, db) = conn_with_sea_orm().await;
        let uid = seed_sysadmin(&db, "alice").await;
        let lock = AsyncMutex::new(c);
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
        let (is_active_result, revalidate_result) = revalidate_after(awaitable, &lock, &spec).await;
        assert!(!is_active_result);
        assert!(revalidate_result.is_ok());
    }

    /// R14-F2 (test_sec_r14f2_revalidated_lock_yield_gap.py): a held
    /// lock only blocks OTHER coroutines racing for the SAME lock --
    /// it does nothing to stop an unrelated capability revocation from
    /// committing to the DB while this task is suspended mid-await
    /// INSIDE the "protected" block. This is the genuine-concurrency
    /// version of the property `revalidate_after_runs_the_real_
    /// awaitable_then_revalidates` above already proves sequentially
    /// (await first, revalidate second): here a SEPARATE tokio task
    /// really does race the revocation against the paused awaitable --
    /// `revalidate_after`'s post-await DB read must observe it. This
    /// is the exact primitive `lifecycle_rest.rs`'s
    /// `delete_project_handler`/`stop_project_handler`/
    /// `rename_project_handler` all three fuse their own in-lock
    /// `systemctl stop`/`is-active` await around -- one shared,
    /// already-tested-here mechanism, so exercising it directly (with
    /// a real paced future standing in for the real systemctl await)
    /// covers all three call sites without needing a full axum/HTTP
    /// round trip through each handler.
    #[tokio::test]
    async fn revalidate_after_catches_a_revocation_that_lands_during_a_real_concurrent_await() {
        let (_dir, c, db) = conn_with_sea_orm().await;
        let uid = seed_sysadmin(&db, "alice").await;
        let lock = std::sync::Arc::new(AsyncMutex::new(c));
        let spec = RevalidationSpec {
            stale_user_id: &uid,
            cookie_header: None,
            now: NOW,
            cap: Capability::SystemProjectsManage,
            project: None,
        };

        let entered = std::sync::Arc::new(tokio::sync::Notify::new());
        let release = std::sync::Arc::new(tokio::sync::Notify::new());

        // Stands in for the real in-lock `systemctl stop`/`is-active`
        // await -- signals it has genuinely started, then blocks on a
        // real async notification (not a poll loop) until released.
        let entered_in_awaitable = entered.clone();
        let release_in_awaitable = release.clone();
        let awaitable = async move {
            entered_in_awaitable.notify_one();
            release_in_awaitable.notified().await;
        };

        // A REAL second task -- not a pre-mutation before the call --
        // races the revocation against the paused awaitable above:
        // waits for entry, revokes alice's sysadmin bit (her only
        // source of `SystemProjectsManage` here) via a genuine
        // concurrent DB write, then releases the pause.
        let lock_for_revoker = lock.clone();
        let uid_for_revoker = uid.clone();
        let revoker = tokio::spawn(async move {
            entered.notified().await;
            {
                let conn = lock_for_revoker.lock().await;
                conn.execute(
                    "UPDATE users SET is_sysadmin = 0 WHERE user_id = ?1",
                    [&uid_for_revoker],
                )
                .unwrap();
            }
            release.notify_one();
        });

        let (_unit, revalidate_result) = revalidate_after(awaitable, &lock, &spec).await;
        revoker.await.unwrap();

        let resp = revalidate_result.unwrap_err();
        assert_eq!(resp.status, 403);
    }

    /// R8-F3 parity check (test_sec_r8f3_project_membership_toctou.py):
    /// R8-F3's whole finding, in Python, was that R7-F1's revalidation
    /// fix re-checked ONLY the capability half after a yield point,
    /// never the MEMBERSHIP half -- because Python composed two
    /// SEPARATE re-check calls and only fixed one of them. This crate's
    /// `revalidate()` never had that seam to begin with: `spec.project.
    /// is_some()` always routes through `revalidate_capability_and_
    /// membership`, a SINGLE call that re-derives capability AND
    /// membership-rank together, so there is no "capability-only"
    /// re-check to have missed the membership half in the first place.
    /// This test proves that structural claim under the SAME genuine-
    /// concurrency shape as the capability sibling above -- a real
    /// second task revokes the caller's PROJECT MEMBERSHIP (capability
    /// left fully intact, via a real group grant) while `revalidate_
    /// after`'s paused awaitable is still in flight.
    #[tokio::test]
    async fn revalidate_after_catches_a_membership_revocation_that_lands_during_a_real_concurrent_await(
    ) {
        let (_dir, c, db) = conn_with_sea_orm().await;
        // A non-sysadmin operator whose ONLY source of `system.projects.
        // manage` is a group grant (capability stays untouched by the
        // race below) plus a real `operator`-tier membership row on
        // "proj-a" (what the race revokes).
        seed_sysadmin(&db, "root").await; // sentinel first user (bootstrap sysadmin), irrelevant otherwise
        let bob = crate::identity::create_user(
            &db,
            "bob",
            "correct horse battery staple",
            None,
            false,
            false,
            &[],
            NOW,
        )
        .await
        .unwrap();
        c.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) VALUES ('g1', 'g1', 0, ?1)",
            [NOW],
        )
        .unwrap();
        c.execute(
            "INSERT INTO group_capability (group_id, capability) VALUES ('g1', 'system.projects.manage')",
            [],
        )
        .unwrap();
        c.execute(
            "INSERT INTO group_membership (group_id, member_user_id, added_at) VALUES ('g1', ?1, ?2)",
            [&bob, NOW],
        )
        .unwrap();
        c.execute(
            "INSERT INTO project_membership (project_name, user_id, role) VALUES ('proj-a', ?1, 'operator')",
            [&bob],
        )
        .unwrap();

        let lock = std::sync::Arc::new(AsyncMutex::new(c));
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

        let entered = std::sync::Arc::new(tokio::sync::Notify::new());
        let release = std::sync::Arc::new(tokio::sync::Notify::new());
        let entered_in_awaitable = entered.clone();
        let release_in_awaitable = release.clone();
        let awaitable = async move {
            entered_in_awaitable.notify_one();
            release_in_awaitable.notified().await;
        };

        let lock_for_revoker = lock.clone();
        let bob_for_revoker = bob.clone();
        let revoker = tokio::spawn(async move {
            entered.notified().await;
            {
                let conn = lock_for_revoker.lock().await;
                // Membership-ONLY strip -- the group capability grant
                // above is left completely untouched.
                conn.execute(
                    "DELETE FROM project_membership WHERE project_name = 'proj-a' AND user_id = ?1",
                    [&bob_for_revoker],
                )
                .unwrap();
            }
            release.notify_one();
        });

        let (_unit, revalidate_result) = revalidate_after(awaitable, &lock, &spec).await;
        revoker.await.unwrap();

        // Found-and-fixed bug (this PR): bob's membership row is
        // GONE post-race (`resolve_user_project_role` -> None), the
        // SAME "role is None" shape `deny_cross_tenant_project_read`
        // closes with a uniform 404 `not_found` -- not the 403 this
        // used to assert (see `perm_gates::not_found`'s own doc).
        let resp = revalidate_result.unwrap_err();
        assert_eq!(resp.status, 404);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON");
        };
        assert_eq!(body["error"], "not_found");
    }
}
