//! `conexus-cli migrate` / `conexus-cli router migrate` -- the
//! Rust-side replacement for Alembic's own `alembic upgrade head` CLI
//! invocation (schema authority, see `conexus_db::migration`'s own
//! module doc). Idempotent by construction: every statement in both
//! baseline migrations is `CREATE TABLE/INDEX/TRIGGER IF NOT EXISTS`,
//! so running this against an already-migrated database (once its
//! `seaql_migrations` tracking table has been seeded -- see
//! `conexus-cli seed-baseline`, a separate, deliberately more
//! cautious command) is a safe no-op, and running it against a
//! genuinely fresh database creates the real schema for the first
//! time.
//!
//! Deliberately NOT the tool that adopts an EXISTING, already-
//! Alembic-migrated database: this command trusts `seaql_migrations`'
//! own bookkeeping at face value (via `MigratorTrait::up`, which
//! calls `install` + runs only what it doesn't already see recorded
//! as applied). A database whose real on-disk schema doesn't actually
//! match the baseline, with no tracking row yet, would have every
//! `IF NOT EXISTS` statement silently do nothing for a table that
//! already exists but is structurally wrong, then mark itself as
//! "fully migrated" regardless -- `seed-baseline`'s whole job is
//! confirming that risk doesn't apply BEFORE writing that tracking
//! row for a database this tool has never touched before.

use std::path::{Path, PathBuf};

use conexus_db::migration::{Migrator, MigratorTrait, RouterMigrator};
use conexus_db::schema::init_schema;
use rusqlite::Connection;

/// The project's live database path -- same convention as
/// `backup::db_path_for`.
fn project_db_path(project_dir: &Path) -> PathBuf {
    project_dir.join(".agent").join("mcp_state.db")
}

pub async fn run_project(project_dir: &Path) -> anyhow::Result<()> {
    let db_path = project_db_path(project_dir);
    if let Some(parent) = db_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    // A genuinely fresh database has no file at all yet; opening it
    // via rusqlite first (matching `conexus-backend::boot::
    // open_and_init_db`'s own real invocation order) creates the file
    // and the sqlite-vec-adjacent PRAGMAs this project's other tools
    // expect, before sea-orm ever touches it.
    {
        let conn = Connection::open(&db_path)?;
        conn.pragma_update(None, "foreign_keys", true)?;
        init_schema(&conn)?;
    }
    let db = sea_orm::Database::connect(format!("sqlite://{}", db_path.display())).await?;
    Migrator::up(&db, None).await?;
    println!("Project database at {} is up to date.", db_path.display());
    Ok(())
}

pub async fn run_router(router_db_path: &Path) -> anyhow::Result<()> {
    if let Some(parent) = router_db_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    {
        let conn = Connection::open(router_db_path)?;
        conn.pragma_update(None, "foreign_keys", true)?;
        conexus_db::schema::init_router_schema(&conn)?;
    }
    let db = sea_orm::Database::connect(format!("sqlite://{}", router_db_path.display())).await?;
    RouterMigrator::up(&db, None).await?;
    println!(
        "Router database at {} is up to date.",
        router_db_path.display()
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch_dir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "conexus-cli-migrate-test-{name}-{}",
            std::process::id()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[tokio::test]
    async fn run_project_creates_a_fresh_database() {
        let dir = scratch_dir("fresh-project");
        run_project(&dir).await.unwrap();

        let conn = Connection::open(project_db_path(&dir)).unwrap();
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'agents'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[tokio::test]
    async fn run_project_is_idempotent_against_an_already_migrated_database() {
        let dir = scratch_dir("idempotent-project");
        run_project(&dir).await.unwrap();
        // A second real run must not error -- exactly the boot-time
        // shape (every process start calls this unconditionally).
        run_project(&dir).await.unwrap();
        std::fs::remove_dir_all(&dir).ok();
    }

    #[tokio::test]
    async fn run_router_creates_a_fresh_database() {
        let dir = scratch_dir("fresh-router");
        let db_path = dir.join("router.db");
        run_router(&db_path).await.unwrap();

        let conn = Connection::open(&db_path).unwrap();
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'users'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[tokio::test]
    async fn run_router_is_idempotent() {
        let dir = scratch_dir("idempotent-router");
        let db_path = dir.join("router.db");
        run_router(&db_path).await.unwrap();
        run_router(&db_path).await.unwrap();
        std::fs::remove_dir_all(&dir).ok();
    }
}
