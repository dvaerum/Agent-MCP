//! `resolve_capabilities` — the DB-backed half `conexus_core::capability`'s
//! own module doc explicitly deferred to this crate.
//!
//! Faithful port of `agent_mcp/core/capabilities.py::resolve_capabilities`,
//! composing three already-ported pieces: the pure bundle functions
//! (`agent_role_bundle`/`project_role_bundle`, `conexus-core`), the
//! group-capability overlay
//! (`conexus_db::group_capability_repository::fetch`), and transitive
//! group resolution (`conexus_db::group_membership_repository::
//! resolve_user_groups`).

use conexus_core::capability::{
    agent_role_bundle, project_role_bundle, AgentRole, Capabilities, Capability, ProjectRole,
};
use conexus_core::principal::PrincipalKind;
use conexus_db::{group_capability_repository, group_membership_repository};
use rusqlite::{Connection, Result};
use std::collections::HashSet;
use std::str::FromStr;

/// Everything `resolve_capabilities` needs about the calling identity.
/// Mirrors the keyword arguments of the Python function of the same
/// name.
pub struct ResolveCapabilitiesInput<'a> {
    pub sysadmin: bool,
    pub kind: PrincipalKind,
    /// The agent-bearer's role. Only consulted when `kind ==
    /// AgentBearer`.
    pub agent_role: Option<AgentRole>,
    /// The operator's user id. Only consulted for the two operator
    /// paths (`OperatorSession`/`ForwardingHeader`); group
    /// memberships never apply to agent bearers.
    pub user_id: Option<&'a str>,
    /// The operator's role inside the target project.
    pub project_role: Option<ProjectRole>,
    /// Pre-resolved transitive group set, when the caller already
    /// walked the membership graph once this request (Python's
    /// `groups` param — arch-deepening R4 #3, so a single request
    /// pays for exactly one walk instead of a second). `None`
    /// self-resolves via `router_conn`.
    pub groups: Option<&'a HashSet<String>>,
}

/// Compute the capability set for a Principal.
///
/// `router_conn`: `Some` when a router-DB connection is available
/// (the two operator-facing auth seams); `None` when it isn't (the
/// per-project agent-side backend has no router.db handle, and
/// neither do most tests). This is the explicit Rust equivalent of
/// Python's `except Exception: pass` around the group lookup: rather
/// than attempting the query and swallowing whatever failure "not
/// initialized" produces, the caller states up front that there's no
/// connection to try, and the group-overlay step is skipped entirely.
/// A `Some(conn)` that fails a query (e.g. a genuinely corrupt or
/// mid-migration router.db) is treated as a real error and
/// propagated, matching this workspace's usual DB-error-propagation
/// convention — "not available" and "available but broken" are
/// different situations, and Python's blanket `except Exception` did
/// not distinguish them.
pub fn resolve_capabilities(
    router_conn: Option<&Connection>,
    input: ResolveCapabilitiesInput<'_>,
) -> Result<Capabilities> {
    if input.sysadmin {
        return Ok(Capabilities::Sysadmin);
    }

    if input.kind == PrincipalKind::AgentBearer {
        let caps = match input.agent_role {
            Some(role) => agent_role_bundle(role),
            None => HashSet::new(),
        };
        return Ok(Capabilities::Set(caps));
    }

    // OperatorSession / ForwardingHeader.
    let mut caps: HashSet<Capability> = HashSet::new();

    if let (Some(user_id), Some(conn)) = (input.user_id, router_conn) {
        let group_ids: HashSet<String> = match input.groups {
            Some(pre_resolved) => pre_resolved.clone(),
            None => group_membership_repository::resolve_user_groups(conn, user_id)?,
        };
        for gid in &group_ids {
            let granted = group_capability_repository::fetch(conn, gid)?;
            // SEC R2-F3 (see conexus_db::group_capability_repository's
            // module doc): `group_capability` has no `project_name`
            // column, so a resource-tier grant would be global across
            // every project the caller can reach — only `system.*`
            // caps (deployment-wide, no project dimension) are safe
            // to admit from a group row. `Capability::from_str`
            // failing closed on an unknown/typo'd string is this
            // crate's equivalent of Python's `& KNOWN_CAPABILITIES`
            // intersection (which also excludes the sysadmin wildcard
            // `"*"`, since that string parses to nothing here either).
            caps.extend(
                granted
                    .iter()
                    .filter_map(|s| Capability::from_str(s).ok())
                    .filter(Capability::is_system_tier),
            );
        }
    }

    if let Some(role) = input.project_role {
        caps.extend(project_role_bundle(role));
    }

    Ok(Capabilities::Set(caps))
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_db::schema::init_router_schema;

    fn router_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        init_router_schema(&conn).unwrap();
        conn
    }

    fn seed_group(conn: &Connection, group_id: &str) {
        conn.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) VALUES (?1, ?1, 0, '2026-01-01T00:00:00Z')",
            [group_id],
        )
        .unwrap();
    }

    fn add_user_member(conn: &Connection, group_id: &str, user_id: &str) {
        // `member_user_id` carries a real FK to `users(user_id)` since
        // Phase E2 PR 3 backfilled it -- seed a placeholder row first
        // (`OR IGNORE` since a test can call this more than once for
        // the same user_id).
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username, created_at) VALUES (?1, ?1, '2026-01-01T00:00:00Z')",
            [user_id],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO group_membership (group_id, member_user_id, added_at) VALUES (?1, ?2, '2026-01-01T00:00:00Z')",
            [group_id, user_id],
        )
        .unwrap();
    }

    fn base_input() -> ResolveCapabilitiesInput<'static> {
        ResolveCapabilitiesInput {
            sysadmin: false,
            kind: PrincipalKind::OperatorSession,
            agent_role: None,
            user_id: None,
            project_role: None,
            groups: None,
        }
    }

    #[test]
    fn sysadmin_short_circuits_before_any_other_field_is_consulted() {
        let mut input = base_input();
        input.sysadmin = true;
        input.kind = PrincipalKind::AgentBearer; // would otherwise return empty
        input.agent_role = None;

        // No router_conn at all -- if the sysadmin branch didn't
        // short-circuit unconditionally, this would panic/degrade
        // instead of returning the wildcard.
        assert_eq!(
            resolve_capabilities(None, input).unwrap(),
            Capabilities::Sysadmin
        );
    }

    #[test]
    fn agent_bearer_returns_its_role_bundle_verbatim() {
        let mut input = base_input();
        input.kind = PrincipalKind::AgentBearer;
        input.agent_role = Some(AgentRole::Manager);

        let result = resolve_capabilities(None, input).unwrap();
        assert_eq!(
            result,
            Capabilities::Set(agent_role_bundle(AgentRole::Manager))
        );
    }

    #[test]
    fn agent_bearer_with_no_role_returns_empty_set() {
        let mut input = base_input();
        input.kind = PrincipalKind::AgentBearer;
        input.agent_role = None;

        assert_eq!(
            resolve_capabilities(None, input).unwrap(),
            Capabilities::Set(HashSet::new())
        );
    }

    #[test]
    fn agent_bearer_ignores_groups_and_project_role_entirely() {
        // Groups never apply to agent bearers, even if a caller
        // mistakenly supplies them -- per-project tokens, not
        // operator identities.
        let mut input = base_input();
        input.kind = PrincipalKind::AgentBearer;
        input.agent_role = Some(AgentRole::Worker);
        input.project_role = Some(ProjectRole::Operator);
        let groups = HashSet::from(["some-group".to_string()]);
        input.groups = Some(&groups);

        assert_eq!(
            resolve_capabilities(None, input).unwrap(),
            Capabilities::Set(agent_role_bundle(AgentRole::Worker))
        );
    }

    #[test]
    fn operator_with_no_router_conn_falls_back_to_bundle_only() {
        let mut input = base_input();
        input.user_id = Some("alice");
        input.project_role = Some(ProjectRole::Viewer);

        assert_eq!(
            resolve_capabilities(None, input).unwrap(),
            Capabilities::Set(project_role_bundle(ProjectRole::Viewer))
        );
    }

    #[test]
    fn operator_with_no_project_role_and_no_groups_returns_empty_set() {
        let conn = router_conn();
        let mut input = base_input();
        input.user_id = Some("alice");

        assert_eq!(
            resolve_capabilities(Some(&conn), input).unwrap(),
            Capabilities::Set(HashSet::new())
        );
    }

    #[test]
    fn system_tier_group_capability_is_admitted() {
        let conn = router_conn();
        seed_group(&conn, "admins");
        add_user_member(&conn, "admins", "alice");
        group_capability_repository::replace(&conn, "admins", ["system.view"]).unwrap();

        let mut input = base_input();
        input.user_id = Some("alice");

        let result = resolve_capabilities(Some(&conn), input).unwrap();
        assert_eq!(
            result,
            Capabilities::Set(HashSet::from([Capability::SystemView]))
        );
    }

    #[test]
    fn resource_tier_group_capability_is_dropped_sec_r2_f3() {
        // The load-bearing regression test for SEC R2-F3: a
        // resource-tier cap (no project dimension on
        // `group_capability`) must NEVER be admitted from a group
        // row, even though `group_capability_repository::replace`
        // itself doesn't validate the vocabulary at the write side.
        let conn = router_conn();
        seed_group(&conn, "admins");
        add_user_member(&conn, "admins", "alice");
        group_capability_repository::replace(&conn, "admins", ["system.view", "memories.create"])
            .unwrap();

        let mut input = base_input();
        input.user_id = Some("alice");

        let result = resolve_capabilities(Some(&conn), input).unwrap();
        assert_eq!(
            result,
            Capabilities::Set(HashSet::from([Capability::SystemView]))
        );
    }

    #[test]
    fn unknown_capability_string_in_a_group_row_is_dropped() {
        // Defense-in-depth against a migration/repair-script/typo'd
        // row -- `Capability::from_str` failing closed is this
        // crate's equivalent of Python's `& KNOWN_CAPABILITIES`
        // intersection (which also excludes the sysadmin wildcard
        // `"*"` from ever being sourced from a group row).
        let conn = router_conn();
        seed_group(&conn, "admins");
        add_user_member(&conn, "admins", "alice");
        group_capability_repository::replace(&conn, "admins", ["system.view", "*", "bogus"])
            .unwrap();

        let mut input = base_input();
        input.user_id = Some("alice");

        let result = resolve_capabilities(Some(&conn), input).unwrap();
        assert_eq!(
            result,
            Capabilities::Set(HashSet::from([Capability::SystemView]))
        );
    }

    #[test]
    fn transitive_group_membership_contributes_capabilities() {
        let conn = router_conn();
        seed_group(&conn, "backend");
        seed_group(&conn, "engineers");
        add_user_member(&conn, "backend", "alice");
        conn.execute(
            "INSERT INTO group_membership (group_id, member_group_id, added_at) VALUES ('engineers', 'backend', '2026-01-01T00:00:00Z')",
            [],
        )
        .unwrap();
        group_capability_repository::replace(&conn, "engineers", ["system.config.write"]).unwrap();

        let mut input = base_input();
        input.user_id = Some("alice");

        let result = resolve_capabilities(Some(&conn), input).unwrap();
        assert_eq!(
            result,
            Capabilities::Set(HashSet::from([Capability::SystemConfigWrite]))
        );
    }

    #[test]
    fn pre_resolved_groups_skip_the_db_walk_and_are_used_verbatim() {
        let conn = router_conn();
        // Deliberately do NOT create a group_membership edge for
        // alice -- if `groups` weren't honoured, the DB walk would
        // find nothing and the group capability below would be
        // missed.
        seed_group(&conn, "admins");
        group_capability_repository::replace(&conn, "admins", ["system.view"]).unwrap();

        let mut input = base_input();
        input.user_id = Some("alice");
        let groups = HashSet::from(["admins".to_string()]);
        input.groups = Some(&groups);

        let result = resolve_capabilities(Some(&conn), input).unwrap();
        assert_eq!(
            result,
            Capabilities::Set(HashSet::from([Capability::SystemView]))
        );
    }

    #[test]
    fn project_role_bundle_and_group_overlay_are_additive() {
        let conn = router_conn();
        seed_group(&conn, "admins");
        add_user_member(&conn, "admins", "alice");
        group_capability_repository::replace(&conn, "admins", ["system.sso.configure"]).unwrap();

        let mut input = base_input();
        input.user_id = Some("alice");
        input.project_role = Some(ProjectRole::Viewer);

        let result = resolve_capabilities(Some(&conn), input).unwrap();
        let mut expected = project_role_bundle(ProjectRole::Viewer);
        expected.insert(Capability::SystemSsoConfigure);
        assert_eq!(result, Capabilities::Set(expected));
    }
}
