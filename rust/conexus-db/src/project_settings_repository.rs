//! Port of `agent_mcp/repositories/project_settings_repository.py`.
//!
//! Byte-for-byte identical SQL/behavior shape to
//! [`crate::project_context_repository`] — same 5 CRUD functions, same
//! BL-R22-1 partial-update rule, same "dumb CRUD" seam — the ONLY
//! difference is the table name. That's deliberate on both the
//! Python and Rust sides, per ADR-0016: `project_context` is agent-
//! authored, RAG-indexed *memory*; `project_settings` is operator-only
//! *config* (feature flags, secrets, tiered-visibility knobs via
//! `core/settings_schema.py`) that must never be RAG-indexed and has
//! tighter access control. Migration `0016` was a hard cutover moving
//! `config_*` keys out of `project_context` specifically because a
//! shared table let a blanket "any `config_*` key is secret"
//! redaction rule sweep up legitimate settings sitting in memory
//! (bug F009). The table SEPARATION is the actual safety boundary —
//! so this is kept as its own module against its own table rather
//! than genericized over a table-name parameter, even though the code
//! is a near-duplicate.
//!
//! This repository itself has zero settings-schema awareness (no
//! type/tier/secrecy validation, no ADR-0018 involvement) — it is
//! dumb CRUD; that validation lives entirely at the tool layer
//! (`project_settings_tools.py` in Python, `conexus-tools::
//! project_settings_tools` in Rust) before it ever calls in here.
//!
//! Phase G (sea-orm migration): the seventh repository converted.
//! [`upsert`] keeps the get-then-branch shape (not sea-orm's
//! `.on_conflict()` builder) for the identical reason `project_context_
//! repository::upsert`'s own doc gives: `on_conflict`'s
//! `update_columns` list is fixed at call-construction time, so it
//! can't express "conditionally include `description` in the `UPDATE`
//! based on a runtime bool" the way BL-R22-1 requires.
//!
//! [`get_bool`]/[`get_bool_override`]/[`get_int`] are this
//! repository's own extra surface beyond the `project_context_
//! repository` template — every real caller reads through one of
//! these three, never `get` directly, so they're converted alongside
//! the CRUD half rather than deferred.

use sea_orm::{
    ActiveValue::Set, ColumnTrait, DatabaseConnection, DbErr, EntityTrait, QueryFilter, QueryOrder,
};

pub use crate::entity::project_settings::Model as ProjectSettingRow;
use crate::entity::project_settings::{ActiveModel, Column, Entity};

/// The two columns [`delete_many`] actually needs to report back —
/// deliberately not the full [`ProjectSettingRow`], matching the
/// narrower `SELECT` Python's version runs before deleting.
#[derive(Debug, Clone, PartialEq)]
pub struct DeletedSettingEntry {
    pub context_key: String,
    pub description: Option<String>,
}

/// Read a boolean toggle. Port of `agent_mcp/tools/access.py::
/// _get_config_bool`, minus its ADR-0018 "resolve the default from the
/// settings-schema registry when omitted" branch — that registry
/// (`core/settings_schema.py`) is not ported to Rust yet, so `default`
/// is a required explicit parameter here (matching the module's OWN
/// pre-ADR-0018 shape: every real call site already knows its default,
/// it's the registry indirection that's out of scope for now, not the
/// value itself).
///
/// A missing row, an unreadable value, or any DB error all fall back to
/// `default` — same "unreachable settings store during early bootstrap
/// degrades to the default" contract as Python.
pub async fn get_bool(db: &DatabaseConnection, context_key: &str, default: bool) -> bool {
    get_bool_override(db, context_key).await.unwrap_or(default)
}

/// [`get_bool`] without a baked-in default -- `None` for "no row, an
/// unreadable value, or a DB error" rather than silently resolving to
/// a caller-supplied fallback. The seam `conexus_auth::PolicySource`
/// impls (e.g. the backend's per-request settings snapshot) need: a
/// `Requirement::Policy`'s own `default` field is the ONE place that
/// fallback belongs, so a `PolicySource` reading this store must be
/// able to say "no override" distinctly from "override is off".
pub async fn get_bool_override(db: &DatabaseConnection, context_key: &str) -> Option<bool> {
    let row = get(db, context_key).await.ok()??;
    match row.value.trim().trim_matches('"').to_lowercase().as_str() {
        "true" | "1" | "yes" | "on" => Some(true),
        "false" | "0" | "no" | "off" => Some(false),
        _ => None,
    }
}

/// Read an integer knob. Port of `agent_mcp/tools/access.py::
/// _get_config_int` (same scope note as [`get_bool`] re: the
/// settings-schema default registry).
///
/// `value` is JSON-encoded on write, but parse liberally — JSON first,
/// then a bare integer string — since tests / external tools may push
/// a raw (non-JSON) value.
pub async fn get_int(db: &DatabaseConnection, context_key: &str, default: i64) -> i64 {
    let Ok(Some(row)) = get(db, context_key).await else {
        return default;
    };
    if let Ok(v) = serde_json::from_str::<i64>(&row.value) {
        return v;
    }
    row.value.trim().parse().unwrap_or(default)
}

/// Single-key lookup. Reads through the caller's own connection pool,
/// so an uncommitted write earlier in the same logical operation is
/// visible here — load-bearing for [`upsert`]'s existence check.
pub async fn get(
    db: &DatabaseConnection,
    context_key: &str,
) -> Result<Option<ProjectSettingRow>, DbErr> {
    Entity::find_by_id(context_key.to_string()).one(db).await
}

/// Full snapshot, ordered by key — backs `view_project_settings`/
/// `GET /api/settings-data`.
pub async fn list_all(db: &DatabaseConnection) -> Result<Vec<ProjectSettingRow>, DbErr> {
    Entity::find()
        .order_by_asc(Column::ContextKey)
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
/// when `description_provided` is true — BL-R22-1's partial-update-
/// parity fix, identical to `project_context_repository::upsert`.
/// `created_at`/`created_by` are never touched on UPDATE.
pub async fn upsert(
    db: &DatabaseConnection,
    context_key: &str,
    value: &str,
    description: Option<&str>,
    description_provided: bool,
    actor: &str,
    now: &str,
) -> Result<(ProjectSettingRow, bool), DbErr> {
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
/// so the caller can map that to a `Conflict`.
pub async fn create_new(
    db: &DatabaseConnection,
    context_key: &str,
    value: &str,
    description: Option<&str>,
    actor: &str,
    now: &str,
) -> Result<Option<ProjectSettingRow>, DbErr> {
    if get(db, context_key).await?.is_some() {
        return Ok(None);
    }
    insert_new(db, context_key, value, description, actor, now).await?;
    get(db, context_key).await
}

/// Deletes rows for the given keys, returning only the entries that
/// actually existed. A no-op for an empty slice.
pub async fn delete_many(
    db: &DatabaseConnection,
    context_keys: &[&str],
) -> Result<Vec<DeletedSettingEntry>, DbErr> {
    if context_keys.is_empty() {
        return Ok(Vec::new());
    }

    let existing: Vec<DeletedSettingEntry> = Entity::find()
        .filter(Column::ContextKey.is_in(context_keys.iter().copied()))
        .all(db)
        .await?
        .into_iter()
        .map(|row| DeletedSettingEntry {
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
            "config_max_agents",
            "10",
            Some("agent cap"),
            true,
            "alice",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();
        assert!(created);
        assert_eq!(row.value, "10");
        assert_eq!(row.description.as_deref(), Some("agent cap"));
        assert_eq!(row.created_at.as_deref(), Some("2026-01-01T00:00:00Z"));
        assert_eq!(row.created_by.as_deref(), Some("alice"));
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

    #[tokio::test]
    async fn project_settings_and_project_context_are_independent_tables() {
        // The whole point of this repository's existence (ADR-0016):
        // a key in one table must not collide with, shadow, or be
        // visible through the other. Both repositories are sea-orm-
        // backed now (Phase G) -- two separate connections against the
        // SAME underlying file, so both observe the same tables
        // (`:memory:` connections never would, since each is its own
        // isolated database).
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.db");
        {
            let c = rusqlite::Connection::open(&path).unwrap();
            init_schema(&c).unwrap();
        }
        let db = sea_orm::Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();

        upsert(
            &db,
            "shared_key",
            "settings-value",
            None,
            true,
            "alice",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();
        crate::project_context_repository::upsert(
            &db,
            "shared_key",
            "context-value",
            None,
            true,
            "alice",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();

        assert_eq!(
            get(&db, "shared_key").await.unwrap().unwrap().value,
            "settings-value"
        );
        assert_eq!(
            crate::project_context_repository::get(&db, "shared_key")
                .await
                .unwrap()
                .unwrap()
                .value,
            "context-value"
        );
    }

    // -- get_bool ----------------------------------------------------------

    #[tokio::test]
    async fn get_bool_missing_key_returns_the_default() {
        let (_dir, db) = test_conn().await;
        assert!(get_bool(&db, "config_nope", true).await);
        assert!(!get_bool(&db, "config_nope", false).await);
    }

    #[tokio::test]
    async fn get_bool_parses_common_truthy_and_falsy_strings() {
        let (_dir, db) = test_conn().await;
        for (raw, expected) in [
            ("true", true),
            ("\"true\"", true),
            ("1", true),
            ("yes", true),
            ("on", true),
            ("false", false),
            ("0", false),
            ("no", false),
            ("off", false),
            ("TRUE", true), // case-insensitive
        ] {
            upsert(&db, "k", raw, None, false, "tester", "2026-01-01T00:00:00Z")
                .await
                .unwrap();
            assert_eq!(get_bool(&db, "k", !expected).await, expected, "raw={raw:?}");
        }
    }

    #[tokio::test]
    async fn get_bool_unparseable_value_falls_back_to_default() {
        let (_dir, db) = test_conn().await;
        upsert(
            &db,
            "k",
            "not-a-bool",
            None,
            false,
            "tester",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();
        assert!(get_bool(&db, "k", true).await);
        assert!(!get_bool(&db, "k", false).await);
    }

    #[tokio::test]
    async fn get_bool_override_is_none_for_a_missing_key() {
        // Unlike `get_bool`, no default to fall back to -- `None` is
        // the distinct "no override on record" signal a `PolicySource`
        // needs (see this function's own doc).
        let (_dir, db) = test_conn().await;
        assert_eq!(get_bool_override(&db, "config_nope").await, None);
    }

    #[tokio::test]
    async fn get_bool_override_returns_the_parsed_value_when_a_row_exists() {
        let (_dir, db) = test_conn().await;
        upsert(
            &db,
            "k",
            "false",
            None,
            false,
            "tester",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();
        assert_eq!(get_bool_override(&db, "k").await, Some(false));
    }

    // -- get_int -------------------------------------------------------------

    #[tokio::test]
    async fn get_int_missing_key_returns_the_default() {
        let (_dir, db) = test_conn().await;
        assert_eq!(get_int(&db, "config_nope", 604800).await, 604800);
    }

    #[tokio::test]
    async fn get_int_parses_a_json_encoded_integer() {
        let (_dir, db) = test_conn().await;
        upsert(
            &db,
            "k",
            "3600",
            None,
            false,
            "tester",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();
        assert_eq!(get_int(&db, "k", 0).await, 3600);
    }

    #[tokio::test]
    async fn get_int_parses_a_bare_non_json_integer_string() {
        let (_dir, db) = test_conn().await;
        // Not JSON (no surrounding quotes on what would be a string, and
        // this raw value itself isn't valid JSON) -- falls back to a
        // plain parse, matching Python's liberal coercion.
        upsert(
            &db,
            "k",
            "  42  ",
            None,
            false,
            "tester",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();
        assert_eq!(get_int(&db, "k", 0).await, 42);
    }

    #[tokio::test]
    async fn get_int_unparseable_value_falls_back_to_default() {
        let (_dir, db) = test_conn().await;
        upsert(
            &db,
            "k",
            "not-an-int",
            None,
            false,
            "tester",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();
        assert_eq!(get_int(&db, "k", 604800).await, 604800);
    }
}
