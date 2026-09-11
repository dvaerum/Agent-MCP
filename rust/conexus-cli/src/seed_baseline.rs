//! `conexus-cli seed-baseline` -- the real adoption tool for cutting a
//! REAL, already-Alembic-migrated database over to `sea-orm-migration`
//! schema authority (see `conexus_db::migration`'s own module doc).
//!
//! Deliberately more cautious than `conexus-cli migrate`/`router
//! migrate`: this is the ONE command that writes to `seaql_migrations`
//! for a database this tool has never touched before, so a database
//! whose real on-disk schema doesn't actually match the baseline gets
//! REFUSED, never silently marked "fully migrated" -- confirmed via
//! `conexus_db::migration::verify::diff_schema` against a fresh
//! `:memory:` database this process just built with the real
//! `Migrator`/`RouterMigrator` baseline, not assumed from the caller's
//! own say-so.
//!
//! Defaults to dry-run (report only); `--apply` is required to
//! actually write the tracking row. Running this command at all is
//! still safe to do against a real production file -- it never
//! touches any table but `seaql_migrations`, and even that write is
//! gated on the diff coming back clean -- but the actual go-ahead to
//! run it with `--apply` against the 3 real live production databases
//! is a separate, explicit operator decision (see the plan's own
//! standing distinction), not something this tool decides on its own.

use std::path::Path;

use conexus_db::migration::verify::diff_schema;
use conexus_db::migration::{Migrator, MigratorTrait, RouterMigrator};
use sea_orm::{
    ActiveModelTrait, ConnectionTrait, Database, DatabaseConnection, DbErr, EntityTrait,
};
use sea_orm_migration::seaql_migrations;

#[derive(Debug, Clone, Copy, PartialEq, Eq, clap::ValueEnum)]
pub enum Kind {
    Project,
    Router,
}

/// A fresh `:memory:` database with the real baseline already applied
/// -- the "what the schema SHOULD look like" side of the diff.
async fn reference_db(kind: Kind) -> Result<DatabaseConnection, DbErr> {
    let db = Database::connect("sqlite::memory:").await?;
    db.execute_unprepared("PRAGMA foreign_keys = ON;").await?;
    match kind {
        Kind::Project => Migrator::up(&db, None).await?,
        Kind::Router => RouterMigrator::up(&db, None).await?,
    }
    Ok(db)
}

/// The migration version strings this baseline expects to see marked
/// applied -- always exactly one today (see the migration module's own
/// "baseline, not a replay" doc), but written to generalize cleanly if
/// a second baseline migration is ever added.
fn expected_versions(kind: Kind) -> Vec<String> {
    match kind {
        Kind::Project => Migrator::migrations()
            .into_iter()
            .map(|m| m.name().to_string())
            .collect(),
        Kind::Router => RouterMigrator::migrations()
            .into_iter()
            .map(|m| m.name().to_string())
            .collect(),
    }
}

/// Marks every expected version as applied, skipping any that's
/// already recorded -- idempotent, so a second `--apply` run against
/// an already-seeded database is a safe no-op rather than a duplicate-
/// primary-key error.
async fn seed_tracking_table(
    db: &DatabaseConnection,
    kind: Kind,
    now_unix: i64,
) -> Result<u32, DbErr> {
    match kind {
        Kind::Project => Migrator::install(db).await?,
        Kind::Router => RouterMigrator::install(db).await?,
    }

    let already_applied: std::collections::HashSet<String> = seaql_migrations::Entity::find()
        .all(db)
        .await?
        .into_iter()
        .map(|m| m.version)
        .collect();

    let mut newly_marked = 0u32;
    for version in expected_versions(kind) {
        if already_applied.contains(&version) {
            continue;
        }
        seaql_migrations::ActiveModel {
            version: sea_orm::ActiveValue::Set(version),
            applied_at: sea_orm::ActiveValue::Set(now_unix),
        }
        .insert(db)
        .await?;
        newly_marked += 1;
    }
    Ok(newly_marked)
}

pub async fn run(db_path: &Path, kind: Kind, apply: bool) -> anyhow::Result<()> {
    if !db_path.exists() {
        anyhow::bail!("database not found at {}", db_path.display());
    }

    let target = Database::connect(format!("sqlite://{}", db_path.display())).await?;
    let reference = reference_db(kind).await?;

    let diff = diff_schema(&target, &reference).await?;
    if !diff.is_empty() {
        anyhow::bail!(
            "{} does NOT structurally match the {:?} baseline -- refusing to seed:\n{}",
            db_path.display(),
            kind,
            diff
        );
    }

    if !apply {
        println!(
            "{} matches the {:?} baseline exactly. Dry run only -- pass --apply to mark it as \
             already migrated.",
            db_path.display(),
            kind
        );
        return Ok(());
    }

    let now_unix = chrono::Utc::now().timestamp();
    let newly_marked = seed_tracking_table(&target, kind, now_unix).await?;
    println!(
        "{} matches the {:?} baseline. Marked {newly_marked} migration(s) as applied \
         (already-applied ones left untouched).",
        db_path.display(),
        kind
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn scratch_path(name: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "conexus-cli-seed-baseline-test-{name}-{}.db",
            std::process::id()
        ))
    }

    /// Builds a fixture matching the REAL target this tool exists for:
    /// a database whose schema already matches the baseline (standing
    /// in for a real Alembic-migrated database) but that
    /// sea-orm-migration has never touched -- no `seaql_migrations`
    /// table at all. Applies each migration's `up()` directly against
    /// a bare `SchemaManager`, bypassing `Migrator::up`'s own
    /// `install()` call entirely, so the tracking table is never
    /// created as a side effect.
    ///
    /// Deliberately NOT `conexus_db::schema::init_schema`/
    /// `init_router_schema` -- caught by a genuinely failing first
    /// draft of this test suite: that DDL is Rust-tests-only and
    /// already knowingly behind real Alembic HEAD (it's exactly the
    /// gap the baseline migration's own module doc lists -- missing
    /// `mcp_sessions`, 3 FKs, DESC index ordering -- so it produces a
    /// real, expected diff against the baseline, not a clean match).
    async fn seed_legacy_schema(path: &Path, kind: Kind) {
        let db = Database::connect(format!("sqlite://{}?mode=rwc", path.display()))
            .await
            .unwrap();
        db.execute_unprepared("PRAGMA foreign_keys = ON;")
            .await
            .unwrap();
        let manager = sea_orm_migration::SchemaManager::new(&db);
        let migrations: Vec<Box<dyn sea_orm_migration::MigrationTrait>> = match kind {
            Kind::Project => Migrator::migrations(),
            Kind::Router => RouterMigrator::migrations(),
        };
        for m in migrations {
            m.up(&manager).await.unwrap();
        }
    }

    #[tokio::test]
    async fn dry_run_against_a_genuinely_matching_project_db_reports_clean_and_writes_nothing() {
        let path = scratch_path("project-dry-run");
        seed_legacy_schema(&path, Kind::Project).await;

        run(&path, Kind::Project, false).await.unwrap();

        // No seaql_migrations table should exist at all -- a dry run
        // must never call `install()`.
        let db = Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        let count: i64 = db
            .query_one_raw(sea_orm::Statement::from_string(
                db.get_database_backend(),
                "SELECT COUNT(*) AS c FROM sqlite_master WHERE type='table' AND name='seaql_migrations'",
            ))
            .await
            .unwrap()
            .unwrap()
            .try_get("", "c")
            .unwrap();
        assert_eq!(count, 0, "dry run must not create the tracking table");
        std::fs::remove_file(&path).ok();
    }

    #[tokio::test]
    async fn apply_against_a_genuinely_matching_project_db_marks_it_applied() {
        let path = scratch_path("project-apply");
        seed_legacy_schema(&path, Kind::Project).await;

        run(&path, Kind::Project, true).await.unwrap();

        let db = Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        let rows = seaql_migrations::Entity::find().all(&db).await.unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].version, "m20260911_000001_baseline");
        std::fs::remove_file(&path).ok();
    }

    #[tokio::test]
    async fn apply_is_idempotent_against_an_already_seeded_database() {
        let path = scratch_path("project-apply-twice");
        seed_legacy_schema(&path, Kind::Project).await;

        run(&path, Kind::Project, true).await.unwrap();
        // A second real run must not error on a duplicate primary key.
        run(&path, Kind::Project, true).await.unwrap();

        let db = Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        let rows = seaql_migrations::Entity::find().all(&db).await.unwrap();
        assert_eq!(rows.len(), 1, "seeding twice must not duplicate the row");
        std::fs::remove_file(&path).ok();
    }

    #[tokio::test]
    async fn a_structurally_different_database_is_refused_even_with_apply() {
        let path = scratch_path("project-mismatch");
        let db = Database::connect(format!("sqlite://{}?mode=rwc", path.display()))
            .await
            .unwrap();
        // A genuinely different schema: missing every real table.
        db.execute_unprepared("CREATE TABLE unrelated_table (id INTEGER PRIMARY KEY);")
            .await
            .unwrap();

        let err = run(&path, Kind::Project, true).await.unwrap_err();
        assert!(
            err.to_string().contains("refusing to seed"),
            "expected a refusal, got: {err}"
        );

        // Confirm the refusal actually left no tracking table behind.
        let count: i64 = db
            .query_one_raw(sea_orm::Statement::from_string(
                db.get_database_backend(),
                "SELECT COUNT(*) AS c FROM sqlite_master WHERE type='table' AND name='seaql_migrations'",
            ))
            .await
            .unwrap()
            .unwrap()
            .try_get("", "c")
            .unwrap();
        assert_eq!(
            count, 0,
            "a refused seed must not create the tracking table"
        );
        std::fs::remove_file(&path).ok();
    }

    #[tokio::test]
    async fn router_kind_seeds_the_router_baseline() {
        let path = scratch_path("router-apply");
        seed_legacy_schema(&path, Kind::Router).await;

        run(&path, Kind::Router, true).await.unwrap();

        let db = Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        let rows = seaql_migrations::Entity::find().all(&db).await.unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].version, "m20260911_000002_router_baseline");
        std::fs::remove_file(&path).ok();
    }

    #[tokio::test]
    async fn a_missing_database_file_is_a_clean_error() {
        let path = scratch_path("does-not-exist");
        let err = run(&path, Kind::Project, false).await.unwrap_err();
        assert!(err.to_string().contains("database not found"));
    }
}
