//! Router database (`router.db`) baseline migration -- the complete
//! schema at Alembic's real HEAD revision (`0006_group_membership_
//! unique`), confirmed live against the real production `router.db`
//! before this file was written (`SELECT version_num FROM
//! alembic_version` returned exactly that revision).
//!
//! Unlike the per-project baseline (`m20260911_000001_baseline`), no
//! drift was found here: `crate::schema::init_router_schema` already
//! has full parity with Alembic HEAD (every FK, every CHECK
//! constraint, every partial unique index from migrations 0002/0005/
//! 0006). This migration reuses that DDL verbatim via
//! `execute_unprepared` (confirmed to support multi-statement strings
//! against the real sqlx-sqlite backend) -- a straight port, not a
//! re-derivation.

use sea_orm_migration::prelude::*;

#[derive(DeriveMigrationName)]
pub struct Migration;

#[async_trait::async_trait]
impl MigrationTrait for Migration {
    async fn up(&self, manager: &SchemaManager) -> Result<(), DbErr> {
        let db = manager.get_connection();
        db.execute_unprepared(
            r#"
            CREATE TABLE IF NOT EXISTS users (
                user_id        TEXT PRIMARY KEY,
                username       TEXT UNIQUE NOT NULL,
                email          TEXT,
                password_hash  TEXT,
                created_at     TEXT NOT NULL,
                last_login_at  TEXT,
                is_sysadmin    INTEGER NOT NULL DEFAULT 0,
                sso_subject    TEXT
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_users_sso_subject
                ON users(sso_subject)
                WHERE sso_subject IS NOT NULL;

            CREATE TABLE IF NOT EXISTS groups (
                group_id     TEXT PRIMARY KEY,
                name         TEXT NOT NULL UNIQUE,
                is_sysadmin  INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS group_capability (
                group_id    TEXT NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
                capability  TEXT NOT NULL,
                PRIMARY KEY (group_id, capability)
            );

            CREATE TABLE IF NOT EXISTS group_membership (
                group_id         TEXT NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
                member_user_id   TEXT REFERENCES users(user_id) ON DELETE CASCADE,
                member_group_id  TEXT REFERENCES groups(group_id) ON DELETE CASCADE,
                added_at         TEXT NOT NULL,
                CHECK ((member_user_id IS NOT NULL) <> (member_group_id IS NOT NULL))
            );

            CREATE INDEX IF NOT EXISTS idx_group_membership_group_id
                ON group_membership(group_id);
            CREATE INDEX IF NOT EXISTS idx_group_membership_member_user_id
                ON group_membership(member_user_id);
            CREATE INDEX IF NOT EXISTS idx_group_membership_member_group_id
                ON group_membership(member_group_id);

            CREATE UNIQUE INDEX IF NOT EXISTS uq_group_membership_user
                ON group_membership(group_id, member_user_id)
                WHERE member_user_id IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS uq_group_membership_group
                ON group_membership(group_id, member_group_id)
                WHERE member_group_id IS NOT NULL;

            CREATE TABLE IF NOT EXISTS sessions (
                session_id    TEXT PRIMARY KEY,
                user_id       TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                created_at    TEXT NOT NULL,
                expires_at    TEXT NOT NULL,
                last_used_at  TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);

            CREATE TABLE IF NOT EXISTS project_membership (
                project_name  TEXT NOT NULL,
                user_id       TEXT REFERENCES users(user_id) ON DELETE CASCADE,
                group_id      TEXT REFERENCES groups(group_id) ON DELETE CASCADE,
                role          TEXT NOT NULL DEFAULT 'operator'
                              CHECK (role IN ('operator', 'viewer')),
                CHECK ((user_id IS NOT NULL) <> (group_id IS NOT NULL))
            );

            CREATE INDEX IF NOT EXISTS idx_project_membership_user_id
                ON project_membership(user_id);
            CREATE INDEX IF NOT EXISTS idx_project_membership_group_id
                ON project_membership(group_id);
            CREATE INDEX IF NOT EXISTS idx_project_membership_project_name
                ON project_membership(project_name);

            CREATE UNIQUE INDEX IF NOT EXISTS uq_project_membership_user
                ON project_membership(project_name, user_id)
                WHERE user_id IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS uq_project_membership_group
                ON project_membership(project_name, group_id)
                WHERE group_id IS NOT NULL;
            "#,
        )
        .await?;
        Ok(())
    }
}
