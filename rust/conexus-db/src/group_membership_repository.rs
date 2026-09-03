//! Port of `resolve_user_groups` from `agent_mcp/router/group_resolver.py`.
//!
//! That Python module owns a much larger surface (cycle detection on
//! insert, `add_group_member`, sysadmin-inheritance checks,
//! `group_resolved_project_roles`, ...) — this crate only ports the one
//! read path `conexus-auth`'s `resolve_capabilities` needs. The rest is
//! deferred to whichever phase eventually ports the router's group-
//! management REST surface (`admin_users_api.py`), since nothing else
//! in the currently-ported crates calls it.
//!
//! Lives on the ROUTER DB (`router.db`), same as `group_capability_repository`.

use rusqlite::{Connection, Result};
use std::collections::HashSet;

/// The transitive set of `group_id`s `user_id` belongs to: every group
/// it's a direct member of, plus every ancestor of those groups (a
/// group nested inside another group makes the parent's membership
/// transitive). Empty when the user has no memberships.
///
/// Ported from `group_resolver._resolve_user_groups_on` +
/// `_ancestors_on`: seed with direct `member_user_id` edges, then walk
/// upward via `member_group_id` level-by-level, batching each level in
/// one `IN (...)` query (O(depth) round-trips, not one query per row).
pub fn resolve_user_groups(conn: &Connection, user_id: &str) -> Result<HashSet<String>> {
    let mut stmt =
        conn.prepare("SELECT group_id FROM group_membership WHERE member_user_id = ?1")?;
    let rows = stmt.query_map([user_id], |row| row.get::<_, String>(0))?;
    let direct: HashSet<String> = rows.collect::<Result<_>>()?;
    drop(stmt);

    if direct.is_empty() {
        return Ok(HashSet::new());
    }
    ancestors(conn, direct)
}

/// Upward closure over `group_membership.member_group_id`: `seed` plus
/// every group reachable by walking upward from it. Shared kernel
/// between `resolve_user_groups` and (a future) `resolve_group_ancestors`
/// if that's ever needed here.
fn ancestors(conn: &Connection, seed: HashSet<String>) -> Result<HashSet<String>> {
    let mut result = seed.clone();
    let mut frontier: Vec<String> = seed.into_iter().collect();

    while !frontier.is_empty() {
        let placeholders = crate::sql_util::in_placeholders(frontier.len());
        let sql = format!(
            "SELECT DISTINCT group_id FROM group_membership WHERE member_group_id IN ({placeholders})"
        );
        let mut stmt = conn.prepare(&sql)?;
        let params = crate::sql_util::to_sql_refs(&frontier);
        let rows = stmt.query_map(params.as_slice(), |row| row.get::<_, String>(0))?;
        let level: Vec<String> = rows.collect::<Result<_>>()?;
        drop(stmt);

        let mut next_frontier = Vec::new();
        for gid in level {
            if result.insert(gid.clone()) {
                next_frontier.push(gid);
            }
        }
        frontier = next_frontier;
    }

    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::init_router_schema;

    fn test_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch("PRAGMA foreign_keys = ON;").unwrap();
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
        conn.execute(
            "INSERT INTO group_membership (group_id, member_user_id, added_at) VALUES (?1, ?2, '2026-01-01T00:00:00Z')",
            [group_id, user_id],
        )
        .unwrap();
    }

    fn add_group_member(conn: &Connection, parent_group_id: &str, child_group_id: &str) {
        conn.execute(
            "INSERT INTO group_membership (group_id, member_group_id, added_at) VALUES (?1, ?2, '2026-01-01T00:00:00Z')",
            [parent_group_id, child_group_id],
        )
        .unwrap();
    }

    #[test]
    fn user_with_no_memberships_resolves_to_empty_set() {
        let conn = test_conn();
        assert_eq!(
            resolve_user_groups(&conn, "nobody").unwrap(),
            HashSet::new()
        );
    }

    #[test]
    fn direct_membership_resolves_to_that_one_group() {
        let conn = test_conn();
        seed_group(&conn, "engineers");
        add_user_member(&conn, "engineers", "alice");

        assert_eq!(
            resolve_user_groups(&conn, "alice").unwrap(),
            HashSet::from(["engineers".to_string()])
        );
    }

    #[test]
    fn nested_group_membership_resolves_transitively() {
        // alice is directly in "backend", which is nested inside
        // "engineers", which is nested inside "all-staff" -- resolving
        // alice's groups must walk the whole chain upward.
        let conn = test_conn();
        seed_group(&conn, "backend");
        seed_group(&conn, "engineers");
        seed_group(&conn, "all-staff");
        add_user_member(&conn, "backend", "alice");
        add_group_member(&conn, "engineers", "backend");
        add_group_member(&conn, "all-staff", "engineers");

        assert_eq!(
            resolve_user_groups(&conn, "alice").unwrap(),
            HashSet::from([
                "backend".to_string(),
                "engineers".to_string(),
                "all-staff".to_string(),
            ])
        );
    }

    #[test]
    fn sibling_groups_dont_leak_into_each_others_resolution() {
        let conn = test_conn();
        seed_group(&conn, "engineers");
        seed_group(&conn, "sales");
        add_user_member(&conn, "engineers", "alice");
        add_user_member(&conn, "sales", "bob");

        assert_eq!(
            resolve_user_groups(&conn, "alice").unwrap(),
            HashSet::from(["engineers".to_string()])
        );
    }

    #[test]
    fn diamond_shaped_membership_graph_resolves_without_duplication_or_hang() {
        // alice is in both "backend" and "frontend", which both nest
        // inside "engineers" -- a naive walk that doesn't dedup the
        // frontier could loop or double-count.
        let conn = test_conn();
        seed_group(&conn, "backend");
        seed_group(&conn, "frontend");
        seed_group(&conn, "engineers");
        add_user_member(&conn, "backend", "alice");
        add_user_member(&conn, "frontend", "alice");
        add_group_member(&conn, "engineers", "backend");
        add_group_member(&conn, "engineers", "frontend");

        assert_eq!(
            resolve_user_groups(&conn, "alice").unwrap(),
            HashSet::from([
                "backend".to_string(),
                "frontend".to_string(),
                "engineers".to_string(),
            ])
        );
    }

    #[test]
    fn membership_in_an_unrelated_group_does_not_expand_to_the_whole_graph() {
        let conn = test_conn();
        seed_group(&conn, "engineers");
        seed_group(&conn, "island");
        add_user_member(&conn, "island", "alice");

        assert_eq!(
            resolve_user_groups(&conn, "alice").unwrap(),
            HashSet::from(["island".to_string()])
        );
    }
}
