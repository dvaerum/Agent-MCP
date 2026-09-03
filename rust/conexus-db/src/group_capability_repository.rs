//! Port of `agent_mcp/repositories/group_capability_repository.py`.
//!
//! The DB seam for a sysadmin-configurable group -> capability grant
//! table: an operator's resolved group memberships can additively
//! widen their effective capability set beyond their `project_role`
//! bundle, for `system.*` capabilities only (resource-tier caps from
//! a group are rejected at the write side — see SEC R2-F3 in the
//! Python source's `core/capabilities.py` — because this table has no
//! `project_name` column, so a resource-tier grant here would be a
//! cross-project privilege escalation). That filtering logic composes
//! ON TOP of these two functions; it isn't part of this module, same
//! as Python's split between this repository and
//! `core.capabilities.resolve_capabilities`.
//!
//! A module of plain functions, matching Python's own design — this
//! lives against the router DB, which has no per-project-DB-style
//! unit-of-work, and (like `project_context_repository`) has no cache
//! to justify a wrapper type.
//!
//! The DB-backed group-capability overlay itself (composing this with
//! `group_membership_repository::resolve_user_groups` and
//! `conexus_core::capability`'s bundle functions into a full
//! `resolve_capabilities()`) lives in `conexus-auth`
//! (`capabilities::resolve_capabilities`) — this crate only ports the
//! two raw DB operations.

use rusqlite::{Connection, Result};
use std::collections::HashSet;

/// Every capability string granted to `group_id`. Empty is
/// indistinguishable from "no such group" — existence-checking the
/// group itself is the caller's job, matching Python (neither `fetch`
/// nor `replace` validates the group exists).
pub fn fetch(conn: &Connection, group_id: &str) -> Result<HashSet<String>> {
    let mut stmt = conn.prepare("SELECT capability FROM group_capability WHERE group_id = ?1")?;
    let rows = stmt.query_map([group_id], |row| row.get::<_, String>(0))?;
    rows.collect()
}

/// Atomically REPLACE the complete capability set for `group_id` —
/// "set" semantics, not additive grant/revoke. Always physically
/// deletes+reinserts (idempotent in effect, not in I/O), matching
/// Python. Deliberately does NOT validate `capabilities` against a
/// known vocabulary — per the Python source, that validation lives at
/// the API/dashboard seam, not here, so the dashboard can pre-flight
/// with a friendlier error than a bare constraint violation.
///
/// DELETE and every INSERT run in one transaction: without that, a
/// mid-sequence failure (e.g. an FK violation from a nonexistent
/// `group_id`) would leave the DELETE committed and the group's
/// capability set silently cleared instead of the whole call failing
/// atomically — see the
/// `replace_on_a_nonexistent_group_fails_on_the_fk_constraint_and_touches_nothing`
/// test for the failure this specifically guards against.
pub fn replace<'a, I: IntoIterator<Item = &'a str>>(
    conn: &Connection,
    group_id: &str,
    capabilities: I,
) -> Result<()> {
    // De-dup, preserving nothing about order (this is a set-store) —
    // matches Python's `dict.fromkeys` dedup before the executemany.
    let mut seen = HashSet::new();
    let deduped: Vec<&str> = capabilities
        .into_iter()
        .filter(|c| seen.insert(*c))
        .collect();

    // SAFETY/correctness note: `unchecked_transaction` is safe here
    // because this function never nests inside another transaction on
    // the same connection — it owns the whole DELETE+INSERT sequence
    // start to finish. rusqlite's checked `Connection::transaction()`
    // would require `&mut Connection`, which every other function in
    // this crate deliberately avoids taking (repositories share a
    // `&Connection`, not an exclusive borrow).
    let tx = conn.unchecked_transaction()?;
    tx.execute(
        "DELETE FROM group_capability WHERE group_id = ?1",
        [group_id],
    )?;
    for cap in &deduped {
        tx.execute(
            "INSERT INTO group_capability (group_id, capability) VALUES (?1, ?2)",
            (group_id, *cap),
        )?;
    }
    tx.commit()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::init_router_schema;

    fn test_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        // FK enforcement is off by default per-connection in SQLite —
        // needed for the CASCADE/violation tests below to mean anything.
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

    #[test]
    fn fetch_unknown_group_returns_empty_set() {
        let conn = test_conn();
        assert_eq!(fetch(&conn, "nope").unwrap(), HashSet::new());
    }

    #[test]
    fn replace_then_fetch_round_trips() {
        let conn = test_conn();
        seed_group(&conn, "g1");
        replace(&conn, "g1", ["system.view", "system.usersManage"]).unwrap();

        let caps = fetch(&conn, "g1").unwrap();
        assert_eq!(
            caps,
            HashSet::from(["system.view".to_string(), "system.usersManage".to_string()])
        );
    }

    #[test]
    fn replace_is_a_full_replace_not_a_merge() {
        let conn = test_conn();
        seed_group(&conn, "g1");
        replace(&conn, "g1", ["system.view"]).unwrap();
        replace(&conn, "g1", ["system.usersManage"]).unwrap();

        // "system.view" from the first call must be GONE, not merged.
        let caps = fetch(&conn, "g1").unwrap();
        assert_eq!(caps, HashSet::from(["system.usersManage".to_string()]));
    }

    #[test]
    fn replace_with_empty_set_clears_capabilities() {
        let conn = test_conn();
        seed_group(&conn, "g1");
        replace(&conn, "g1", ["system.view"]).unwrap();
        replace(&conn, "g1", []).unwrap();

        assert_eq!(fetch(&conn, "g1").unwrap(), HashSet::new());
    }

    #[test]
    fn replace_deduplicates_caller_supplied_duplicates() {
        let conn = test_conn();
        seed_group(&conn, "g1");
        replace(&conn, "g1", ["system.view", "system.view", "system.view"]).unwrap();

        assert_eq!(
            fetch(&conn, "g1").unwrap(),
            HashSet::from(["system.view".to_string()])
        );
    }

    #[test]
    fn replace_does_not_affect_other_groups() {
        let conn = test_conn();
        seed_group(&conn, "g1");
        seed_group(&conn, "g2");
        replace(&conn, "g1", ["system.view"]).unwrap();
        replace(&conn, "g2", ["system.usersManage"]).unwrap();

        assert_eq!(
            fetch(&conn, "g1").unwrap(),
            HashSet::from(["system.view".to_string()])
        );
        assert_eq!(
            fetch(&conn, "g2").unwrap(),
            HashSet::from(["system.usersManage".to_string()])
        );
    }

    #[test]
    fn deleting_a_group_cascades_to_its_capabilities() {
        let conn = test_conn();
        seed_group(&conn, "g1");
        replace(&conn, "g1", ["system.view"]).unwrap();

        conn.execute("DELETE FROM groups WHERE group_id = 'g1'", [])
            .unwrap();

        assert_eq!(
            fetch(&conn, "g1").unwrap(),
            HashSet::new(),
            "ON DELETE CASCADE must have removed the rows"
        );
    }

    #[test]
    fn replace_on_a_nonexistent_group_fails_on_the_fk_constraint_and_touches_nothing() {
        let conn = test_conn();
        seed_group(&conn, "g1");
        replace(&conn, "g1", ["system.view"]).unwrap();

        // "nonexistent-group" was never inserted into `groups`, so the
        // INSERT half of replace() must fail on the FK constraint
        // (the DELETE half is a no-op either way, since no rows exist
        // for that group_id yet).
        let err = replace(&conn, "nonexistent-group", ["system.view"]);
        assert!(err.is_err());
        assert_eq!(fetch(&conn, "nonexistent-group").unwrap(), HashSet::new());

        // An unrelated group's state is untouched by a failing call
        // for a different group_id -- the transaction wrapper matters
        // for exactly this reason once a real caller runs replace()
        // for many groups in sequence: one bad group_id must not be
        // able to leave a partial DELETE-without-matching-INSERT state
        // behind for itself, which this schema can't directly
        // reproduce for an EXISTING group (there's no capability-side
        // constraint that can fail after a valid group's own DELETE
        // succeeds) -- so this test pins the observable half: replace()
        // is all-or-nothing per call.
        assert_eq!(
            fetch(&conn, "g1").unwrap(),
            HashSet::from(["system.view".to_string()])
        );
    }
}
