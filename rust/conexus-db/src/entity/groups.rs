//! sea-orm `Entity` for `groups` — lives on the ROUTER db
//! (`schema::init_router_schema`), same distinction `group_capability`
//! already establishes for the same DB file.
//!
//! Module named `groups` (plural), matching the real table name;
//! `group_capability.rs`'s own retrofit below shows why this Entity
//! didn't exist until now — `group_capability`/`group_membership`/
//! `sessions`/`project_membership` all predate it and had nothing to
//! relate to.
//!
//! `Relation` deliberately has only TWO `has_many` edges
//! (`group_capability`, `project_membership`), not three. The obvious
//! third edge — `group_membership` rows where THIS group is the
//! containing group (`group_membership.group_id`) — is real at the SQL
//! level, but `group_membership` also has a SECOND, independent FK to
//! `groups` (`member_group_id`, for the group-inside-group case).
//! sea-orm's `EntityTrait::has_many` builder resolves its join purely
//! from `R: Related<Self>` (see `base_entity.rs`: `R::to().rev()`) — a
//! single `impl Related<groups::Entity> for group_membership::Entity`
//! can point at only ONE of those two columns, so a `has_many` here
//! would silently pick one FK's join condition over the other. Rather
//! than build convenience sugar that quietly ignores half of a real
//! relationship, `group_membership::Entity`'s own `Relation` enum
//! carries BOTH `belongs_to` variants explicitly (`Group` and
//! `MemberGroup`, each with its own `from`/`to`) and callers needing
//! either edge use those directly — a real, defensible reason per this
//! PR's own brief, not an oversight.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "groups")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = false)]
    pub group_id: String,
    pub name: String,
    pub is_sysadmin: bool,
    pub created_at: String,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {
    #[sea_orm(has_many = "super::group_capability::Entity")]
    GroupCapability,
    #[sea_orm(has_many = "super::project_membership::Entity")]
    ProjectMembership,
}

impl Related<super::group_capability::Entity> for Entity {
    fn to() -> RelationDef {
        Relation::GroupCapability.def()
    }
}

impl Related<super::project_membership::Entity> for Entity {
    fn to() -> RelationDef {
        Relation::ProjectMembership.def()
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
        let path = dir.path().join("groups_entity_test.db");

        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            init_router_schema(&conn).unwrap();
            conn.execute(
                "INSERT INTO groups (group_id, name, is_sysadmin, created_at) \
                 VALUES ('g1', 'admins', 1, '2026-01-01T00:00:00Z')",
                [],
            )
            .unwrap();
        }

        let url = format!("sqlite://{}", path.display());
        let db = Database::connect(&url).await.unwrap();

        let rows = Entity::find().all(&db).await.unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].group_id, "g1");
        assert_eq!(rows[0].name, "admins");
        assert!(rows[0].is_sysadmin);

        let am = ActiveModel {
            group_id: Set("g2".to_string()),
            name: Set("viewers".to_string()),
            is_sysadmin: Set(false),
            created_at: Set("2026-01-02T00:00:00Z".to_string()),
        };
        am.insert(&db).await.unwrap();
        drop(db);

        let conn = rusqlite::Connection::open(&path).unwrap();
        let (name, is_sysadmin): (String, i64) = conn
            .query_row(
                "SELECT name, is_sysadmin FROM groups WHERE group_id = 'g2'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(name, "viewers");
        assert_eq!(is_sysadmin, 0);
    }
}
