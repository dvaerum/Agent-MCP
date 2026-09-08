//! Port of `agent_mcp/repositories/project_context_repository.py`.
//!
//! A module of plain functions, not a struct/class with methods —
//! matching the Python source's own deliberate design: unlike
//! `AgentRepository`, there is no in-memory cache here, so there is
//! no per-repository state to hold and thus no reason for a wrapper
//! type. Every function takes the `&DatabaseConnection` it should run
//! against — this crate has no separate "opens its own connection"
//! path, matching every other repository here.
//!
//! Phase G (sea-orm migration): the fifth repository converted.
//! [`upsert`] keeps the rusqlite version's two-branch shape (`get`
//! first to determine created-vs-updated, then a different write per
//! branch) rather than reaching for sea-orm's `.on_conflict()` builder
//! — `on_conflict`'s `update_columns` list is fixed at call-construction
//! time, so it can't express "conditionally include `description` in
//! the `UPDATE` based on a runtime bool" the way BL-R22-1 requires;
//! the get-then-branch shape sidesteps that entirely.

use sea_orm::{
    ActiveValue::Set, ColumnTrait, DatabaseConnection, DbErr, EntityTrait, QueryFilter, QueryOrder,
    QuerySelect,
};

pub use crate::entity::project_context::Model as ProjectContextRow;
use crate::entity::project_context::{ActiveModel, Column, Entity};

/// The two columns [`delete_many`] actually needs to report back —
/// deliberately not the full [`ProjectContextRow`], matching the
/// narrower `SELECT` the rusqlite version ran before deleting.
#[derive(Debug, Clone, PartialEq)]
pub struct DeletedContextEntry {
    pub context_key: String,
    pub description: Option<String>,
}

/// Single-key lookup. Reads through the caller's own connection pool,
/// so an uncommitted write earlier in the same logical operation is
/// visible here when the caller runs inside a transaction — load-
/// bearing for [`upsert`]'s existence check and any caller-side
/// authorization gate that needs to see its own prior writes.
pub async fn get(
    db: &DatabaseConnection,
    context_key: &str,
) -> Result<Option<ProjectContextRow>, DbErr> {
    Entity::find_by_id(context_key.to_string()).one(db).await
}

/// Full snapshot, ordered by key — for backup/consistency-validation
/// call sites.
pub async fn list_all(db: &DatabaseConnection) -> Result<Vec<ProjectContextRow>, DbErr> {
    Entity::find()
        .order_by_asc(Column::ContextKey)
        .all(db)
        .await
}

/// The newest `limit` rows by `updated_at`, for a bounded dashboard
/// read (`GET /api/context-data`, `/api/all-data`'s context section).
/// A SQL-level `ORDER BY ... LIMIT`, not `list_all` truncated
/// afterward -- pentest R2-F2's whole point is that a project with
/// thousands of context rows must never materialise the full table on
/// a dashboard poll just to keep the newest `limit`.
pub async fn list_recent(
    db: &DatabaseConnection,
    limit: i64,
) -> Result<Vec<ProjectContextRow>, DbErr> {
    Entity::find()
        .order_by_desc(Column::UpdatedAt)
        .limit(limit as u64)
        .all(db)
        .await
}

async fn insert_new(
    db: &DatabaseConnection,
    context_key: &str,
    value: &str,
    description: Option<&str>,
    actor: &str,
    now: &str,
) -> Result<(), DbErr> {
    let am = ActiveModel {
        context_key: Set(context_key.to_string()),
        value: Set(value.to_string()),
        description: Set(description.map(str::to_string)),
        created_at: Set(Some(now.to_string())),
        created_by: Set(Some(actor.to_string())),
        updated_at: Set(now.to_string()),
        updated_by: Set(actor.to_string()),
    };
    Entity::insert(am).exec(db).await?;
    Ok(())
}

/// INSERT-or-UPDATE. On UPDATE, `description` is only overwritten
/// when `description_provided` is true — a value-only update (the
/// caller didn't ask to change the description) must NOT NULL it out
/// (this is BL-R22-1's partial-update-parity fix: inferring "clear
/// the description" from `description: None` was the actual bug,
/// which is exactly why this is a separate bool rather than
/// `Option<Option<&str>>`-style inference). `created_at`/`created_by`
/// are never touched on UPDATE — they're set once, on the row's
/// actual creation. Returns the refreshed row plus whether this call
/// created it (`true`) or updated an existing one (`false`).
pub async fn upsert(
    db: &DatabaseConnection,
    context_key: &str,
    value: &str,
    description: Option<&str>,
    description_provided: bool,
    actor: &str,
    now: &str,
) -> Result<(ProjectContextRow, bool), DbErr> {
    let created = get(db, context_key).await?.is_none();

    if created {
        insert_new(db, context_key, value, description, actor, now).await?;
    } else if description_provided {
        Entity::update_many()
            .col_expr(Column::Value, sea_orm::sea_query::Expr::value(value))
            .col_expr(Column::UpdatedAt, sea_orm::sea_query::Expr::value(now))
            .col_expr(Column::UpdatedBy, sea_orm::sea_query::Expr::value(actor))
            .col_expr(
                Column::Description,
                sea_orm::sea_query::Expr::value(description),
            )
            .filter(Column::ContextKey.eq(context_key))
            .exec(db)
            .await?;
    } else {
        Entity::update_many()
            .col_expr(Column::Value, sea_orm::sea_query::Expr::value(value))
            .col_expr(Column::UpdatedAt, sea_orm::sea_query::Expr::value(now))
            .col_expr(Column::UpdatedBy, sea_orm::sea_query::Expr::value(actor))
            .filter(Column::ContextKey.eq(context_key))
            .exec(db)
            .await?;
    }

    let row = get(db, context_key)
        .await?
        .expect("row was just written under this same connection");
    Ok((row, created))
}

/// INSERT-only — `None` (no write) if `context_key` already exists,
/// so the caller can map that to a `Conflict` without this function
/// needing to know about `ToolResult`.
pub async fn create_new(
    db: &DatabaseConnection,
    context_key: &str,
    value: &str,
    description: Option<&str>,
    actor: &str,
    now: &str,
) -> Result<Option<ProjectContextRow>, DbErr> {
    if get(db, context_key).await?.is_some() {
        return Ok(None);
    }
    insert_new(db, context_key, value, description, actor, now).await?;
    get(db, context_key).await
}

/// Deletes rows for the given keys, returning only the entries that
/// actually existed (missing keys are silently omitted, not errors).
/// A no-op for an empty slice — no query is run at all.
pub async fn delete_many(
    db: &DatabaseConnection,
    context_keys: &[&str],
) -> Result<Vec<DeletedContextEntry>, DbErr> {
    if context_keys.is_empty() {
        return Ok(Vec::new());
    }

    let existing: Vec<DeletedContextEntry> = Entity::find()
        .filter(Column::ContextKey.is_in(context_keys.iter().copied()))
        .all(db)
        .await?
        .into_iter()
        .map(|row| DeletedContextEntry {
            context_key: row.context_key,
            description: row.description,
        })
        .collect();

    if !existing.is_empty() {
        Entity::delete_many()
            .filter(Column::ContextKey.is_in(context_keys.iter().copied()))
            .exec(db)
            .await?;
    }

    Ok(existing)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::init_schema;
    use sea_orm::Database;

    async fn test_conn() -> (tempfile::TempDir, DatabaseConnection) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.db");
        {
            let c = rusqlite::Connection::open(&path).unwrap();
            init_schema(&c).unwrap();
        }
        let db = Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        (dir, db)
    }

    #[tokio::test]
    async fn get_returns_none_for_unknown_key() {
        let (_dir, db) = test_conn().await;
        assert_eq!(get(&db, "nope").await.unwrap(), None);
    }

    #[tokio::test]
    async fn upsert_creates_a_new_row_and_reports_created_true() {
        let (_dir, db) = test_conn().await;
        let (row, created) = upsert(
            &db,
            "greeting",
            "hello",
            Some("a friendly greeting"),
            true,
            "alice",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();
        assert!(created);
        assert_eq!(row.value, "hello");
        assert_eq!(row.description.as_deref(), Some("a friendly greeting"));
        assert_eq!(row.created_at.as_deref(), Some("2026-01-01T00:00:00Z"));
        assert_eq!(row.created_by.as_deref(), Some("alice"));
        assert_eq!(row.updated_at, "2026-01-01T00:00:00Z");
        assert_eq!(row.updated_by, "alice");
    }

    #[tokio::test]
    async fn upsert_on_existing_key_updates_and_reports_created_false() {
        let (_dir, db) = test_conn().await;
        upsert(
            &db,
            "k",
            "v1",
            Some("d1"),
            true,
            "alice",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();

        let (row, created) = upsert(
            &db,
            "k",
            "v2",
            Some("d2"),
            true,
            "bob",
            "2026-01-02T00:00:00Z",
        )
        .await
        .unwrap();
        assert!(!created);
        assert_eq!(row.value, "v2");
        assert_eq!(row.description.as_deref(), Some("d2"));
        // created_at/created_by must NEVER change on UPDATE.
        assert_eq!(row.created_at.as_deref(), Some("2026-01-01T00:00:00Z"));
        assert_eq!(row.created_by.as_deref(), Some("alice"));
        assert_eq!(row.updated_by, "bob");
    }

    #[tokio::test]
    async fn upsert_value_only_update_preserves_existing_description() {
        let (_dir, db) = test_conn().await;
        upsert(
            &db,
            "k",
            "v1",
            Some("original description"),
            true,
            "alice",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();

        // BL-R22-1: description_provided=false must NOT null out the
        // existing description, even though `description` here is
        // None -- that's the whole point of the separate bool flag.
        let (row, _) = upsert(&db, "k", "v2", None, false, "alice", "2026-01-02T00:00:00Z")
            .await
            .unwrap();
        assert_eq!(row.value, "v2");
        assert_eq!(row.description.as_deref(), Some("original description"));
    }

    #[tokio::test]
    async fn upsert_can_explicitly_clear_description_when_provided() {
        let (_dir, db) = test_conn().await;
        upsert(
            &db,
            "k",
            "v1",
            Some("will be cleared"),
            true,
            "alice",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();

        let (row, _) = upsert(&db, "k", "v2", None, true, "alice", "2026-01-02T00:00:00Z")
            .await
            .unwrap();
        assert_eq!(row.description, None);
    }

    #[tokio::test]
    async fn create_new_succeeds_for_a_fresh_key() {
        let (_dir, db) = test_conn().await;
        let row = create_new(&db, "k", "v1", Some("d1"), "alice", "2026-01-01T00:00:00Z")
            .await
            .unwrap()
            .unwrap();
        assert_eq!(row.value, "v1");
    }

    #[tokio::test]
    async fn create_new_returns_none_on_conflict_and_does_not_touch_the_existing_row() {
        let (_dir, db) = test_conn().await;
        create_new(&db, "k", "v1", Some("d1"), "alice", "2026-01-01T00:00:00Z")
            .await
            .unwrap();

        let result = create_new(&db, "k", "v2", Some("d2"), "bob", "2026-01-02T00:00:00Z")
            .await
            .unwrap();
        assert_eq!(result, None);

        let row = get(&db, "k").await.unwrap().unwrap();
        assert_eq!(
            row.value, "v1",
            "the conflicting create_new must not have mutated the existing row"
        );
    }

    #[tokio::test]
    async fn list_all_returns_rows_ordered_by_context_key() {
        let (_dir, db) = test_conn().await;
        upsert(
            &db,
            "zeta",
            "v",
            None,
            true,
            "alice",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();
        upsert(
            &db,
            "alpha",
            "v",
            None,
            true,
            "alice",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();
        upsert(&db, "mu", "v", None, true, "alice", "2026-01-01T00:00:00Z")
            .await
            .unwrap();

        let rows = list_all(&db).await.unwrap();
        let keys: Vec<&str> = rows.iter().map(|r| r.context_key.as_str()).collect();
        assert_eq!(keys, vec!["alpha", "mu", "zeta"]);
    }

    #[tokio::test]
    async fn list_recent_orders_newest_updated_first_and_respects_the_limit() {
        let (_dir, db) = test_conn().await;
        upsert(
            &db,
            "oldest",
            "v",
            None,
            true,
            "alice",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();
        upsert(
            &db,
            "middle",
            "v",
            None,
            true,
            "alice",
            "2026-01-02T00:00:00Z",
        )
        .await
        .unwrap();
        upsert(
            &db,
            "newest",
            "v",
            None,
            true,
            "alice",
            "2026-01-03T00:00:00Z",
        )
        .await
        .unwrap();

        let all = list_recent(&db, 10).await.unwrap();
        let keys: Vec<&str> = all.iter().map(|r| r.context_key.as_str()).collect();
        assert_eq!(keys, vec!["newest", "middle", "oldest"]);

        let capped = list_recent(&db, 2).await.unwrap();
        let capped_keys: Vec<&str> = capped.iter().map(|r| r.context_key.as_str()).collect();
        assert_eq!(capped_keys, vec!["newest", "middle"]);
    }

    #[tokio::test]
    async fn delete_many_empty_slice_is_a_noop() {
        let (_dir, db) = test_conn().await;
        assert_eq!(delete_many(&db, &[]).await.unwrap(), Vec::new());
    }

    #[tokio::test]
    async fn delete_many_silently_omits_missing_keys_and_removes_the_rest() {
        let (_dir, db) = test_conn().await;
        upsert(
            &db,
            "a",
            "v",
            Some("desc-a"),
            true,
            "alice",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();
        upsert(&db, "b", "v", None, true, "alice", "2026-01-01T00:00:00Z")
            .await
            .unwrap();

        let deleted = delete_many(&db, &["a", "b", "does-not-exist"])
            .await
            .unwrap();
        let mut keys: Vec<&str> = deleted.iter().map(|e| e.context_key.as_str()).collect();
        keys.sort();
        assert_eq!(keys, vec!["a", "b"]);
        assert_eq!(
            deleted
                .iter()
                .find(|e| e.context_key == "a")
                .unwrap()
                .description
                .as_deref(),
            Some("desc-a")
        );

        assert_eq!(get(&db, "a").await.unwrap(), None);
        assert_eq!(get(&db, "b").await.unwrap(), None);
    }
}
