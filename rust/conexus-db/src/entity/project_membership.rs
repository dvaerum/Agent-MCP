//! sea-orm `Entity` for `project_membership` — lives on the ROUTER db
//! (`schema::init_router_schema`), same distinction `group_capability`
//! already establishes for the same DB file.
//!
//! # Primary key: a synthetic `rowid`, not a mechanical 1:1 mapping
//!
//! `project_membership` (see `schema.rs`'s own `init_router_schema`
//! doc comment on this table) has NO `PRIMARY KEY` clause at all — its
//! only uniqueness comes from two *partial* unique indexes,
//! `uq_project_membership_user (project_name, user_id) WHERE user_id
//! IS NOT NULL` and `uq_project_membership_group (project_name,
//! group_id) WHERE group_id IS NOT NULL`, enforced by a `CHECK
//! ((user_id IS NOT NULL) <> (group_id IS NOT NULL))` that makes every
//! row exactly a user-row or a group-row, never both.
//!
//! sea-orm's `DeriveEntityModel` genuinely requires at least one
//! `#[sea_orm(primary_key)]` field to compile at all — verified
//! directly against `sea-orm-macros` 2.0.2's own source
//! (`derives/primary_key.rs::impl_primary_key_to_column`), which emits
//! `compile_error!("Entity must have a primary key column. See
//! <https://github.com/SeaQL/sea-orm/issues/485> for details.")` when
//! the `PrimaryKey` enum has zero variants. So "no primary key at all"
//! is not an option; some field has to carry the attribute.
//!
//! A literal composite `(project_name, user_id, group_id)` key was
//! rejected: `user_id`/`group_id` are each `NULL` on roughly half of
//! all rows (the CHECK constraint guarantees it), and every group-row
//! for a given project would then present as sea-orm's SAME logical
//! key `(project_name, NULL, NULL)` in Rust terms — `PrimaryKeyTrait`
//! has no NULL-aware uniqueness the way SQL's partial unique indexes
//! do, so that key would be silently non-unique for exactly the rows
//! it's supposed to distinguish. The two REAL uniqueness constraints
//! are also each conditional on a `WHERE ... IS NOT NULL` clause sea-
//! orm's primary-key derive has no syntax to express at all.
//!
//! Instead this `Model` declares SQLite's own implicit `rowid` pseudo-
//! column (present on every ordinary rowid table — this one is not
//! `WITHOUT ROWID`, verified directly against the `CREATE TABLE`
//! in `schema.rs`) as an `i64 primary_key, auto_increment = true`
//! field named literally `rowid`, matching sea-orm's default column-
//! name-from-field-name convention with no `column_name` override
//! needed. It requires no schema migration (it costs nothing —
//! `rowid` already exists on the table today) and gives every row a
//! genuine, always-unique identity, which is exactly what
//! `PrimaryKeyTrait` needs; it does NOT replace or weaken the real
//! partial-unique-index constraints, which remain enforced by SQLite
//! exactly as before and are unrelated to this `rowid` choice. This
//! module's own round-trip test proves `rowid` is actually queryable
//! and insertable through the real schema, not just that the type
//! compiles.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "project_membership")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = true)]
    pub rowid: i64,
    pub project_name: String,
    pub user_id: Option<String>,
    pub group_id: Option<String>,
    pub role: String,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {
    #[sea_orm(
        belongs_to = "super::users::Entity",
        from = "Column::UserId",
        to = "super::users::Column::UserId",
        on_delete = "Cascade"
    )]
    User,
    #[sea_orm(
        belongs_to = "super::groups::Entity",
        from = "Column::GroupId",
        to = "super::groups::Column::GroupId",
        on_delete = "Cascade"
    )]
    Group,
}

impl Related<super::users::Entity> for Entity {
    fn to() -> RelationDef {
        Relation::User.def()
    }
}

impl Related<super::groups::Entity> for Entity {
    fn to() -> RelationDef {
        Relation::Group.def()
    }
}

impl ActiveModelBehavior for ActiveModel {}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::init_router_schema;
    use sea_orm::{ActiveValue::Set, Database, EntityTrait};

    #[tokio::test]
    async fn entity_round_trips_against_the_real_router_schema() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("project_membership_entity_test.db");

        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            init_router_schema(&conn).unwrap();
            conn.execute(
                "INSERT INTO users (user_id, username, created_at, is_sysadmin) \
                 VALUES ('u1', 'alice', '2026-01-01T00:00:00Z', 0)",
                [],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO groups (group_id, name, is_sysadmin, created_at) \
                 VALUES ('g1', 'admins', 0, '2026-01-01T00:00:00Z')",
                [],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO project_membership (project_name, user_id, group_id, role) \
                 VALUES ('proj-a', 'u1', NULL, 'operator')",
                [],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO project_membership (project_name, user_id, group_id, role) \
                 VALUES ('proj-a', NULL, 'g1', 'viewer')",
                [],
            )
            .unwrap();
        }

        let url = format!("sqlite://{}", path.display());
        let db = Database::connect(&url).await.unwrap();

        let mut rows = Entity::find().all(&db).await.unwrap();
        rows.sort_by_key(|r| r.rowid);
        assert_eq!(rows.len(), 2);

        // The synthetic `rowid` primary key genuinely distinguishes
        // these two rows even though `(project_name, user_id, group_id)`
        // would not (both would collapse to the same NULL-bearing key
        // in Rust terms, per this module's own doc comment above).
        assert_ne!(rows[0].rowid, rows[1].rowid);

        let user_row = rows.iter().find(|r| r.user_id.is_some()).unwrap();
        assert_eq!(user_row.project_name, "proj-a");
        assert_eq!(user_row.user_id.as_deref(), Some("u1"));
        assert!(user_row.group_id.is_none());
        assert_eq!(user_row.role, "operator");

        let group_row = rows.iter().find(|r| r.group_id.is_some()).unwrap();
        assert_eq!(group_row.project_name, "proj-a");
        assert!(group_row.user_id.is_none());
        assert_eq!(group_row.group_id.as_deref(), Some("g1"));
        assert_eq!(group_row.role, "viewer");

        // Round-trip an insert through the ActiveModel too, leaving
        // `rowid` unset so SQLite assigns it (auto_increment = true) --
        // proves the pseudo-column is genuinely writable, not just
        // readable.
        let am = ActiveModel {
            rowid: sea_orm::ActiveValue::NotSet,
            project_name: Set("proj-b".to_string()),
            user_id: Set(Some("u1".to_string())),
            group_id: Set(None),
            role: Set("viewer".to_string()),
        };
        let inserted = am.insert(&db).await.unwrap();
        assert!(inserted.rowid > 0);
        drop(db);

        let conn = rusqlite::Connection::open(&path).unwrap();
        let (project_name, role): (String, String) = conn
            .query_row(
                "SELECT project_name, role FROM project_membership WHERE rowid = ?1",
                [inserted.rowid],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(project_name, "proj-b");
        assert_eq!(role, "viewer");
    }
}
