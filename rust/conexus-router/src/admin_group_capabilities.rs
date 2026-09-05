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

#![allow(dead_code)]

use std::collections::HashSet;

use conexus_core::capability::Capability;
use conexus_core::principal::Principal;
use conexus_db::group_capability_repository;
use conexus_db::group_membership_repository;
use rusqlite::Connection;

use crate::admin_users_gate::{self, AdminUsersError};
use crate::mcp_handler::HandlerResponse;

fn not_found(group_id: &str) -> HandlerResponse {
    admin_users_gate::error_envelope(
        AdminUsersError::NotFound,
        &format!("unknown group_id: {group_id:?}"),
        None,
    )
}

fn group_exists(conn: &Connection, group_id: &str) -> rusqlite::Result<bool> {
    Ok(group_membership_repository::get_group(conn, group_id)?.is_some())
}

fn sorted_caps(conn: &Connection, group_id: &str) -> rusqlite::Result<Vec<String>> {
    let caps = group_capability_repository::fetch(conn, group_id)?;
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
pub fn decide_list_group_capabilities(
    conn: &Connection,
    group_id: &str,
) -> rusqlite::Result<ListGroupCapabilitiesOutcome> {
    if !group_exists(conn, group_id)? {
        return Ok(ListGroupCapabilitiesOutcome::Rejected(not_found(group_id)));
    }
    Ok(ListGroupCapabilitiesOutcome::Found(sorted_caps(
        conn, group_id,
    )?))
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
pub fn decide_replace_group_capabilities(
    conn: &Connection,
    group_id: &str,
    caller_is_sysadmin: bool,
    caller_username: &str,
    caller_principal: Option<&Principal>,
    raw_body: &serde_json::Value,
) -> rusqlite::Result<ReplaceGroupCapabilitiesOutcome> {
    if !group_exists(conn, group_id)? {
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
    let current = group_capability_repository::fetch(conn, group_id)?;
    let new_caps: HashSet<String> = ordered.iter().cloned().collect();
    let delta: Vec<String> = new_caps.symmetric_difference(&current).cloned().collect();
    let lacked = admin_users_gate::caps_caller_lacks(caller_is_sysadmin, caller_principal, &delta);
    if !lacked.is_empty() {
        return Ok(ReplaceGroupCapabilitiesOutcome::Rejected(
            admin_users_gate::forbid_cap_amplification(caller_username, &lacked),
        ));
    }

    group_capability_repository::replace(conn, group_id, ordered.iter().map(String::as_str))?;
    Ok(ReplaceGroupCapabilitiesOutcome::Replaced(sorted_caps(
        conn, group_id,
    )?))
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_db::schema::init_router_schema;

    fn conn() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch("PRAGMA foreign_keys = ON;").unwrap();
        init_router_schema(&c).unwrap();
        c
    }
    const NOW: &str = "2026-01-01T00:00:00.000+00:00";

    fn seed_group(c: &Connection, name: &str) -> String {
        group_membership_repository::create_group(c, name, false, NOW)
            .unwrap()
            .group_id
    }

    // -- decide_list_group_capabilities --------------------------------

    #[test]
    fn lists_sorted_capabilities() {
        let c = conn();
        let gid = seed_group(&c, "engineers");
        group_capability_repository::replace(
            &c,
            &gid,
            ["system.projects.manage", "system.config.write"],
        )
        .unwrap();
        let outcome = decide_list_group_capabilities(&c, &gid).unwrap();
        let ListGroupCapabilitiesOutcome::Found(caps) = outcome else {
            panic!("expected Found, got {outcome:?}");
        };
        assert_eq!(caps, vec!["system.config.write", "system.projects.manage"]);
    }

    #[test]
    fn rejects_listing_an_unknown_group() {
        let c = conn();
        let outcome = decide_list_group_capabilities(&c, "nope").unwrap();
        let ListGroupCapabilitiesOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 404);
    }

    // -- decide_replace_group_capabilities ------------------------------

    #[test]
    fn a_sysadmin_replaces_the_full_capability_set() {
        let c = conn();
        let gid = seed_group(&c, "engineers");
        let outcome = decide_replace_group_capabilities(
            &c,
            &gid,
            true,
            "admin",
            None,
            &serde_json::json!({"capabilities": ["system.config.write"]}),
        )
        .unwrap();
        let ReplaceGroupCapabilitiesOutcome::Replaced(caps) = outcome else {
            panic!("expected Replaced, got {outcome:?}");
        };
        assert_eq!(caps, vec!["system.config.write"]);
    }

    #[test]
    fn rejects_replacing_on_an_unknown_group() {
        let c = conn();
        let outcome = decide_replace_group_capabilities(
            &c,
            "nope",
            true,
            "admin",
            None,
            &serde_json::json!({"capabilities": []}),
        )
        .unwrap();
        let ReplaceGroupCapabilitiesOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 404);
    }

    #[test]
    fn rejects_a_non_array_body() {
        let c = conn();
        let gid = seed_group(&c, "engineers");
        let outcome = decide_replace_group_capabilities(
            &c,
            &gid,
            true,
            "admin",
            None,
            &serde_json::json!({"capabilities": "not-a-list"}),
        )
        .unwrap();
        let ReplaceGroupCapabilitiesOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 400);
    }

    #[test]
    fn rejects_a_non_string_entry() {
        let c = conn();
        let gid = seed_group(&c, "engineers");
        let outcome = decide_replace_group_capabilities(
            &c,
            &gid,
            true,
            "admin",
            None,
            &serde_json::json!({"capabilities": [42]}),
        )
        .unwrap();
        let ReplaceGroupCapabilitiesOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 400);
    }

    #[test]
    fn rejects_an_unknown_capability_string() {
        let c = conn();
        let gid = seed_group(&c, "engineers");
        let outcome = decide_replace_group_capabilities(
            &c,
            &gid,
            true,
            "admin",
            None,
            &serde_json::json!({"capabilities": ["system.not.a.real.cap"]}),
        )
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

    #[test]
    fn rejects_a_resource_tier_capability() {
        let c = conn();
        let gid = seed_group(&c, "engineers");
        let outcome = decide_replace_group_capabilities(
            &c,
            &gid,
            true,
            "admin",
            None,
            &serde_json::json!({"capabilities": ["tasks.create"]}),
        )
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

    #[test]
    fn a_non_sysadmin_cannot_grant_a_capability_they_lack() {
        let c = conn();
        let gid = seed_group(&c, "engineers");
        let outcome = decide_replace_group_capabilities(
            &c,
            &gid,
            false,
            "bob",
            None, // no principal at all -- fails closed
            &serde_json::json!({"capabilities": ["system.config.write"]}),
        )
        .unwrap();
        let ReplaceGroupCapabilitiesOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected, got {outcome:?}");
        };
        assert_eq!(resp.status, 403);
    }

    #[test]
    fn a_non_sysadmin_cannot_revoke_a_capability_they_lack_either() {
        // AZ-R12-1: a shrinking PUT (here, an empty list) removes the
        // existing cap -- that's a REVOKE, and the caller must hold
        // the cap being revoked just like a grant.
        let c = conn();
        let gid = seed_group(&c, "engineers");
        group_capability_repository::replace(&c, &gid, ["system.config.write"]).unwrap();
        let outcome = decide_replace_group_capabilities(
            &c,
            &gid,
            false,
            "bob",
            None,
            &serde_json::json!({"capabilities": []}),
        )
        .unwrap();
        let ReplaceGroupCapabilitiesOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected, got {outcome:?}");
        };
        assert_eq!(resp.status, 403);
        // Confirm the reject actually left the group's caps untouched.
        assert_eq!(
            group_capability_repository::fetch(&c, &gid).unwrap().len(),
            1
        );
    }

    #[test]
    fn duplicate_entries_are_deduped() {
        let c = conn();
        let gid = seed_group(&c, "engineers");
        let outcome = decide_replace_group_capabilities(
            &c,
            &gid,
            true,
            "admin",
            None,
            &serde_json::json!({"capabilities": ["system.config.write", "system.config.write"]}),
        )
        .unwrap();
        let ReplaceGroupCapabilitiesOutcome::Replaced(caps) = outcome else {
            panic!("expected Replaced, got {outcome:?}");
        };
        assert_eq!(caps, vec!["system.config.write"]);
    }
}
