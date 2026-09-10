//! Port of `agent_mcp/core/principal_builder.py::_resolve_can_wake_loop`
//! (Phase E1 PR A) — the DB-backed check gating
//! `Principal::can_wake_loop`, which in turn gates the wake-loop
//! bootstrap `initialize` instructions (`conexus-backend::instructions`)
//! and, later, any other feature that reads this bit.
//!
//! Deliberately NOT the same function as
//! `conexus_wakeloop::event_feed::check_auto_event_loop_flags` — that
//! is a different Python function (`_check_auto_event_loop_flags`)
//! with different, intentionally fail-OPEN semantics for the wake
//! loop's own per-iteration recheck ("a lookup failure must never
//! itself stop an otherwise-healthy agent's loop"). This function
//! ports `_resolve_can_wake_loop`, which fails CLOSED on any lookup
//! problem (a defensive default for a one-shot bootstrap-instructions
//! decision, not a recurring loop condition) and additionally excludes
//! the `"admin"` pseudo-agent id outright. The two must not be
//! conflated or merged.

use conexus_db::agent_repository::AgentRepository;
use conexus_db::project_settings_repository;
use rusqlite::Connection;

/// True iff `agent_id`'s bearer should see the wake-loop bootstrap
/// instructions on `initialize`: the global
/// `config_auto_event_loop_global` toggle is on (default `true`) AND
/// the agent's own `auto_event_loop` column is on. The `"admin"`
/// pseudo-agent id never qualifies — admins coordinate, they don't run
/// the worker wake loop. Any DB error, or no such agent, resolves to
/// `false` (fail-closed — matches Python's own `except Exception:
/// return False`).
///
/// Phase G: `project_settings_repository` is sea-orm-backed now, so
/// the global-flag read goes through `sea_orm_db` and happens BEFORE
/// `conn` (the legacy connection this function's own `AgentRepository::
/// get_by_id` call still needs -- `get_by_id` is one of several
/// `AgentRepository` methods staying PERMANENTLY rusqlite-only, per
/// `conexus_db::agent_repository`'s own module doc; `AgentRepository`
/// as a WHOLE is no longer un-converted since Phase G's own PRs 1-3)
/// is ever locked. This ordering isn't cosmetic: `conn: &tokio::sync::Mutex<Connection>`
/// locked AFTER the only `.await` in this function means the resulting
/// `MutexGuard` never has to coexist with a suspension point, so
/// there's no `!Send`-future hazard to reason about at all (see
/// `conexus_wakeloop::event_feed::assemble_event_feed`'s own doc
/// comment for the general rule this sidesteps).
pub async fn resolve_can_wake_loop(
    conn: &tokio::sync::Mutex<Connection>,
    sea_orm_db: &sea_orm::DatabaseConnection,
    agent_id: &str,
) -> bool {
    if agent_id == "admin" {
        return false;
    }
    if !project_settings_repository::get_bool(sea_orm_db, "config_auto_event_loop_global", true)
        .await
    {
        return false;
    }
    let guard = conn.lock().await;
    match AgentRepository::get_by_id(&guard, agent_id) {
        Ok(Some(row)) => row.auto_event_loop,
        Ok(None) | Err(_) => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_db::schema::init_schema;

    fn test_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        conn
    }

    fn seed_agent(conn: &Connection, agent_id: &str, auto_event_loop: bool) {
        conn.execute(
            "INSERT INTO agents (token, agent_id, created_at, status, working_directory, \
             agent_role, auto_event_loop) VALUES (?1, ?2, '2026-01-01T00:00:00Z', 'active', \
             '/tmp', 'worker', ?3)",
            (format!("tok-{agent_id}"), agent_id, auto_event_loop),
        )
        .unwrap();
    }

    /// A real temp-file-backed sea-orm connection for `resolve_can_
    /// wake_loop`'s `config_auto_event_loop_global` reads. A SEPARATE
    /// temp file from `test_conn`'s is fine -- `agents` and
    /// `project_settings` are disjoint tables, each read through
    /// exactly one connection type in this function, so nothing here
    /// needs the two sides to observe each other's data (matches
    /// `conexus_wakeloop::event_feed`'s own `test_sea_orm_db` doc
    /// comment, which established this exact precedent first).
    async fn test_sea_orm_db() -> (tempfile::TempDir, sea_orm::DatabaseConnection) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.db");
        {
            let c = Connection::open(&path).unwrap();
            init_schema(&c).unwrap();
        }
        let db = sea_orm::Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        (dir, db)
    }

    #[tokio::test]
    async fn admin_never_qualifies_even_with_both_flags_on() {
        let conn = test_conn();
        seed_agent(&conn, "admin", true);
        let conn = tokio::sync::Mutex::new(conn);
        let (_dir, sea_orm_db) = test_sea_orm_db().await;
        assert!(!resolve_can_wake_loop(&conn, &sea_orm_db, "admin").await);
    }

    #[tokio::test]
    async fn a_live_worker_with_both_flags_on_qualifies() {
        let conn = test_conn();
        seed_agent(&conn, "worker-1", true);
        let conn = tokio::sync::Mutex::new(conn);
        let (_dir, sea_orm_db) = test_sea_orm_db().await;
        assert!(resolve_can_wake_loop(&conn, &sea_orm_db, "worker-1").await);
    }

    #[tokio::test]
    async fn per_agent_flag_off_disqualifies_even_when_global_is_on() {
        let conn = test_conn();
        seed_agent(&conn, "worker-1", false);
        let conn = tokio::sync::Mutex::new(conn);
        let (_dir, sea_orm_db) = test_sea_orm_db().await;
        assert!(!resolve_can_wake_loop(&conn, &sea_orm_db, "worker-1").await);
    }

    #[tokio::test]
    async fn global_flag_off_disqualifies_even_when_per_agent_is_on() {
        let conn = test_conn();
        seed_agent(&conn, "worker-1", true);
        let conn = tokio::sync::Mutex::new(conn);
        let (_dir, sea_orm_db) = test_sea_orm_db().await;
        project_settings_repository::upsert(
            &sea_orm_db,
            "config_auto_event_loop_global",
            "false",
            None,
            false,
            "operator",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();
        assert!(!resolve_can_wake_loop(&conn, &sea_orm_db, "worker-1").await);
    }

    #[tokio::test]
    async fn global_flag_defaults_to_on_with_no_row_present() {
        let conn = test_conn();
        seed_agent(&conn, "worker-1", true);
        let conn = tokio::sync::Mutex::new(conn);
        let (_dir, sea_orm_db) = test_sea_orm_db().await;
        assert!(resolve_can_wake_loop(&conn, &sea_orm_db, "worker-1").await);
    }

    #[tokio::test]
    async fn an_unknown_agent_id_resolves_to_false() {
        let conn = test_conn();
        let conn = tokio::sync::Mutex::new(conn);
        let (_dir, sea_orm_db) = test_sea_orm_db().await;
        assert!(!resolve_can_wake_loop(&conn, &sea_orm_db, "nonexistent").await);
    }
}
