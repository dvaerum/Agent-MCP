//! sea-orm `Entity` for `users` — lives on the ROUTER db
//! (`schema::init_router_schema`), not the per-project agent db, same
//! distinction `group_capability.rs`'s own module doc already
//! establishes (confirmed directly against `schema.rs`, not assumed).
//!
//! `is_sysadmin` is `INTEGER NOT NULL DEFAULT 0` in SQL, mapped to
//! `bool` here — the same INTEGER-as-bool mapping `scheduled_directive`
//! already uses for its own `enabled` column.
//!
//! `Relation` has three `has_many` edges to every table with an
//! unambiguous single FK back to `users` (`sessions.user_id`,
//! `group_membership.member_user_id`, `project_membership.user_id`).
//! `group_membership` also has an FK to `groups` twice over
//! (`group_id`/`member_group_id`), but never a second FK to `users`,
//! so `group_membership::Entity`'s `Related<users::Entity>` impl is
//! unambiguous and this `has_many` (which resolves via that impl,
//! see `EntityTrait::has_many`'s own `R: Related<Self>` bound) compiles
//! cleanly.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "users")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = false)]
    pub user_id: String,
    pub username: String,
    pub email: Option<String>,
    pub password_hash: Option<String>,
    pub created_at: String,
    pub last_login_at: Option<String>,
    pub is_sysadmin: bool,
    pub sso_subject: Option<String>,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {
    #[sea_orm(has_many = "super::sessions::Entity")]
    Sessions,
    #[sea_orm(has_many = "super::group_membership::Entity")]
    GroupMembership,
    #[sea_orm(has_many = "super::project_membership::Entity")]
    ProjectMembership,
}

impl Related<super::sessions::Entity> for Entity {
    fn to() -> RelationDef {
        Relation::Sessions.def()
    }
}

impl Related<super::group_membership::Entity> for Entity {
    fn to() -> RelationDef {
        Relation::GroupMembership.def()
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
        let path = dir.path().join("users_entity_test.db");

        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            init_router_schema(&conn).unwrap();
            conn.execute(
                "INSERT INTO users (user_id, username, email, password_hash, created_at, \
                 last_login_at, is_sysadmin, sso_subject) \
                 VALUES ('u1', 'alice', 'alice@example.com', 'hash', '2026-01-01T00:00:00Z', \
                 NULL, 1, NULL)",
                [],
            )
            .unwrap();
        }

        let url = format!("sqlite://{}", path.display());
        let db = Database::connect(&url).await.unwrap();

        let rows = Entity::find().all(&db).await.unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].user_id, "u1");
        assert_eq!(rows[0].username, "alice");
        assert_eq!(rows[0].email.as_deref(), Some("alice@example.com"));
        assert_eq!(rows[0].password_hash.as_deref(), Some("hash"));
        assert!(rows[0].last_login_at.is_none());
        assert!(rows[0].is_sysadmin);
        assert!(rows[0].sso_subject.is_none());

        let am = ActiveModel {
            user_id: Set("u2".to_string()),
            username: Set("bob".to_string()),
            email: Set(None),
            password_hash: Set(None),
            created_at: Set("2026-01-02T00:00:00Z".to_string()),
            last_login_at: Set(None),
            is_sysadmin: Set(false),
            sso_subject: Set(Some("oidc:bob".to_string())),
        };
        am.insert(&db).await.unwrap();
        drop(db);

        let conn = rusqlite::Connection::open(&path).unwrap();
        let (username, sso_subject, is_sysadmin): (String, Option<String>, i64) = conn
            .query_row(
                "SELECT username, sso_subject, is_sysadmin FROM users WHERE user_id = 'u2'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(username, "bob");
        assert_eq!(sso_subject.as_deref(), Some("oidc:bob"));
        assert_eq!(is_sysadmin, 0);
    }
}
