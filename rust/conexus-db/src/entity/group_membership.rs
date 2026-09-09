//! sea-orm `Entity` for `group_membership` — lives on the ROUTER db
//! (`schema::init_router_schema`), same distinction `group_capability`
//! already establishes for the same DB file. No `Entity` for this
//! table existed anywhere in `conexus-db` before this PR (checked
//! directly against `group_membership_repository.rs` and this
//! `entity/` directory) — this is a genuinely new definition, not a
//! duplicate of existing work.
//!
//! # Primary key: synthetic `rowid`, same reasoning as `project_membership`
//!
//! Like `project_membership`, `group_membership`'s own `CREATE TABLE`
//! in `schema.rs` has NO `PRIMARY KEY` clause at all — its two FK
//! columns (`member_user_id`/`member_group_id`) are mutually exclusive
//! per a `CHECK` constraint, and its only uniqueness comes from two
//! *partial* unique indexes (`uq_group_membership_user`/
//! `uq_group_membership_group`, each `WHERE ... IS NOT NULL`) sea-orm's
//! primary-key derive has no syntax to express. See
//! `project_membership.rs`'s own module doc for the full reasoning
//! (verified against `sea-orm-macros` 2.0.2 source that a primary key
//! field is mandatory to compile at all, and why a literal composite
//! key over nullable columns would be non-unique in Rust terms even
//! though SQL's partial indexes make it work at the SQL level) — not
//! repeated here to keep one canonical explanation.
//!
//! `member_group_id` gives this table a SECOND, independent FK to
//! `groups` alongside `group_id`; see `groups.rs`'s own module doc for
//! why that means `groups::Entity` has no `has_many` edge back to this
//! table and this `Entity` has no `impl Related<groups::Entity>` — the
//! ambiguity is real (two candidate join columns), so both `belongs_to`
//! variants stay directly usable on this `Relation` enum instead of
//! being collapsed into a `Related` impl that would silently pick one.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "group_membership")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = true)]
    pub rowid: i64,
    pub group_id: String,
    pub member_user_id: Option<String>,
    pub member_group_id: Option<String>,
    pub added_at: String,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {
    #[sea_orm(
        belongs_to = "super::groups::Entity",
        from = "Column::GroupId",
        to = "super::groups::Column::GroupId",
        on_delete = "Cascade"
    )]
    Group,
    #[sea_orm(
        belongs_to = "super::users::Entity",
        from = "Column::MemberUserId",
        to = "super::users::Column::UserId",
        on_delete = "Cascade"
    )]
    MemberUser,
    #[sea_orm(
        belongs_to = "super::groups::Entity",
        from = "Column::MemberGroupId",
        to = "super::groups::Column::GroupId",
        on_delete = "Cascade"
    )]
    MemberGroup,
}

impl Related<super::users::Entity> for Entity {
    fn to() -> RelationDef {
        Relation::MemberUser.def()
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
        let path = dir.path().join("group_membership_entity_test.db");

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
                 VALUES ('g1', 'parent', 0, '2026-01-01T00:00:00Z')",
                [],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO groups (group_id, name, is_sysadmin, created_at) \
                 VALUES ('g2', 'child', 0, '2026-01-01T00:00:00Z')",
                [],
            )
            .unwrap();
            // Use the real repository function (not a hand-rolled
            // INSERT) for the user-membership edge, per this PR's own
            // "write via the existing repository" option.
            crate::group_membership_repository::add_group_member(
                &conn,
                "g1",
                Some("u1"),
                None,
                "2026-01-01T00:00:00Z",
            )
            .unwrap();
            // And the group-inside-group edge, exercising the OTHER FK
            // to `groups` (`member_group_id`).
            crate::group_membership_repository::add_group_member(
                &conn,
                "g1",
                None,
                Some("g2"),
                "2026-01-01T00:00:01Z",
            )
            .unwrap();
        }

        let url = format!("sqlite://{}", path.display());
        let db = Database::connect(&url).await.unwrap();

        let mut rows = Entity::find().all(&db).await.unwrap();
        rows.sort_by_key(|r| r.rowid);
        assert_eq!(rows.len(), 2);
        assert_ne!(rows[0].rowid, rows[1].rowid);

        let user_edge = rows.iter().find(|r| r.member_user_id.is_some()).unwrap();
        assert_eq!(user_edge.group_id, "g1");
        assert_eq!(user_edge.member_user_id.as_deref(), Some("u1"));
        assert!(user_edge.member_group_id.is_none());

        let group_edge = rows.iter().find(|r| r.member_group_id.is_some()).unwrap();
        assert_eq!(group_edge.group_id, "g1");
        assert!(group_edge.member_user_id.is_none());
        assert_eq!(group_edge.member_group_id.as_deref(), Some("g2"));

        // ActiveModel insert path, leaving `rowid` NotSet.
        let am = ActiveModel {
            rowid: sea_orm::ActiveValue::NotSet,
            group_id: Set("g1".to_string()),
            member_user_id: Set(None),
            member_group_id: Set(None),
            added_at: Set("2026-01-01T00:00:02Z".to_string()),
        };
        // This particular row violates the real CHECK constraint
        // (neither member column set) -- proves the Entity doesn't
        // silently bypass real SQL constraints just because sea-orm
        // is the one issuing the INSERT.
        let err = am.insert(&db).await.unwrap_err();
        assert!(format!("{err}").to_lowercase().contains("check"));
    }
}
