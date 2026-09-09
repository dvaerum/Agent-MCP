//! OIDC group-claim -> agent-mcp group mapping + de-provisioning
//! reconciliation. Port target: `agent_mcp/router/sso.py`'s
//! `apply_group_mapping`/`reconcile_oidc_group_membership` (Phase E2
//! PR22 step 4/8, `conexus-router-oidc-group-mapping`).
//!
//! Every DB primitive this needs already exists in
//! `conexus_db::group_membership_repository` (`ensure_group`,
//! `add_group_member`, `remove_group_member`,
//! `user_group_memberships_by_name_prefix` -- the last one's own doc
//! comment already names "the SSO OIDC `oidc:`-namespaced group
//! reconcile scope" as its intended consumer, confirming this is the
//! right fn to call) plus one new addition this PR needs:
//! `is_direct_user_member` (the idempotent-add pre-check --
//! `group_membership` has a real UNIQUE index on `(group_id,
//! member_user_id)`, so a naive re-INSERT on an already-existing edge
//! would hit a constraint violation instead of silently no-op'ing).
//!
//! **Namespace scoping is the whole safety property**: only groups
//! under the reserved `oidc:` prefix are ever revoked by
//! [`reconcile_oidc_group_membership`]. `group_membership` carries no
//! per-row provenance column, so at the row level an IdP-derived
//! grant is indistinguishable from a manual admin grant -- the
//! `oidc:` prefix is the ONLY unambiguous IdP-sourced marker (those
//! groups are provisioned exclusively by this module's own wildcard-
//! JIT path), so it's the only namespace ever revoked. An operator's
//! manual grant, and an explicit-mapping target group (an arbitrary
//! local slug an operator bound a claim to), are both left
//! additive-only -- an SSO login can never remove either.
//!
//! **Phase G (sea-orm migration, router step 4 PR G)**: both public
//! functions converted to `sea_orm::DatabaseConnection` -- found via
//! this PR's own "verify against the real current files" mandate as a
//! real caller not in the originally-handed-in inventory. Their one
//! real caller, `oidc_handlers.rs`'s ID-token handler, is already
//! async; PR D already established that SAME handler's OTHER call
//! (`find_or_create_oidc_user`) has no hot-path-sync constraint, and
//! this pair of calls has none either -- they run sequentially before
//! (still out-of-scope, rusqlite-based) `identity::create_session`,
//! with no shared transaction between the two steps to preserve.

use std::collections::{HashMap, HashSet};

use conexus_db::group_membership_repository as repo;
use sea_orm::DatabaseConnection;

use crate::sso::sanitise_username;

/// Groups JIT-provisioned by the wildcard mapping entry live under
/// this reserved prefix -- the sole marker that distinguishes an
/// IdP-sourced grant from a manually-managed one. Port of
/// `_WILDCARD_GROUP_PREFIX`.
pub const WILDCARD_GROUP_PREFIX: &str = "oidc:";

/// Port of `_sanitise_group_name` -- "same shape as a username;
/// groups share the slug convention" (the real Python docstring, kept
/// verbatim). Reuses `sso::sanitise_username` directly rather than a
/// second copy of the identical slugifier.
fn sanitise_group_name(raw: &str) -> String {
    sanitise_username(raw)
}

/// The agent-mcp group name a single claim maps to, or `None` if the
/// claim is unmapped and there's no wildcard entry. Shared by
/// [`apply_group_mapping`] and [`mapped_group_names`] so the two
/// can't drift apart on what "maps to" means.
fn mapped_name(
    claim: &str,
    mapping: &HashMap<String, String>,
    wildcard: Option<&str>,
) -> Option<String> {
    if let Some(target) = mapping.get(claim) {
        if !target.is_empty() {
            return Some(target.clone());
        }
        return None;
    }
    wildcard.map(|_| format!("{WILDCARD_GROUP_PREFIX}{}", sanitise_group_name(claim)))
}

/// Port of `_mapped_group_names`: the FULL set of agent-mcp group
/// names the current claims map to, regardless of whether the user is
/// already a member. Used by the de-provisioning reconciler to
/// compute which IdP-managed memberships the current claim still
/// justifies.
fn mapped_group_names(
    group_claims: &[String],
    mapping: &HashMap<String, String>,
) -> HashSet<String> {
    let wildcard = mapping.get("*").map(String::as_str);
    group_claims
        .iter()
        .filter_map(|claim| mapped_name(claim, mapping, wildcard))
        .collect()
}

/// Port of `apply_group_mapping`. Maps OIDC group claims to agent-mcp
/// groups; returns the group names the user was newly added to.
///
/// Idempotent: re-running with the same claims is a no-op for
/// `group_membership` rows that already exist. A DB error on any one
/// claim degrades that claim to "silently skipped" (matching Python's
/// own `except sqlite3.OperationalError: return False/None` posture
/// on a backlevel deploy whose groups tables haven't migrated in yet)
/// rather than aborting the whole login on a partial-schema gap.
pub async fn apply_group_mapping(
    db: &DatabaseConnection,
    user_id: &str,
    group_claims: &[String],
    mapping: &HashMap<String, String>,
    now: &str,
) -> HashSet<String> {
    let wildcard = mapping.get("*").map(String::as_str);
    let mut added = HashSet::new();

    for claim in group_claims {
        let Some(group_name) = mapped_name(claim, mapping, wildcard) else {
            continue;
        };
        let Ok(group_id) = repo::ensure_group(db, &group_name).await else {
            continue;
        };
        let Ok(already_member) = repo::is_direct_user_member(db, &group_id, user_id).await else {
            continue;
        };
        if already_member {
            continue;
        }
        if repo::add_group_member(db, &group_id, Some(user_id), None, now)
            .await
            .is_ok()
        {
            added.insert(group_name);
        }
    }
    added
}

/// Port of `reconcile_oidc_group_membership`: revoke IdP-managed
/// (`oidc:`-namespaced) group memberships the current claim no longer
/// justifies; return the group names removed.
///
/// De-provisioning counterpart to [`apply_group_mapping`] (that one
/// is additive-only, so a user dropped from an IdP group would
/// otherwise keep the local `group_membership` row -- and, since
/// group-resolution derives sysadmin/project-role transitively from
/// those rows, keep the privilege indefinitely).
pub async fn reconcile_oidc_group_membership(
    db: &DatabaseConnection,
    user_id: &str,
    group_claims: &[String],
    mapping: &HashMap<String, String>,
) -> HashSet<String> {
    let claimed = mapped_group_names(group_claims, mapping);
    let claimed_oidc: HashSet<&str> = claimed
        .iter()
        .filter(|n| n.starts_with(WILDCARD_GROUP_PREFIX))
        .map(String::as_str)
        .collect();

    let Ok(current_oidc) =
        repo::user_group_memberships_by_name_prefix(db, user_id, WILDCARD_GROUP_PREFIX).await
    else {
        return HashSet::new();
    };

    let mut removed = HashSet::new();
    for (name, group_id) in current_oidc {
        if claimed_oidc.contains(name.as_str()) {
            continue;
        }
        if repo::remove_group_member(db, &group_id, user_id)
            .await
            .unwrap_or(false)
        {
            removed.insert(name);
        }
    }
    removed
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_db::schema::init_router_schema;

    const NOW: &str = "2026-09-06T00:00:00Z";

    /// A file-backed router DB, sea-orm only -- every DB primitive
    /// this module's own functions now call is async/sea-orm, so
    /// there's nothing left in this test module that needs a live
    /// rusqlite handle after schema init/seeding.
    async fn db() -> (tempfile::TempDir, DatabaseConnection) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("oidc_group_mapping_test.db");
        {
            let c = rusqlite::Connection::open(&path).unwrap();
            init_router_schema(&c).unwrap();
            c.execute(
                "INSERT INTO users (user_id, username, created_at) VALUES ('u1', 'alice', ?1)",
                [NOW],
            )
            .unwrap();
        }
        let db = sea_orm::Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        (dir, db)
    }

    fn mapping(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    fn claims(names: &[&str]) -> Vec<String> {
        names.iter().map(|s| s.to_string()).collect()
    }

    async fn user_group_names(db: &DatabaseConnection, user_id: &str) -> HashSet<String> {
        repo::user_group_memberships_by_name_prefix(db, user_id, "")
            .await
            .unwrap()
            .into_keys()
            .collect()
    }

    #[tokio::test]
    async fn an_explicit_mapping_adds_the_user_to_the_named_local_group() {
        let (_dir, db) = db().await;
        let map = mapping(&[("admins", "admins")]);
        let added = apply_group_mapping(&db, "u1", &claims(&["admins"]), &map, NOW).await;
        assert_eq!(added, HashSet::from(["admins".to_string()]));
        assert!(user_group_names(&db, "u1").await.contains("admins"));
    }

    #[tokio::test]
    async fn an_unmapped_claim_with_no_wildcard_is_silently_ignored() {
        let (_dir, db) = db().await;
        let map = mapping(&[("admins", "admins")]);
        let added = apply_group_mapping(&db, "u1", &claims(&["engineers"]), &map, NOW).await;
        assert!(added.is_empty());
        assert!(user_group_names(&db, "u1").await.is_empty());
    }

    #[tokio::test]
    async fn the_wildcard_jit_creates_a_namespaced_group_for_an_unmapped_claim() {
        let (_dir, db) = db().await;
        let map = mapping(&[("*", "*")]);
        let added = apply_group_mapping(&db, "u1", &claims(&["Site Admins!"]), &map, NOW).await;
        assert_eq!(added, HashSet::from(["oidc:site-admins".to_string()]));
    }

    #[tokio::test]
    async fn an_explicit_mapping_takes_priority_over_the_wildcard() {
        let (_dir, db) = db().await;
        let map = mapping(&[("admins", "admins"), ("*", "*")]);
        let added = apply_group_mapping(&db, "u1", &claims(&["admins"]), &map, NOW).await;
        // Not `oidc:admins` -- the explicit target wins, matching
        // Python's own `target = mapping.get(claim); if target: ...`
        // precedence.
        assert_eq!(added, HashSet::from(["admins".to_string()]));
    }

    #[tokio::test]
    async fn a_second_call_with_the_same_claims_is_idempotent() {
        let (_dir, db) = db().await;
        let map = mapping(&[("*", "*")]);
        apply_group_mapping(&db, "u1", &claims(&["engineers"]), &map, NOW).await;
        let second = apply_group_mapping(&db, "u1", &claims(&["engineers"]), &map, NOW).await;
        // Already a member -- nothing NEWLY added the second time.
        assert!(second.is_empty());
        assert_eq!(
            user_group_names(&db, "u1").await,
            HashSet::from(["oidc:engineers".to_string()])
        );
    }

    #[tokio::test]
    async fn reconcile_revokes_an_oidc_group_no_longer_claimed() {
        let (_dir, db) = db().await;
        let map = mapping(&[("*", "*")]);
        apply_group_mapping(&db, "u1", &claims(&["engineers", "admins"]), &map, NOW).await;

        let removed =
            reconcile_oidc_group_membership(&db, "u1", &claims(&["engineers"]), &map).await;

        assert_eq!(removed, HashSet::from(["oidc:admins".to_string()]));
        let remaining = user_group_names(&db, "u1").await;
        assert!(remaining.contains("oidc:engineers"));
        assert!(!remaining.contains("oidc:admins"));
    }

    #[tokio::test]
    async fn reconcile_never_touches_a_manually_managed_group() {
        // A local, non-`oidc:`-namespaced group must survive
        // reconciliation even when nothing in the current claim set
        // justifies it -- an SSO login must never undo a manual grant.
        let (_dir, db) = db().await;
        let group_id = repo::ensure_group(&db, "trusted-operators").await.unwrap();
        repo::add_group_member(&db, &group_id, Some("u1"), None, NOW)
            .await
            .unwrap();

        let map = mapping(&[("*", "*")]);
        let removed = reconcile_oidc_group_membership(&db, "u1", &claims(&[]), &map).await;

        assert!(removed.is_empty());
        assert!(user_group_names(&db, "u1")
            .await
            .contains("trusted-operators"));
    }

    #[tokio::test]
    async fn reconcile_never_touches_an_explicit_mapping_target_even_when_unclaimed() {
        // An explicit-mapping target group (an arbitrary local slug an
        // operator bound a claim to) is left additive-only, same as a
        // fully manual grant -- only `oidc:`-namespaced wildcard
        // groups are ever revoked.
        let (_dir, db) = db().await;
        let map = mapping(&[("admins", "admins")]);
        apply_group_mapping(&db, "u1", &claims(&["admins"]), &map, NOW).await;

        let removed = reconcile_oidc_group_membership(&db, "u1", &claims(&[]), &map).await;

        assert!(removed.is_empty());
        assert!(user_group_names(&db, "u1").await.contains("admins"));
    }

    #[tokio::test]
    async fn reconcile_is_idempotent_when_the_claim_set_is_unchanged() {
        let (_dir, db) = db().await;
        let map = mapping(&[("*", "*")]);
        apply_group_mapping(&db, "u1", &claims(&["engineers"]), &map, NOW).await;
        let removed =
            reconcile_oidc_group_membership(&db, "u1", &claims(&["engineers"]), &map).await;
        assert!(removed.is_empty());
    }
}
