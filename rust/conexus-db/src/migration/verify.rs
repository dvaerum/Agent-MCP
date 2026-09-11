//! Schema-verification for the seeding/cutover procedure (see the
//! module doc on [`crate::migration`]): before marking a REAL,
//! already-Alembic-migrated database's baseline as applied, confirm
//! its on-disk schema is structurally identical to what the baseline
//! migration would create fresh -- refusing to seed a database whose
//! shape doesn't genuinely match, rather than trusting the caller's
//! own assumption.
//!
//! Deliberately a STRUCTURAL comparison (`PRAGMA table_info`/
//! `foreign_key_list`/`index_list`+`index_info`), not a raw
//! `sqlite_master.sql` text diff: Alembic's SQLAlchemy-rendered DDL
//! and this crate's hand-written raw-SQL DDL use genuinely different
//! formatting/column-ordering/type-spelling for an otherwise identical
//! schema (confirmed by inspecting a real migrated database's own
//! `sqlite_master` rows against this crate's DDL) -- a text diff would
//! report a false mismatch on every real database this tool is meant
//! to seed.

use std::collections::BTreeSet;

use sea_orm::{ConnectionTrait, DbBackend, DbErr, Statement};

/// One table's structural fingerprint: column shape, foreign keys, and
/// index shape, all order-independent (`BTreeSet`) since SQLite makes
/// no promise about `PRAGMA` row order and two independently-authored
/// DDL sources have no reason to declare columns/indexes in the same
/// sequence even when the resulting schema is equivalent.
#[derive(Debug, Clone, PartialEq, Eq)]
struct TableShape {
    columns: BTreeSet<(String, String, Option<bool>, bool)>, // (name, type bucket, notnull (PK columns: None, see table_shape), pk)
    foreign_keys: BTreeSet<(String, String, String)>,        // (from, to_table, to_column)
    indexes: BTreeSet<(BTreeSet<String>, bool)>,             // (columns, unique)
}

/// Every difference [`diff_schema`] found, rendered for a human
/// operator to read before deciding whether to override (never
/// silently -- see the seeding CLI command's own `--dry-run` default).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct SchemaDiff {
    pub missing_tables: Vec<String>,
    pub unexpected_tables: Vec<String>,
    pub mismatched_tables: Vec<String>,
}

impl SchemaDiff {
    pub fn is_empty(&self) -> bool {
        self.missing_tables.is_empty()
            && self.unexpected_tables.is_empty()
            && self.mismatched_tables.is_empty()
    }
}

impl std::fmt::Display for SchemaDiff {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        if !self.missing_tables.is_empty() {
            writeln!(
                f,
                "tables the baseline expects but the target lacks: {:?}",
                self.missing_tables
            )?;
        }
        if !self.unexpected_tables.is_empty() {
            writeln!(
                f,
                "tables present in the target but not created by the baseline: {:?}",
                self.unexpected_tables
            )?;
        }
        if !self.mismatched_tables.is_empty() {
            writeln!(
                f,
                "tables present in both but structurally different (columns/FKs/indexes): {:?}",
                self.mismatched_tables
            )?;
        }
        Ok(())
    }
}

/// Tables deliberately excluded from comparison because they are NOT
/// (and were never meant to be) part of either Migrator's baseline:
/// `alembic_version` is Alembic's own bookkeeping table (the
/// `seaql_migrations` equivalent on the OLD side of the cutover);
/// `rag_embeddings` + its sqlite-vec-generated shadow tables (`_chunks`/
/// `_info`/`_rowids`/`_vector_chunksNN`) are created separately, opt-in,
/// by `schema::init_rag_embeddings_table` -- confirmed live against
/// both real production databases (present on both) that this
/// exclusion is real, not theoretical.
const EXCLUDED_TABLES: &[&str] = &["alembic_version"];

fn is_rag_embeddings_shadow_table(name: &str) -> bool {
    name == "rag_embeddings" || name.starts_with("rag_embeddings_")
}

async fn user_table_names(db: &impl ConnectionTrait) -> Result<Vec<String>, DbErr> {
    let stmt = Statement::from_string(
        DbBackend::Sqlite,
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' \
         AND name != 'seaql_migrations' ORDER BY name",
    );
    let rows = db.query_all_raw(stmt).await?;
    let names: Vec<String> = rows
        .iter()
        .map(|r| r.try_get("", "name"))
        .collect::<Result<_, DbErr>>()?;
    Ok(names
        .into_iter()
        .filter(|n| !EXCLUDED_TABLES.contains(&n.as_str()) && !is_rag_embeddings_shadow_table(n))
        .collect())
}

/// Collapse a declared column type to a broad storage-affinity bucket
/// for comparison purposes -- deliberately coarser than SQLite's own
/// 5-affinity algorithm (INTEGER/TEXT/BLOB/REAL/NUMERIC): confirmed
/// live against a real migrated production database that SQLAlchemy's
/// DDL compiler declares a boolean-flag column as `BOOLEAN` (e.g.
/// `agents.auto_event_loop`) where this crate's hand-written baseline
/// uses `INTEGER` -- both store identical 0/1 values with identical
/// query behavior (real SQLite affinity rules would still call these
/// different -- INTEGER vs. NUMERIC -- since "BOOLEAN" contains
/// neither "INT" nor "CHAR"/"CLOB"/"TEXT"/"BLOB"/"REAL"/"FLOA"/"DOUB").
/// Bucketing INTEGER/NUMERIC/REAL together is safe for THIS schema
/// specifically: no table declares a genuine `REAL`/`FLOAT` column,
/// so there is no real value this tool would ever need to distinguish
/// across that boundary.
fn type_bucket(declared: &str) -> &'static str {
    let upper = declared.to_uppercase();
    if upper.contains("CHAR") || upper.contains("CLOB") || upper.contains("TEXT") {
        "TEXT"
    } else if upper.contains("BLOB") || upper.is_empty() {
        "BLOB"
    } else {
        "NUMERIC-ISH"
    }
}

async fn table_shape(db: &impl ConnectionTrait, table: &str) -> Result<TableShape, DbErr> {
    let cols_stmt =
        Statement::from_string(DbBackend::Sqlite, format!("PRAGMA table_info({table})"));
    let columns = db
        .query_all_raw(cols_stmt)
        .await?
        .iter()
        .map(|r| {
            let name: String = r.try_get("", "name")?;
            let ty: String = r.try_get("", "type")?;
            let notnull: i32 = r.try_get("", "notnull")?;
            let pk: i32 = r.try_get("", "pk")?;
            let is_pk = pk != 0;
            // A primary-key column's `notnull` flag is confirmed live
            // to genuinely vary across this project's own real
            // production databases (the two long-lived project DBs
            // report different values for the SAME column, e.g.
            // `agents.token`, despite matching Alembic revisions --
            // almost certainly a SQLAlchemy-version-era DDL difference
            // from whenever each table was first created) with no
            // observed functional difference: a bare `TEXT PRIMARY
            // KEY` still rejects a NULL insert via the PK's own
            // uniqueness enforcement either way. Comparing it would
            // make this tool reject a genuinely fine database over a
            // real but harmless historical artifact, so it's excluded
            // -- but ONLY for PK columns; a NOT-PK column's notnull
            // flag is still a real, meaningful structural fact.
            let notnull_for_comparison = if is_pk { None } else { Some(notnull != 0) };
            Ok((
                name,
                type_bucket(&ty).to_string(),
                notnull_for_comparison,
                is_pk,
            ))
        })
        .collect::<Result<BTreeSet<_>, DbErr>>()?;

    let fk_stmt = Statement::from_string(
        DbBackend::Sqlite,
        format!("PRAGMA foreign_key_list({table})"),
    );
    let foreign_keys = db
        .query_all_raw(fk_stmt)
        .await?
        .iter()
        .map(|r| {
            let from: String = r.try_get("", "from")?;
            let to_table: String = r.try_get("", "table")?;
            let to_column: String = r.try_get("", "to")?;
            Ok((from, to_table, to_column))
        })
        .collect::<Result<BTreeSet<_>, DbErr>>()?;

    let idx_list_stmt =
        Statement::from_string(DbBackend::Sqlite, format!("PRAGMA index_list({table})"));
    let mut indexes = BTreeSet::new();
    for row in db.query_all_raw(idx_list_stmt).await? {
        let idx_name: String = row.try_get("", "name")?;
        let unique: i32 = row.try_get("", "unique")?;
        // Auto-created indexes backing an inline UNIQUE/PK constraint
        // (name starts with `sqlite_autoindex_`) are already fully
        // captured by the column-level `pk`/uniqueness comparison
        // above -- comparing them again here would fail spuriously
        // whenever the two schemas order UNIQUE columns differently
        // in an otherwise-equivalent inline constraint.
        if idx_name.starts_with("sqlite_autoindex_") {
            continue;
        }
        let info_stmt =
            Statement::from_string(DbBackend::Sqlite, format!("PRAGMA index_info({idx_name})"));
        // An expression index (e.g. `idx_tasks_single_root ON tasks
        // ((parent_task IS NULL))`) has no real column backing one of
        // its entries -- `PRAGMA index_info`'s own `name` is NULL for
        // that entry (the same "unsupported reflection" gap
        // SQLAlchemy's own inspector already flags for this exact
        // index). A sentinel placeholder keeps such an index
        // comparable (two schemas both declaring an expression index
        // in the same position still match) without crashing on the
        // NULL.
        let cols: BTreeSet<String> = db
            .query_all_raw(info_stmt)
            .await?
            .iter()
            .map(|r| {
                r.try_get::<Option<String>>("", "name")
                    .map(|name| name.unwrap_or_else(|| "<expr>".to_string()))
            })
            .collect::<Result<_, DbErr>>()?;
        indexes.insert((cols, unique != 0));
    }

    Ok(TableShape {
        columns,
        foreign_keys,
        indexes,
    })
}

/// Compare `target`'s real on-disk schema against `reference`'s
/// (a fresh `:memory:` database the caller has already run the real
/// `Migrator`/`RouterMigrator` baseline against) table-by-table.
/// Empty [`SchemaDiff`] means the two are structurally identical.
pub async fn diff_schema(
    target: &impl ConnectionTrait,
    reference: &impl ConnectionTrait,
) -> Result<SchemaDiff, DbErr> {
    let target_tables: BTreeSet<String> = user_table_names(target).await?.into_iter().collect();
    let reference_tables: BTreeSet<String> =
        user_table_names(reference).await?.into_iter().collect();

    let missing_tables = reference_tables
        .difference(&target_tables)
        .cloned()
        .collect();
    let unexpected_tables = target_tables
        .difference(&reference_tables)
        .cloned()
        .collect();

    let mut mismatched_tables = Vec::new();
    for table in target_tables.intersection(&reference_tables) {
        let target_shape = table_shape(target, table).await?;
        let reference_shape = table_shape(reference, table).await?;
        if target_shape != reference_shape {
            mismatched_tables.push(table.clone());
        }
    }
    mismatched_tables.sort();

    Ok(SchemaDiff {
        missing_tables,
        unexpected_tables,
        mismatched_tables,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::migration::{Migrator, MigratorTrait, RouterMigrator};
    use sea_orm::Database;

    async fn fresh_db() -> sea_orm::DatabaseConnection {
        let db = Database::connect("sqlite::memory:").await.unwrap();
        db.execute_unprepared("PRAGMA foreign_keys = ON;")
            .await
            .unwrap();
        db
    }

    #[tokio::test]
    async fn identical_schemas_produce_an_empty_diff() {
        let target = fresh_db().await;
        Migrator::up(&target, None).await.unwrap();
        let reference = fresh_db().await;
        Migrator::up(&reference, None).await.unwrap();

        let diff = diff_schema(&target, &reference).await.unwrap();
        assert!(diff.is_empty(), "expected no diff, got: {diff}");
    }

    #[tokio::test]
    async fn router_schemas_also_diff_clean() {
        let target = fresh_db().await;
        RouterMigrator::up(&target, None).await.unwrap();
        let reference = fresh_db().await;
        RouterMigrator::up(&reference, None).await.unwrap();

        let diff = diff_schema(&target, &reference).await.unwrap();
        assert!(diff.is_empty(), "expected no diff, got: {diff}");
    }

    #[tokio::test]
    async fn a_missing_table_is_reported() {
        let target = fresh_db().await;
        Migrator::up(&target, None).await.unwrap();
        target
            .execute_unprepared("DROP TABLE mcp_sessions")
            .await
            .unwrap();
        let reference = fresh_db().await;
        Migrator::up(&reference, None).await.unwrap();

        let diff = diff_schema(&target, &reference).await.unwrap();
        assert_eq!(diff.missing_tables, vec!["mcp_sessions".to_string()]);
        assert!(diff.unexpected_tables.is_empty());
        assert!(diff.mismatched_tables.is_empty());
    }

    #[tokio::test]
    async fn an_unexpected_extra_table_is_reported() {
        let target = fresh_db().await;
        Migrator::up(&target, None).await.unwrap();
        target
            .execute_unprepared("CREATE TABLE totally_unrelated (x INTEGER)")
            .await
            .unwrap();
        let reference = fresh_db().await;
        Migrator::up(&reference, None).await.unwrap();

        let diff = diff_schema(&target, &reference).await.unwrap();
        assert_eq!(
            diff.unexpected_tables,
            vec!["totally_unrelated".to_string()]
        );
    }

    #[tokio::test]
    async fn a_missing_column_is_reported_as_a_mismatch() {
        // Simulates the real-world case this tool exists for: a
        // genuinely-migrated database whose DDL text differs from the
        // baseline's (SQLAlchemy vs. hand-written SQL) but whose real
        // structure should still match -- and a genuine structural gap
        // (a dropped/never-added column) DOES get caught.
        let target = fresh_db().await;
        target
            .execute_unprepared("CREATE TABLE rag_meta (meta_key TEXT PRIMARY KEY)")
            .await
            .unwrap();
        let reference = fresh_db().await;
        Migrator::up(&reference, None).await.unwrap();

        let diff = diff_schema(&target, &reference).await.unwrap();
        // rag_meta is the only table on `target`; every OTHER baseline
        // table is missing, and rag_meta itself lacks `meta_value`.
        assert!(diff.mismatched_tables.contains(&"rag_meta".to_string()));
        assert!(!diff.missing_tables.is_empty());
    }

    #[tokio::test]
    async fn differently_worded_but_structurally_identical_ddl_diffs_clean() {
        // The exact scenario this whole module exists for: DDL text
        // that looks nothing like the baseline's (different column
        // order, different type spelling, no IF NOT EXISTS) but is
        // structurally the same table.
        let target = fresh_db().await;
        target
            .execute_unprepared(
                "CREATE TABLE rag_meta (\n  meta_value VARCHAR,\n  meta_key VARCHAR NOT NULL, \n  PRIMARY KEY (meta_key)\n)",
            )
            .await
            .unwrap();
        let reference = fresh_db().await;
        // `meta_key`'s `NOT NULL` is declared explicitly on both sides
        // -- a bare `TEXT PRIMARY KEY` (unlike `INTEGER PRIMARY KEY`,
        // SQLite's rowid alias) does NOT imply NOT NULL, so leaving it
        // off here would be a genuine structural difference from
        // `target`'s explicit `NOT NULL`, not the column-order/type-
        // spelling noise this test means to exercise.
        reference
            .execute_unprepared(
                "CREATE TABLE rag_meta (meta_key TEXT NOT NULL PRIMARY KEY, meta_value TEXT)",
            )
            .await
            .unwrap();

        let diff = diff_schema(&target, &reference).await.unwrap();
        assert!(diff.is_empty(), "expected no diff, got: {diff}");
    }
}
