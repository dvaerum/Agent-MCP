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
pub fn resolve_can_wake_loop(conn: &Connection, agent_id: &str) -> bool {
    if agent_id == "admin" {
        return false;
    }
    if !project_settings_repository::get_bool(conn, "config_auto_event_loop_global", true) {
        return false;
    }
    match AgentRepository::get_by_id(conn, agent_id) {
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

    #[test]
    fn admin_never_qualifies_even_with_both_flags_on() {
        let conn = test_conn();
        seed_agent(&conn, "admin", true);
        assert!(!resolve_can_wake_loop(&conn, "admin"));
    }

    #[test]
    fn a_live_worker_with_both_flags_on_qualifies() {
        let conn = test_conn();
        seed_agent(&conn, "worker-1", true);
        assert!(resolve_can_wake_loop(&conn, "worker-1"));
    }

    #[test]
    fn per_agent_flag_off_disqualifies_even_when_global_is_on() {
        let conn = test_conn();
        seed_agent(&conn, "worker-1", false);
        assert!(!resolve_can_wake_loop(&conn, "worker-1"));
    }

    #[test]
    fn global_flag_off_disqualifies_even_when_per_agent_is_on() {
        let conn = test_conn();
        seed_agent(&conn, "worker-1", true);
        project_settings_repository::upsert(
            &conn,
            "config_auto_event_loop_global",
            "false",
            None,
            false,
            "operator",
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        assert!(!resolve_can_wake_loop(&conn, "worker-1"));
    }

    #[test]
    fn global_flag_defaults_to_on_with_no_row_present() {
        let conn = test_conn();
        seed_agent(&conn, "worker-1", true);
        assert!(resolve_can_wake_loop(&conn, "worker-1"));
    }

    #[test]
    fn an_unknown_agent_id_resolves_to_false() {
        let conn = test_conn();
        assert!(!resolve_can_wake_loop(&conn, "nonexistent"));
    }
}
