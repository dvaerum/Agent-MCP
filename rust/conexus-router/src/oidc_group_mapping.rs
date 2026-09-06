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
#![allow(dead_code)]

use std::collections::{HashMap, HashSet};

use conexus_db::group_membership_repository as repo;
use rusqlite::Connection;

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
pub fn apply_group_mapping(
    conn: &Connection,
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
        let Ok(group_id) = repo::ensure_group(conn, &group_name) else {
            continue;
        };
        let Ok(already_member) = repo::is_direct_user_member(conn, &group_id, user_id) else {
            continue;
        };
        if already_member {
            continue;
        }
        if repo::add_group_member(conn, &group_id, Some(user_id), None, now).is_ok() {
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
pub fn reconcile_oidc_group_membership(
    conn: &Connection,
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
        repo::user_group_memberships_by_name_prefix(conn, user_id, WILDCARD_GROUP_PREFIX)
    else {
        return HashSet::new();
    };

    let mut removed = HashSet::new();
    for (name, group_id) in current_oidc {
        if claimed_oidc.contains(name.as_str()) {
            continue;
        }
        if repo::remove_group_member(conn, &group_id, user_id).unwrap_or(false) {
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

    fn conn() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        init_router_schema(&c).unwrap();
        c.execute(
            "INSERT INTO users (user_id, username, created_at) VALUES ('u1', 'alice', ?1)",
            [NOW],
        )
        .unwrap();
        c
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

    fn user_group_names(c: &Connection, user_id: &str) -> HashSet<String> {
        repo::user_group_memberships_by_name_prefix(c, user_id, "")
            .unwrap()
            .into_keys()
            .collect()
    }

    #[test]
    fn an_explicit_mapping_adds_the_user_to_the_named_local_group() {
        let c = conn();
        let map = mapping(&[("admins", "admins")]);
        let added = apply_group_mapping(&c, "u1", &claims(&["admins"]), &map, NOW);
        assert_eq!(added, HashSet::from(["admins".to_string()]));
        assert!(user_group_names(&c, "u1").contains("admins"));
    }

    #[test]
    fn an_unmapped_claim_with_no_wildcard_is_silently_ignored() {
        let c = conn();
        let map = mapping(&[("admins", "admins")]);
        let added = apply_group_mapping(&c, "u1", &claims(&["engineers"]), &map, NOW);
        assert!(added.is_empty());
        assert!(user_group_names(&c, "u1").is_empty());
    }

    #[test]
    fn the_wildcard_jit_creates_a_namespaced_group_for_an_unmapped_claim() {
        let c = conn();
        let map = mapping(&[("*", "*")]);
        let added = apply_group_mapping(&c, "u1", &claims(&["Site Admins!"]), &map, NOW);
        assert_eq!(added, HashSet::from(["oidc:site-admins".to_string()]));
    }

    #[test]
    fn an_explicit_mapping_takes_priority_over_the_wildcard() {
        let c = conn();
        let map = mapping(&[("admins", "admins"), ("*", "*")]);
        let added = apply_group_mapping(&c, "u1", &claims(&["admins"]), &map, NOW);
        // Not `oidc:admins` -- the explicit target wins, matching
        // Python's own `target = mapping.get(claim); if target: ...`
        // precedence.
        assert_eq!(added, HashSet::from(["admins".to_string()]));
    }

    #[test]
    fn a_second_call_with_the_same_claims_is_idempotent() {
        let c = conn();
        let map = mapping(&[("*", "*")]);
        apply_group_mapping(&c, "u1", &claims(&["engineers"]), &map, NOW);
        let second = apply_group_mapping(&c, "u1", &claims(&["engineers"]), &map, NOW);
        // Already a member -- nothing NEWLY added the second time.
        assert!(second.is_empty());
        assert_eq!(
            user_group_names(&c, "u1"),
            HashSet::from(["oidc:engineers".to_string()])
        );
    }

    #[test]
    fn reconcile_revokes_an_oidc_group_no_longer_claimed() {
        let c = conn();
        let map = mapping(&[("*", "*")]);
        apply_group_mapping(&c, "u1", &claims(&["engineers", "admins"]), &map, NOW);

        let removed = reconcile_oidc_group_membership(&c, "u1", &claims(&["engineers"]), &map);

        assert_eq!(removed, HashSet::from(["oidc:admins".to_string()]));
        let remaining = user_group_names(&c, "u1");
        assert!(remaining.contains("oidc:engineers"));
        assert!(!remaining.contains("oidc:admins"));
    }

    #[test]
    fn reconcile_never_touches_a_manually_managed_group() {
        // A local, non-`oidc:`-namespaced group must survive
        // reconciliation even when nothing in the current claim set
        // justifies it -- an SSO login must never undo a manual grant.
        let c = conn();
        let group_id = repo::ensure_group(&c, "trusted-operators").unwrap();
        repo::add_group_member(&c, &group_id, Some("u1"), None, NOW).unwrap();

        let map = mapping(&[("*", "*")]);
        let removed = reconcile_oidc_group_membership(&c, "u1", &claims(&[]), &map);

        assert!(removed.is_empty());
        assert!(user_group_names(&c, "u1").contains("trusted-operators"));
    }

    #[test]
    fn reconcile_never_touches_an_explicit_mapping_target_even_when_unclaimed() {
        // An explicit-mapping target group (an arbitrary local slug an
        // operator bound a claim to) is left additive-only, same as a
        // fully manual grant -- only `oidc:`-namespaced wildcard
        // groups are ever revoked.
        let c = conn();
        let map = mapping(&[("admins", "admins")]);
        apply_group_mapping(&c, "u1", &claims(&["admins"]), &map, NOW);

        let removed = reconcile_oidc_group_membership(&c, "u1", &claims(&[]), &map);

        assert!(removed.is_empty());
        assert!(user_group_names(&c, "u1").contains("admins"));
    }

    #[test]
    fn reconcile_is_idempotent_when_the_claim_set_is_unchanged() {
        let c = conn();
        let map = mapping(&[("*", "*")]);
        apply_group_mapping(&c, "u1", &claims(&["engineers"]), &map, NOW);
        let removed = reconcile_oidc_group_membership(&c, "u1", &claims(&["engineers"]), &map);
        assert!(removed.is_empty());
    }
}
