//! Port of `agent_mcp/repositories/project_settings_repository.py`.
//!
//! Byte-for-byte identical SQL/behavior shape to
//! [`crate::project_context_repository`] — same 5 functions, same
//! BL-R22-1 partial-update rule, same `&Connection`-only seam — the
//! ONLY difference is the table name. That's deliberate on both the
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
//! (`project_settings_tools.py` in Python) before it ever calls in
//! here, and stays there in the Rust port too (a future `conexus-
//! tools` concern, not `conexus-db`'s).

use crate::sql_util::{in_placeholders, to_sql_refs};
use rusqlite::{Connection, OptionalExtension, Result, Row};

/// One row of the `project_settings` table.
#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct ProjectSettingRow {
    pub context_key: String,
    pub value: String,
    pub description: Option<String>,
    pub created_at: Option<String>,
    pub created_by: Option<String>,
    pub updated_at: String,
    pub updated_by: String,
}

/// The two columns [`delete_many`] actually needs to report back —
/// deliberately not the full [`ProjectSettingRow`], matching the
/// narrower `SELECT` Python's version runs before deleting.
#[derive(Debug, Clone, PartialEq)]
pub struct DeletedSettingEntry {
    pub context_key: String,
    pub description: Option<String>,
}

const COLUMNS: &str =
    "context_key, value, description, created_at, created_by, updated_at, updated_by";

fn row_to_setting(row: &Row) -> rusqlite::Result<ProjectSettingRow> {
    Ok(ProjectSettingRow {
        context_key: row.get(0)?,
        value: row.get(1)?,
        description: row.get(2)?,
        created_at: row.get(3)?,
        created_by: row.get(4)?,
        updated_at: row.get(5)?,
        updated_by: row.get(6)?,
    })
}

/// Single-key lookup. Reads through the caller's own open connection
/// (typically mid-transaction), so an uncommitted write earlier in
/// the same transaction is visible here — load-bearing for
/// [`upsert`]'s existence check.
pub fn get(conn: &Connection, context_key: &str) -> Result<Option<ProjectSettingRow>> {
    conn.query_row(
        &format!("SELECT {COLUMNS} FROM project_settings WHERE context_key = ?1"),
        [context_key],
        row_to_setting,
    )
    .optional()
}

/// Full snapshot, ordered by key — backs `view_project_settings`/
/// `GET /api/settings-data`.
pub fn list_all(conn: &Connection) -> Result<Vec<ProjectSettingRow>> {
    let mut stmt = conn.prepare(&format!(
        "SELECT {COLUMNS} FROM project_settings ORDER BY context_key"
    ))?;
    let rows = stmt.query_map([], row_to_setting)?;
    rows.collect()
}

fn insert_new(
    conn: &Connection,
    context_key: &str,
    value: &str,
    description: Option<&str>,
    actor: &str,
    now: &str,
) -> Result<()> {
    conn.execute(
        "INSERT INTO project_settings (context_key, value, description, created_at, created_by, updated_at, updated_by) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?4, ?5)",
        (context_key, value, description, now, actor),
    )?;
    Ok(())
}

/// INSERT-or-UPDATE. On UPDATE, `description` is only overwritten
/// when `description_provided` is true — BL-R22-1's partial-update-
/// parity fix, identical to `project_context_repository::upsert`.
/// `created_at`/`created_by` are never touched on UPDATE.
pub fn upsert(
    conn: &Connection,
    context_key: &str,
    value: &str,
    description: Option<&str>,
    description_provided: bool,
    actor: &str,
    now: &str,
) -> Result<(ProjectSettingRow, bool)> {
    let created = get(conn, context_key)?.is_none();

    if created {
        insert_new(conn, context_key, value, description, actor, now)?;
    } else if description_provided {
        conn.execute(
            "UPDATE project_settings SET value = ?1, updated_at = ?2, updated_by = ?3, description = ?4 \
             WHERE context_key = ?5",
            (value, now, actor, description, context_key),
        )?;
    } else {
        conn.execute(
            "UPDATE project_settings SET value = ?1, updated_at = ?2, updated_by = ?3 WHERE context_key = ?4",
            (value, now, actor, context_key),
        )?;
    }

    let row = get(conn, context_key)?.expect("row was just written under this same connection");
    Ok((row, created))
}

/// INSERT-only — `None` (no write) if `context_key` already exists,
/// so the caller can map that to a `Conflict`.
pub fn create_new(
    conn: &Connection,
    context_key: &str,
    value: &str,
    description: Option<&str>,
    actor: &str,
    now: &str,
) -> Result<Option<ProjectSettingRow>> {
    if get(conn, context_key)?.is_some() {
        return Ok(None);
    }
    insert_new(conn, context_key, value, description, actor, now)?;
    get(conn, context_key)
}

/// Deletes rows for the given keys, returning only the entries that
/// actually existed. A no-op for an empty slice.
pub fn delete_many(conn: &Connection, context_keys: &[&str]) -> Result<Vec<DeletedSettingEntry>> {
    if context_keys.is_empty() {
        return Ok(Vec::new());
    }

    let select_sql = format!(
        "SELECT context_key, description FROM project_settings WHERE context_key IN ({})",
        in_placeholders(context_keys.len())
    );
    let params = to_sql_refs(context_keys);
    let mut stmt = conn.prepare(&select_sql)?;
    let existing: Vec<DeletedSettingEntry> = stmt
        .query_map(params.as_slice(), |row| {
            Ok(DeletedSettingEntry {
                context_key: row.get(0)?,
                description: row.get(1)?,
            })
        })?
        .collect::<Result<Vec<_>>>()?;
    drop(stmt);

    for entry in &existing {
        conn.execute(
            "DELETE FROM project_settings WHERE context_key = ?1",
            [&entry.context_key],
        )?;
    }

    Ok(existing)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::init_schema;

    fn test_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        conn
    }

    #[test]
    fn get_returns_none_for_unknown_key() {
        let conn = test_conn();
        assert_eq!(get(&conn, "nope").unwrap(), None);
    }

    #[test]
    fn upsert_creates_a_new_row_and_reports_created_true() {
        let conn = test_conn();
        let (row, created) = upsert(
            &conn,
            "config_max_agents",
            "10",
            Some("agent cap"),
            true,
            "alice",
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        assert!(created);
        assert_eq!(row.value, "10");
        assert_eq!(row.description.as_deref(), Some("agent cap"));
        assert_eq!(row.created_at.as_deref(), Some("2026-01-01T00:00:00Z"));
        assert_eq!(row.created_by.as_deref(), Some("alice"));
        assert_eq!(row.updated_by, "alice");
    }

    #[test]
    fn upsert_on_existing_key_updates_and_reports_created_false() {
        let conn = test_conn();
        upsert(
            &conn,
            "k",
            "v1",
            Some("d1"),
            true,
            "alice",
            "2026-01-01T00:00:00Z",
        )
        .unwrap();

        let (row, created) = upsert(
            &conn,
            "k",
            "v2",
            Some("d2"),
            true,
            "bob",
            "2026-01-02T00:00:00Z",
        )
        .unwrap();
        assert!(!created);
        assert_eq!(row.value, "v2");
        assert_eq!(row.description.as_deref(), Some("d2"));
        assert_eq!(row.created_at.as_deref(), Some("2026-01-01T00:00:00Z"));
        assert_eq!(row.created_by.as_deref(), Some("alice"));
        assert_eq!(row.updated_by, "bob");
    }

    #[test]
    fn upsert_value_only_update_preserves_existing_description() {
        let conn = test_conn();
        upsert(
            &conn,
            "k",
            "v1",
            Some("original description"),
            true,
            "alice",
            "2026-01-01T00:00:00Z",
        )
        .unwrap();

        let (row, _) = upsert(
            &conn,
            "k",
            "v2",
            None,
            false,
            "alice",
            "2026-01-02T00:00:00Z",
        )
        .unwrap();
        assert_eq!(row.value, "v2");
        assert_eq!(row.description.as_deref(), Some("original description"));
    }

    #[test]
    fn upsert_can_explicitly_clear_description_when_provided() {
        let conn = test_conn();
        upsert(
            &conn,
            "k",
            "v1",
            Some("will be cleared"),
            true,
            "alice",
            "2026-01-01T00:00:00Z",
        )
        .unwrap();

        let (row, _) = upsert(
            &conn,
            "k",
            "v2",
            None,
            true,
            "alice",
            "2026-01-02T00:00:00Z",
        )
        .unwrap();
        assert_eq!(row.description, None);
    }

    #[test]
    fn create_new_succeeds_for_a_fresh_key() {
        let conn = test_conn();
        let row = create_new(
            &conn,
            "k",
            "v1",
            Some("d1"),
            "alice",
            "2026-01-01T00:00:00Z",
        )
        .unwrap()
        .unwrap();
        assert_eq!(row.value, "v1");
    }

    #[test]
    fn create_new_returns_none_on_conflict_and_does_not_touch_the_existing_row() {
        let conn = test_conn();
        create_new(
            &conn,
            "k",
            "v1",
            Some("d1"),
            "alice",
            "2026-01-01T00:00:00Z",
        )
        .unwrap();

        let result =
            create_new(&conn, "k", "v2", Some("d2"), "bob", "2026-01-02T00:00:00Z").unwrap();
        assert_eq!(result, None);

        let row = get(&conn, "k").unwrap().unwrap();
        assert_eq!(
            row.value, "v1",
            "the conflicting create_new must not have mutated the existing row"
        );
    }

    #[test]
    fn list_all_returns_rows_ordered_by_context_key() {
        let conn = test_conn();
        upsert(
            &conn,
            "zeta",
            "v",
            None,
            true,
            "alice",
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        upsert(
            &conn,
            "alpha",
            "v",
            None,
            true,
            "alice",
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        upsert(
            &conn,
            "mu",
            "v",
            None,
            true,
            "alice",
            "2026-01-01T00:00:00Z",
        )
        .unwrap();

        let rows = list_all(&conn).unwrap();
        let keys: Vec<&str> = rows.iter().map(|r| r.context_key.as_str()).collect();
        assert_eq!(keys, vec!["alpha", "mu", "zeta"]);
    }

    #[test]
    fn delete_many_empty_slice_is_a_noop() {
        let conn = test_conn();
        assert_eq!(delete_many(&conn, &[]).unwrap(), Vec::new());
    }

    #[test]
    fn delete_many_silently_omits_missing_keys_and_removes_the_rest() {
        let conn = test_conn();
        upsert(
            &conn,
            "a",
            "v",
            Some("desc-a"),
            true,
            "alice",
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        upsert(&conn, "b", "v", None, true, "alice", "2026-01-01T00:00:00Z").unwrap();

        let deleted = delete_many(&conn, &["a", "b", "does-not-exist"]).unwrap();
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

        assert_eq!(get(&conn, "a").unwrap(), None);
        assert_eq!(get(&conn, "b").unwrap(), None);
    }

    #[test]
    fn project_settings_and_project_context_are_independent_tables() {
        // The whole point of this repository's existence (ADR-0016):
        // a key in one table must not collide with, shadow, or be
        // visible through the other.
        let conn = test_conn();
        upsert(
            &conn,
            "shared_key",
            "settings-value",
            None,
            true,
            "alice",
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        crate::project_context_repository::upsert(
            &conn,
            "shared_key",
            "context-value",
            None,
            true,
            "alice",
            "2026-01-01T00:00:00Z",
        )
        .unwrap();

        assert_eq!(
            get(&conn, "shared_key").unwrap().unwrap().value,
            "settings-value"
        );
        assert_eq!(
            crate::project_context_repository::get(&conn, "shared_key")
                .unwrap()
                .unwrap()
                .value,
            "context-value"
        );
    }
}
