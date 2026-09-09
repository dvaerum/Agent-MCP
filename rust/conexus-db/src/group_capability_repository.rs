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
//! **Phase G (sea-orm migration, router step 4 PR F)**: [`fetch`]/
//! [`replace`] are rewritten onto `sea_orm::DatabaseConnection` against
//! `conexus_db::entity::group_capability`. [`fetch`] keeps a
//! deliberately-still-sync [`fetch_sync`] rusqlite twin: it's the ONE
//! function this module exports that `conexus_auth::capabilities::
//! resolve_capabilities` calls, and `resolve_capabilities` is itself
//! called synchronously from `session_gate.rs::evaluate_session_gate`/
//! `project_gate.rs`'s revalidation functions -- this migration's own
//! declared highest-risk hot path, never forced async as a side effect
//! of a repository-layer conversion. [`replace`] has no such caller
//! (its only real production caller, `admin_group_capabilities.rs::
//! decide_replace_group_capabilities`, opens no transaction of its own
//! and is itself converted to async in the same PR) so it converts
//! wholesale, with no sync twin.

use rusqlite::{Connection, Result};
use sea_orm::{
    ActiveModelTrait, ActiveValue::Set, ColumnTrait, DatabaseConnection, DbErr, EntityTrait,
    QueryFilter, TransactionTrait,
};
use std::collections::HashSet;

use crate::entity::group_capability::{ActiveModel, Column, Entity};

/// Every capability string granted to `group_id`. Empty is
/// indistinguishable from "no such group" — existence-checking the
/// group itself is the caller's job, matching Python (neither this
/// function nor [`replace`] validates the group exists).
pub async fn fetch(
    db: &DatabaseConnection,
    group_id: &str,
) -> std::result::Result<HashSet<String>, DbErr> {
    let rows = Entity::find()
        .filter(Column::GroupId.eq(group_id))
        .all(db)
        .await?;
    Ok(rows.into_iter().map(|r| r.capability).collect())
}

/// Sync rusqlite twin of [`fetch`] -- see this module's own doc for
/// why this one function keeps a twin while [`replace`] doesn't.
pub fn fetch_sync(conn: &Connection, group_id: &str) -> Result<HashSet<String>> {
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
/// DELETE and every INSERT run in ONE sea-orm transaction
/// (`DatabaseConnection::begin`): without that, a mid-sequence failure
/// (e.g. an FK violation from a nonexistent `group_id`) would leave
/// the DELETE committed and the group's capability set silently
/// cleared instead of the whole call failing atomically — same
/// all-or-nothing guarantee the prior rusqlite
/// `unchecked_transaction`-based implementation gave, now via sea-orm;
/// see the
/// `replace_on_a_nonexistent_group_fails_on_the_fk_constraint_and_touches_nothing`
/// test for the failure this specifically guards against.
pub async fn replace<'a, I: IntoIterator<Item = &'a str>>(
    db: &DatabaseConnection,
    group_id: &str,
    capabilities: I,
) -> std::result::Result<(), DbErr> {
    // De-dup, preserving nothing about order (this is a set-store) —
    // matches Python's `dict.fromkeys` dedup before the executemany.
    let mut seen = HashSet::new();
    let deduped: Vec<&str> = capabilities
        .into_iter()
        .filter(|c| seen.insert(*c))
        .collect();

    let tx = db.begin().await?;
    Entity::delete_many()
        .filter(Column::GroupId.eq(group_id))
        .exec(&tx)
        .await?;
    for cap in &deduped {
        ActiveModel {
            group_id: Set(group_id.to_string()),
            capability: Set((*cap).to_string()),
        }
        .insert(&tx)
        .await?;
    }
    tx.commit().await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::init_router_schema;
    use sea_orm::ConnectionTrait;

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
    fn fetch_sync_unknown_group_returns_empty_set() {
        let conn = test_conn();
        assert_eq!(fetch_sync(&conn, "nope").unwrap(), HashSet::new());
    }

    /// A file-backed router DB opened as both a `rusqlite::Connection`
    /// (schema init + FK pragma) and a sea-orm `DatabaseConnection` on
    /// the SAME file -- an in-memory `:memory:` DB can't be shared
    /// across two separate connection handles the way a real file can.
    /// Same recipe as `identity.rs`'s/`group_membership_repository.rs`'s
    /// own `conn_with_sea_orm()`.
    async fn conn_with_sea_orm() -> (tempfile::TempDir, Connection, DatabaseConnection) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("group_capability_test.db");
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch("PRAGMA foreign_keys = ON;").unwrap();
        init_router_schema(&conn).unwrap();
        let db = sea_orm::Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        // sqlx's sqlite driver's own FK-pragma default isn't something
        // to rely on sight-unseen for a test that specifically proves
        // FK enforcement -- set it explicitly on this handle too,
        // matching the `conn.execute_batch("PRAGMA foreign_keys = ON;")`
        // rusqlite side does above.
        db.execute_unprepared("PRAGMA foreign_keys = ON;")
            .await
            .unwrap();
        (dir, conn, db)
    }

    #[tokio::test]
    async fn fetch_unknown_group_returns_empty_set() {
        let (_dir, _conn, db) = conn_with_sea_orm().await;
        assert_eq!(fetch(&db, "nope").await.unwrap(), HashSet::new());
    }

    #[tokio::test]
    async fn replace_then_fetch_round_trips() {
        let (_dir, conn, db) = conn_with_sea_orm().await;
        seed_group(&conn, "g1");
        replace(&db, "g1", ["system.view", "system.usersManage"])
            .await
            .unwrap();

        let caps = fetch(&db, "g1").await.unwrap();
        assert_eq!(
            caps,
            HashSet::from(["system.view".to_string(), "system.usersManage".to_string()])
        );
        // Both connections see the same committed state.
        assert_eq!(caps, fetch_sync(&conn, "g1").unwrap());
    }

    #[tokio::test]
    async fn replace_is_a_full_replace_not_a_merge() {
        let (_dir, conn, db) = conn_with_sea_orm().await;
        seed_group(&conn, "g1");
        replace(&db, "g1", ["system.view"]).await.unwrap();
        replace(&db, "g1", ["system.usersManage"]).await.unwrap();

        // "system.view" from the first call must be GONE, not merged.
        let caps = fetch(&db, "g1").await.unwrap();
        assert_eq!(caps, HashSet::from(["system.usersManage".to_string()]));
    }

    #[tokio::test]
    async fn replace_with_empty_set_clears_capabilities() {
        let (_dir, conn, db) = conn_with_sea_orm().await;
        seed_group(&conn, "g1");
        replace(&db, "g1", ["system.view"]).await.unwrap();
        replace(&db, "g1", []).await.unwrap();

        assert_eq!(fetch(&db, "g1").await.unwrap(), HashSet::new());
    }

    #[tokio::test]
    async fn replace_deduplicates_caller_supplied_duplicates() {
        let (_dir, conn, db) = conn_with_sea_orm().await;
        seed_group(&conn, "g1");
        replace(&db, "g1", ["system.view", "system.view", "system.view"])
            .await
            .unwrap();

        assert_eq!(
            fetch(&db, "g1").await.unwrap(),
            HashSet::from(["system.view".to_string()])
        );
    }

    #[tokio::test]
    async fn replace_does_not_affect_other_groups() {
        let (_dir, conn, db) = conn_with_sea_orm().await;
        seed_group(&conn, "g1");
        seed_group(&conn, "g2");
        replace(&db, "g1", ["system.view"]).await.unwrap();
        replace(&db, "g2", ["system.usersManage"]).await.unwrap();

        assert_eq!(
            fetch(&db, "g1").await.unwrap(),
            HashSet::from(["system.view".to_string()])
        );
        assert_eq!(
            fetch(&db, "g2").await.unwrap(),
            HashSet::from(["system.usersManage".to_string()])
        );
    }

    #[tokio::test]
    async fn deleting_a_group_cascades_to_its_capabilities() {
        let (_dir, conn, db) = conn_with_sea_orm().await;
        seed_group(&conn, "g1");
        replace(&db, "g1", ["system.view"]).await.unwrap();

        conn.execute("DELETE FROM groups WHERE group_id = 'g1'", [])
            .unwrap();

        assert_eq!(
            fetch(&db, "g1").await.unwrap(),
            HashSet::new(),
            "ON DELETE CASCADE must have removed the rows"
        );
    }

    #[tokio::test]
    async fn replace_on_a_nonexistent_group_fails_on_the_fk_constraint_and_touches_nothing() {
        let (_dir, conn, db) = conn_with_sea_orm().await;
        seed_group(&conn, "g1");
        replace(&db, "g1", ["system.view"]).await.unwrap();

        // "nonexistent-group" was never inserted into `groups`, so the
        // INSERT half of replace() must fail on the FK constraint
        // (the DELETE half is a no-op either way, since no rows exist
        // for that group_id yet). sqlx's sqlite driver enables
        // `PRAGMA foreign_keys` by default (unlike rusqlite, which
        // needs it set explicitly per-connection), so the constraint
        // is live on `db` with no extra pragma call.
        let err = replace(&db, "nonexistent-group", ["system.view"]).await;
        assert!(err.is_err());
        assert_eq!(
            fetch(&db, "nonexistent-group").await.unwrap(),
            HashSet::new()
        );

        // An unrelated group's state is untouched by a failing call
        // for a different group_id -- the transaction wrapper matters
        // for exactly this reason once a real caller runs replace()
        // for many groups in sequence.
        assert_eq!(
            fetch(&db, "g1").await.unwrap(),
            HashSet::from(["system.view".to_string()])
        );
    }
}
