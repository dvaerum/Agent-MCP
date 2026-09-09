//! Decision functions for `admin_users_api.py`'s group-capabilities
//! handlers (`list_group_capabilities_handler`/
//! `replace_group_capabilities_handler`). Phase E2,
//! `conexus-router-admin-users-crud` (research item 8 of 10) --
//! composes `admin_users_gate.rs`'s amplification guard
//! (`caps_caller_lacks`/`forbid_cap_amplification`) with the already-
//! ported `conexus-db::group_capability_repository`.
//!
//! Framework-agnostic, matching every other decision-function module
//! this phase -- real axum route registration and the async
//! body-read yield point stay deferred to PR 23.
//!
//! **Phase G (sea-orm migration, router step 4 PR G)**: converted
//! wholesale to `sea_orm::DatabaseConnection` -- this module's own
//! call-site classification found no function here opens a rusqlite
//! transaction (a read-then-conditionally-write sequence, but never a
//! `BEGIN IMMEDIATE`), so there was no forced-sync constraint to
//! preserve a twin for; `group_membership_repository::get_group`/
//! `group_capability_repository::fetch`/`replace`'s own async primaries
//! (added in this same PR) serve every function here directly.

#![allow(dead_code)]

use std::collections::HashSet;

use conexus_core::capability::Capability;
use conexus_core::principal::Principal;
use conexus_db::group_capability_repository;
use conexus_db::group_membership_repository;
use sea_orm::DatabaseConnection;

use crate::admin_users_gate::{self, AdminUsersError};
use crate::mcp_handler::HandlerResponse;

fn not_found(group_id: &str) -> HandlerResponse {
    admin_users_gate::error_envelope(
        AdminUsersError::NotFound,
        &format!("unknown group_id: {group_id:?}"),
        None,
    )
}

async fn group_exists(db: &DatabaseConnection, group_id: &str) -> Result<bool, sea_orm::DbErr> {
    Ok(group_membership_repository::get_group(db, group_id)
        .await?
        .is_some())
}

async fn sorted_caps(
    db: &DatabaseConnection,
    group_id: &str,
) -> Result<Vec<String>, sea_orm::DbErr> {
    let caps = group_capability_repository::fetch(db, group_id).await?;
    let mut out: Vec<String> = caps.into_iter().collect();
    out.sort();
    Ok(out)
}

#[derive(Debug)]
pub enum ListGroupCapabilitiesOutcome {
    Found(Vec<String>),
    Rejected(HandlerResponse),
}

/// Port of `list_group_capabilities_handler`.
pub async fn decide_list_group_capabilities(
    db: &DatabaseConnection,
    group_id: &str,
) -> Result<ListGroupCapabilitiesOutcome, sea_orm::DbErr> {
    if !group_exists(db, group_id).await? {
        return Ok(ListGroupCapabilitiesOutcome::Rejected(not_found(group_id)));
    }
    Ok(ListGroupCapabilitiesOutcome::Found(
        sorted_caps(db, group_id).await?,
    ))
}

#[derive(Debug)]
pub enum ReplaceGroupCapabilitiesOutcome {
    Replaced(Vec<String>),
    Rejected(HandlerResponse),
}

fn validation_rejected(message: &str) -> HandlerResponse {
    admin_users_gate::error_envelope(AdminUsersError::Validation, message, None)
}

/// Port of `replace_group_capabilities_handler`. Argument extraction/
/// validation order matches the real Python source exactly --
/// non-list body, non-string entries, unknown capability strings,
/// resource-tier (non-`system.*`) rejection (SEC R2-F3), and finally
/// the SYMMETRIC-DIFFERENCE amplification guard (AZ-R12-1: a
/// shrinking PUT revokes caps too, so both added AND removed caps
/// must be within the caller's own held set unless they're a real
/// sysadmin).
pub async fn decide_replace_group_capabilities(
    db: &DatabaseConnection,
    group_id: &str,
    caller_is_sysadmin: bool,
    caller_username: &str,
    caller_principal: Option<&Principal>,
    raw_body: &serde_json::Value,
) -> Result<ReplaceGroupCapabilitiesOutcome, sea_orm::DbErr> {
    if !group_exists(db, group_id).await? {
        return Ok(ReplaceGroupCapabilitiesOutcome::Rejected(not_found(
            group_id,
        )));
    }

    let Some(raw_caps) = raw_body.get("capabilities").and_then(|v| v.as_array()) else {
        return Ok(ReplaceGroupCapabilitiesOutcome::Rejected(
            validation_rejected("body must be {\"capabilities\": [...]} with a JSON array"),
        ));
    };

    // Validate types + drop duplicates, preserving caller order so the
    // unknown-cap error message quotes the first offender in the
    // order the operator typed them.
    let mut seen: HashSet<String> = HashSet::new();
    let mut ordered: Vec<String> = Vec::new();
    for entry in raw_caps {
        let Some(s) = entry.as_str() else {
            let type_name = match entry {
                serde_json::Value::Null => "NoneType",
                serde_json::Value::Bool(_) => "bool",
                serde_json::Value::Number(_) => "number",
                serde_json::Value::Array(_) => "list",
                serde_json::Value::Object(_) => "dict",
                serde_json::Value::String(_) => unreachable!(),
            };
            return Ok(ReplaceGroupCapabilitiesOutcome::Rejected(
                validation_rejected(&format!(
                    "capabilities entries must be strings; got {type_name}"
                )),
            ));
        };
        if seen.insert(s.to_string()) {
            ordered.push(s.to_string());
        }
    }

    let unknown: Vec<String> = ordered
        .iter()
        .filter(|c| c.parse::<Capability>().is_err())
        .cloned()
        .collect();
    if !unknown.is_empty() {
        let quoted = unknown
            .iter()
            .map(|c| format!("{c:?}"))
            .collect::<Vec<_>>()
            .join(", ");
        return Ok(ReplaceGroupCapabilitiesOutcome::Rejected(
            admin_users_gate::error_envelope(
                AdminUsersError::UnknownCapability,
                &format!("unknown capability string(s): {quoted}"),
                Some(serde_json::json!({"unknown": unknown})),
            ),
        ));
    }

    // SEC R2-F3: group_capability has no project_name column, so a
    // resource-tier grant here would be global across every project
    // the caller can reach -- fail loud rather than accept a grant
    // that silently becomes a no-op downstream (resource_capabilities
    // are only admitted from project_membership.role, not this
    // table).
    let non_system: Vec<String> = ordered
        .iter()
        .filter(|c| !c.starts_with("system."))
        .cloned()
        .collect();
    if !non_system.is_empty() {
        let quoted = non_system
            .iter()
            .map(|c| format!("{c:?}"))
            .collect::<Vec<_>>()
            .join(", ");
        return Ok(ReplaceGroupCapabilitiesOutcome::Rejected(
            admin_users_gate::error_envelope(
                AdminUsersError::ResourceCapabilityNotDelegableToGroup,
                &format!(
                    "group_capability grants are global (no project scope) and may only \
                     carry system.* capabilities; resource-tier capability string(s) \
                     {quoted} must be granted via project_membership.role instead"
                ),
                Some(serde_json::json!({"non_system": non_system})),
            ),
        ));
    }

    // AZ-1/AZ-R12-1: the amplification guard covers the SYMMETRIC
    // DIFFERENCE (added ∪ removed), not just the new list -- this is
    // an atomic REPLACE, so a shrinking PUT revokes caps too, and a
    // non-sysadmin must not strip authority they don't themselves
    // hold any more than they may grant it.
    let current = group_capability_repository::fetch(db, group_id).await?;
    let new_caps: HashSet<String> = ordered.iter().cloned().collect();
    let delta: Vec<String> = new_caps.symmetric_difference(&current).cloned().collect();
    let lacked = admin_users_gate::caps_caller_lacks(caller_is_sysadmin, caller_principal, &delta);
    if !lacked.is_empty() {
        return Ok(ReplaceGroupCapabilitiesOutcome::Rejected(
            admin_users_gate::forbid_cap_amplification(caller_username, &lacked),
        ));
    }

    group_capability_repository::replace(db, group_id, ordered.iter().map(String::as_str)).await?;
    Ok(ReplaceGroupCapabilitiesOutcome::Replaced(
        sorted_caps(db, group_id).await?,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_db::schema::init_router_schema;

    const NOW: &str = "2026-01-01T00:00:00.000+00:00";

    /// A file-backed router DB -- everything in this module is
    /// async/sea-orm now, but schema init still goes through rusqlite
    /// (`init_router_schema`), so a dropped `rusqlite::Connection` is
    /// opened just long enough to run it before handing back the
    /// sea-orm handle.
    async fn db() -> (tempfile::TempDir, DatabaseConnection) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("admin_group_capabilities_test.db");
        {
            let c = rusqlite::Connection::open(&path).unwrap();
            c.execute_batch("PRAGMA foreign_keys = ON;").unwrap();
            init_router_schema(&c).unwrap();
        }
        let db = sea_orm::Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        (dir, db)
    }

    async fn seed_group(db: &DatabaseConnection, name: &str) -> String {
        group_membership_repository::create_group(db, name, false, NOW)
            .await
            .unwrap()
            .group_id
    }

    // -- decide_list_group_capabilities --------------------------------

    #[tokio::test]
    async fn lists_sorted_capabilities() {
        let (_dir, db) = db().await;
        let gid = seed_group(&db, "engineers").await;
        group_capability_repository::replace(
            &db,
            &gid,
            ["system.projects.manage", "system.config.write"],
        )
        .await
        .unwrap();
        let outcome = decide_list_group_capabilities(&db, &gid).await.unwrap();
        let ListGroupCapabilitiesOutcome::Found(caps) = outcome else {
            panic!("expected Found, got {outcome:?}");
        };
        assert_eq!(caps, vec!["system.config.write", "system.projects.manage"]);
    }

    #[tokio::test]
    async fn rejects_listing_an_unknown_group() {
        let (_dir, db) = db().await;
        let outcome = decide_list_group_capabilities(&db, "nope").await.unwrap();
        let ListGroupCapabilitiesOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 404);
    }

    // -- decide_replace_group_capabilities ------------------------------

    #[tokio::test]
    async fn a_sysadmin_replaces_the_full_capability_set() {
        let (_dir, db) = db().await;
        let gid = seed_group(&db, "engineers").await;
        let outcome = decide_replace_group_capabilities(
            &db,
            &gid,
            true,
            "admin",
            None,
            &serde_json::json!({"capabilities": ["system.config.write"]}),
        )
        .await
        .unwrap();
        let ReplaceGroupCapabilitiesOutcome::Replaced(caps) = outcome else {
            panic!("expected Replaced, got {outcome:?}");
        };
        assert_eq!(caps, vec!["system.config.write"]);
    }

    #[tokio::test]
    async fn rejects_replacing_on_an_unknown_group() {
        let (_dir, db) = db().await;
        let outcome = decide_replace_group_capabilities(
            &db,
            "nope",
            true,
            "admin",
            None,
            &serde_json::json!({"capabilities": []}),
        )
        .await
        .unwrap();
        let ReplaceGroupCapabilitiesOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 404);
    }

    #[tokio::test]
    async fn rejects_a_non_array_body() {
        let (_dir, db) = db().await;
        let gid = seed_group(&db, "engineers").await;
        let outcome = decide_replace_group_capabilities(
            &db,
            &gid,
            true,
            "admin",
            None,
            &serde_json::json!({"capabilities": "not-a-list"}),
        )
        .await
        .unwrap();
        let ReplaceGroupCapabilitiesOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 400);
    }

    #[tokio::test]
    async fn rejects_a_non_string_entry() {
        let (_dir, db) = db().await;
        let gid = seed_group(&db, "engineers").await;
        let outcome = decide_replace_group_capabilities(
            &db,
            &gid,
            true,
            "admin",
            None,
            &serde_json::json!({"capabilities": [42]}),
        )
        .await
        .unwrap();
        let ReplaceGroupCapabilitiesOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 400);
    }

    #[tokio::test]
    async fn rejects_an_unknown_capability_string() {
        let (_dir, db) = db().await;
        let gid = seed_group(&db, "engineers").await;
        let outcome = decide_replace_group_capabilities(
            &db,
            &gid,
            true,
            "admin",
            None,
            &serde_json::json!({"capabilities": ["system.not.a.real.cap"]}),
        )
        .await
        .unwrap();
        let ReplaceGroupCapabilitiesOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected, got {outcome:?}");
        };
        assert_eq!(resp.status, 400);
        let crate::mcp_handler::HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON");
        };
        assert_eq!(body["error"], "unknown_capability");
    }

    #[tokio::test]
    async fn rejects_a_resource_tier_capability() {
        let (_dir, db) = db().await;
        let gid = seed_group(&db, "engineers").await;
        let outcome = decide_replace_group_capabilities(
            &db,
            &gid,
            true,
            "admin",
            None,
            &serde_json::json!({"capabilities": ["tasks.create"]}),
        )
        .await
        .unwrap();
        let ReplaceGroupCapabilitiesOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected, got {outcome:?}");
        };
        assert_eq!(resp.status, 400);
        let crate::mcp_handler::HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON");
        };
        assert_eq!(body["error"], "resource_capability_not_delegable_to_group");
    }

    #[tokio::test]
    async fn a_non_sysadmin_cannot_grant_a_capability_they_lack() {
        let (_dir, db) = db().await;
        let gid = seed_group(&db, "engineers").await;
        let outcome = decide_replace_group_capabilities(
            &db,
            &gid,
            false,
            "bob",
            None, // no principal at all -- fails closed
            &serde_json::json!({"capabilities": ["system.config.write"]}),
        )
        .await
        .unwrap();
        let ReplaceGroupCapabilitiesOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected, got {outcome:?}");
        };
        assert_eq!(resp.status, 403);
    }

    #[tokio::test]
    async fn a_non_sysadmin_cannot_revoke_a_capability_they_lack_either() {
        // AZ-R12-1: a shrinking PUT (here, an empty list) removes the
        // existing cap -- that's a REVOKE, and the caller must hold
        // the cap being revoked just like a grant.
        let (_dir, db) = db().await;
        let gid = seed_group(&db, "engineers").await;
        group_capability_repository::replace(&db, &gid, ["system.config.write"])
            .await
            .unwrap();
        let outcome = decide_replace_group_capabilities(
            &db,
            &gid,
            false,
            "bob",
            None,
            &serde_json::json!({"capabilities": []}),
        )
        .await
        .unwrap();
        let ReplaceGroupCapabilitiesOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected, got {outcome:?}");
        };
        assert_eq!(resp.status, 403);
        // Confirm the reject actually left the group's caps untouched.
        assert_eq!(
            group_capability_repository::fetch(&db, &gid)
                .await
                .unwrap()
                .len(),
            1
        );
    }

    #[tokio::test]
    async fn a_non_sysadmin_can_grant_a_capability_they_themselves_hold() {
        // test_sec_r4_cap_amplification.py's
        // `test_delegated_caps_manager_can_grant_held_cap`: the guard
        // only blocks amplification beyond the caller's own authority
        // -- granting a cap the caller ALREADY holds must succeed.
        use conexus_core::capability::Capabilities;
        use conexus_core::principal::PrincipalKind;
        let (_dir, db) = db().await;
        let gid = seed_group(&db, "engineers").await;
        let principal = Principal {
            kind: PrincipalKind::OperatorSession,
            user_id: Some("bob".to_string()),
            agent_id: None,
            project_name: None,
            project_role: None,
            agent_role: None,
            can_wake_loop: false,
            source_token: None,
            capabilities: Capabilities::Set(HashSet::from([
                Capability::SystemGroupsCapabilitiesManage,
            ])),
        };
        let outcome = decide_replace_group_capabilities(
            &db,
            &gid,
            false,
            "bob",
            Some(&principal),
            &serde_json::json!({"capabilities": ["system.groups.capabilities.manage"]}),
        )
        .await
        .unwrap();
        let ReplaceGroupCapabilitiesOutcome::Replaced(caps) = outcome else {
            panic!("expected Replaced, got {outcome:?}");
        };
        assert_eq!(caps, vec!["system.groups.capabilities.manage"]);
    }

    #[tokio::test]
    async fn a_non_sysadmin_can_strip_a_capability_they_themselves_hold() {
        // test_sec_r12_revoke_amplification.py's
        // `test_delegate_can_strip_held_cap_via_shrinking_put`: a
        // shrinking PUT that removes a cap the CALLER holds is a
        // revoke within their own authority.
        use conexus_core::capability::Capabilities;
        use conexus_core::principal::PrincipalKind;
        let (_dir, db) = db().await;
        let gid = seed_group(&db, "g-target").await;
        group_capability_repository::replace(&db, &gid, ["system.users.manage"])
            .await
            .unwrap();
        let principal = Principal {
            kind: PrincipalKind::OperatorSession,
            user_id: Some("bob".to_string()),
            agent_id: None,
            project_name: None,
            project_role: None,
            agent_role: None,
            can_wake_loop: false,
            source_token: None,
            capabilities: Capabilities::Set(HashSet::from([
                Capability::SystemGroupsCapabilitiesManage,
                Capability::SystemUsersManage,
            ])),
        };
        let outcome = decide_replace_group_capabilities(
            &db,
            &gid,
            false,
            "bob",
            Some(&principal),
            &serde_json::json!({"capabilities": []}),
        )
        .await
        .unwrap();
        let ReplaceGroupCapabilitiesOutcome::Replaced(caps) = outcome else {
            panic!("expected Replaced, got {outcome:?}");
        };
        assert!(caps.is_empty());
    }

    #[tokio::test]
    async fn duplicate_entries_are_deduped() {
        let (_dir, db) = db().await;
        let gid = seed_group(&db, "engineers").await;
        let outcome = decide_replace_group_capabilities(
            &db,
            &gid,
            true,
            "admin",
            None,
            &serde_json::json!({"capabilities": ["system.config.write", "system.config.write"]}),
        )
        .await
        .unwrap();
        let ReplaceGroupCapabilitiesOutcome::Replaced(caps) = outcome else {
            panic!("expected Replaced, got {outcome:?}");
        };
        assert_eq!(caps, vec!["system.config.write"]);
    }
}
