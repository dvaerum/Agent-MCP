//! Per-project background maintenance loops (Phase F, prancy-napping-pie
//! -- 1 of the 4 background loops the operator approved porting,
//! 2026-09-07). Every loop here runs for the lifetime of the process;
//! `conexus-backend` has no in-process graceful-shutdown coordination
//! (unlike Python's `g.server_running` flag, needed there because one
//! Python process serves several concerns cooperatively) -- this
//! binary serves exactly one project and exits whole when the OS
//! kills it, so a loop with no explicit stop condition is the correct,
//! simplest port, not a corner cut.
//!
//! Also unlike Python's own `g.startup_complete_event` gate (deferring
//! the first cycle until `MCP_PROJECT_DIR` is set, so the DB engine
//! cache doesn't bind to the wrong path): `main()`'s boot sequence
//! already opens and initializes the DB connection synchronously,
//! fully, BEFORE `SharedState`/these loops are ever constructed --
//! there is no equivalent race to guard against, so no startup gate is
//! needed here either.

use std::sync::Arc;
use std::time::Duration;

use crate::server::SharedState;

/// Port of `agent_mcp/features/message_retention.py`. The
/// `agent_messages` table grows unbounded (rows are only ever flipped
/// to `read=1`, never deleted) -- this prunes read rows older than a
/// per-project `config_message_retention_days` knob (absent/0 =
/// unbounded, upstream behavior unchanged).
mod message_retention {
    use super::*;

    /// A misconfigured `config_message_retention_days` (e.g. an
    /// operator typo like `10**18`) would overflow `chrono::Duration`'s
    /// internal bound if fed through unclamped -- verified directly
    /// this same session (Phase F test_sec_r16 port) that
    /// `chrono::Duration::seconds` panics well before 1e15 seconds;
    /// clamping in DAYS here keeps every downstream computation the
    /// same order of magnitude Python's own `MAX_RETENTION_DAYS` clamp
    /// does, for the identical reason.
    const MAX_RETENTION_DAYS: i64 = 3650;

    pub const DEFAULT_INTERVAL: Duration = Duration::from_secs(24 * 60 * 60);

    async fn read_retention_days(sea_orm_db: &sea_orm::DatabaseConnection) -> i64 {
        let days = conexus_db::project_settings_repository::get_int(
            sea_orm_db,
            "config_message_retention_days",
            0,
        )
        .await;
        if days <= 0 {
            return 0;
        }
        days.min(MAX_RETENTION_DAYS)
    }

    /// Deletes read messages older than the configured retention
    /// window. Returns the number of rows deleted; a no-op (`Ok(0)`,
    /// never touching the table) when retention is disabled -- same
    /// contract as Python's `prune_old_messages()`.
    ///
    /// Phase G: fully sea-orm now -- `message_repository::
    /// prune_read_before` was converted in this repository's own PR
    /// 1/5, so this no longer needs the legacy `&tokio::sync::
    /// Mutex<Connection>` handle (or its lock) at all.
    pub async fn prune_old_messages(
        sea_orm_db: &sea_orm::DatabaseConnection,
        now: chrono::DateTime<chrono::Utc>,
    ) -> anyhow::Result<i64> {
        let days = read_retention_days(sea_orm_db).await;
        if days <= 0 {
            return Ok(0);
        }
        let cutoff = (now - chrono::Duration::days(days)).to_rfc3339();
        Ok(conexus_db::message_repository::prune_read_before(sea_orm_db, &cutoff).await?)
    }

    pub async fn run_periodically(shared: Arc<SharedState>, interval: Duration) {
        loop {
            let result = prune_old_messages(&shared.sea_orm_db, chrono::Utc::now()).await;
            match result {
                Ok(0) => {}
                Ok(deleted) => {
                    eprintln!(
                        "conexus-backend: message retention deleted {deleted} read message(s)"
                    );
                }
                Err(e) => {
                    eprintln!("conexus-backend: message retention cycle failed: {e}");
                }
            }
            tokio::time::sleep(interval).await;
        }
    }
}

/// Port of `agent_mcp/features/subject_backfill.py`. A root message
/// sent without an explicit subject stores `subject = NULL`; this
/// sweep titles the backlog LATER (batched, so the local model is
/// loaded once per sweep and amortised) rather than blocking the
/// synchronous send path on a model call.
mod subject_backfill {
    use super::*;
    use conexus_tools::message_suggestions;

    pub const DEFAULT_INTERVAL: Duration = Duration::from_secs(120);
    const DEFAULT_BATCH_LIMIT: i64 = 25;

    fn batch_limit(get_env: &impl Fn(&str) -> Option<String>) -> i64 {
        get_env("MCP_SUBJECT_BACKFILL_BATCH_LIMIT")
            .and_then(|v| v.trim().parse::<i64>().ok())
            .filter(|&n| n > 0)
            .unwrap_or(DEFAULT_BATCH_LIMIT)
    }

    /// Titles up to `batch_limit` NULL-subject root messages via the
    /// configured local model. Returns the number titled this sweep;
    /// `0` when the model is unconfigured or there's nothing to do.
    ///
    /// The `suggest_subject` HTTP call to the local model runs between
    /// the fetch and the write-back, with no lock held across it (a
    /// slow/unavailable model must never stall every other tool call/
    /// agent's DB access) -- previously enforced by explicitly
    /// releasing `shared.conn`'s mutex guard between the two rusqlite
    /// calls; now implicit, since `message_repository::
    /// fetch_null_subject_roots`/`set_message_subject` are sea-orm-
    /// backed (Phase G, this repository's own PR 1/5) and
    /// `sea_orm::DatabaseConnection` needs no external lock at all.
    pub async fn backfill_null_subjects(
        shared: &Arc<SharedState>,
        get_env: impl Fn(&str) -> Option<String> + Clone,
        batch_limit: i64,
    ) -> anyhow::Result<i64> {
        if !message_suggestions::subject_model_configured(&get_env) {
            return Ok(0);
        }

        let roots = conexus_db::message_repository::fetch_null_subject_roots(
            &shared.sea_orm_db,
            batch_limit,
        )
        .await?;
        if roots.is_empty() {
            return Ok(0);
        }

        let mut titled = 0i64;
        for root in &roots {
            let Some(subject) =
                message_suggestions::suggest_subject(get_env.clone(), &root.message_content).await
            else {
                // Model unavailable / empty completion -- leave NULL,
                // retry next sweep. Don't burn the rest of the batch
                // on a dead model.
                continue;
            };
            let ok = conexus_db::message_repository::set_message_subject(
                &shared.sea_orm_db,
                &root.message_id,
                &subject,
            )
            .await?;
            if ok {
                titled += 1;
                // Release any held skinny message event: the message
                // now has a real title, so wake the recipient's
                // parked wait_for_events promptly instead of on the
                // next poll.
                shared.waiter_registry.notify(&root.recipient_id);
            }
        }
        Ok(titled)
    }

    pub async fn run_periodically(
        shared: Arc<SharedState>,
        get_env: impl Fn(&str) -> Option<String> + Clone + Send + 'static,
        interval: Duration,
    ) {
        loop {
            let limit = batch_limit(&get_env);
            match backfill_null_subjects(&shared, get_env.clone(), limit).await {
                Ok(0) => {}
                Ok(titled) => {
                    eprintln!("conexus-backend: subject backfill titled {titled} message(s)");
                }
                Err(e) => {
                    eprintln!("conexus-backend: subject backfill cycle failed: {e}");
                }
            }
            tokio::time::sleep(interval).await;
        }
    }
}

/// Port of `agent_mcp/features/claude_session_monitor.py`. Watches
/// `.agent/registry.json` (the git-agentmcp hook's own multi-agent
/// coordination file) for Claude Code process activity and mirrors it
/// into the `claude_code_sessions` table.
mod claude_session_monitor {
    use super::*;
    use serde_json::{Map, Value};
    use std::collections::HashMap;
    use std::path::{Path, PathBuf};
    use std::time::SystemTime;

    pub const DEFAULT_INTERVAL: Duration = Duration::from_secs(5);

    fn registry_path(project_dir: &Path) -> PathBuf {
        project_dir.join(".agent").join("registry.json")
    }

    /// Per-loop mutable state -- Python's module-level singleton
    /// (`known_sessions`/`last_modified`) owned by the spawned task
    /// itself instead of a shared global, matching this crate's own
    /// "no process-wide mutable statics outside `SharedState`"
    /// convention.
    #[derive(Default)]
    pub struct MonitorState {
        last_modified: Option<SystemTime>,
        // pub(crate), not private: this crate's own sibling test
        // module (`claude_session_monitor_tests`) asserts against it
        // directly to prove the mtime-gate/diff logic, not just the
        // DB side effects.
        pub(crate) known_sessions: HashMap<String, Value>,
    }

    fn str_field<'a>(data: &'a Value, key: &str) -> Option<&'a str> {
        data.get(key).and_then(Value::as_str)
    }

    fn int_field(data: &Value, key: &str) -> i64 {
        data.get(key).and_then(Value::as_i64).unwrap_or(0)
    }

    /// One sweep: re-reads the registry ONLY if its mtime advanced
    /// since the last sweep, diffs against `state.known_sessions`, and
    /// syncs the DB (new -> `register_new_session` + a durable
    /// `claude_session_detected` audit row; still-present -> `
    /// update_activity`; dropped-out -> `mark_inactive`). A missing or
    /// unreadable/malformed registry file is a silent no-op, matching
    /// Python's own "normal for new projects" tolerance.
    pub async fn check_registry_changes(shared: &Arc<SharedState>, state: &mut MonitorState) {
        let path = registry_path(&shared.project_dir);
        let Ok(mtime) = std::fs::metadata(&path).and_then(|m| m.modified()) else {
            return;
        };
        if let Some(last) = state.last_modified {
            if mtime <= last {
                return;
            }
        }
        state.last_modified = Some(mtime);

        let Ok(contents) = std::fs::read_to_string(&path) else {
            return;
        };
        let registry: Value = match serde_json::from_str(&contents) {
            Ok(v) => v,
            Err(e) => {
                eprintln!(
                    "conexus-backend: invalid JSON in registry file {}: {e}",
                    path.display()
                );
                return;
            }
        };
        let sessions: Map<String, Value> = registry
            .get("sessions")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();

        let now = chrono::Utc::now().to_rfc3339();
        let stale: Vec<String> = state
            .known_sessions
            .keys()
            .filter(|id| !sessions.contains_key(*id))
            .cloned()
            .collect();

        // `claude_code_session_repository` is sea-orm-backed (Phase G);
        // `agent_action_repository`'s audit write below is not yet --
        // both connections point at the SAME underlying SQLite file
        // (opened together at boot, see `SharedState`'s own doc), so
        // interleaving them here is safe.
        for (id, data) in &sessions {
            let last_activity = str_field(data, "last_activity").unwrap_or(&now).to_string();
            let metadata = data.to_string();
            if state.known_sessions.contains_key(id) {
                if let Err(e) = conexus_db::claude_code_session_repository::update_activity(
                    &shared.sea_orm_db,
                    id,
                    &last_activity,
                    &metadata,
                )
                .await
                {
                    eprintln!("conexus-backend: error updating claude session {id}: {e}");
                }
            } else {
                let new_session = conexus_db::claude_code_session_repository::NewSession {
                    session_id: id,
                    pid: int_field(data, "pid"),
                    parent_pid: int_field(data, "parent_pid"),
                    working_directory: str_field(data, "working_directory"),
                    metadata: &metadata,
                };
                if let Err(e) = conexus_db::claude_code_session_repository::register_new_session(
                    &shared.sea_orm_db,
                    &new_session,
                    &last_activity,
                    &now,
                )
                .await
                {
                    eprintln!("conexus-backend: error registering claude session {id}: {e}");
                    continue;
                }
                let details = serde_json::json!({
                    "session_id": id,
                    "pid": int_field(data, "pid"),
                    "parent_pid": int_field(data, "parent_pid"),
                    "working_directory": str_field(data, "working_directory"),
                });
                let _ = conexus_db::agent_action_repository::log_agent_action(
                    &shared.sea_orm_db,
                    "system",
                    "claude_session_detected",
                    None,
                    Some(&details),
                    &now,
                )
                .await;
            }
        }
        for id in &stale {
            if let Err(e) = conexus_db::claude_code_session_repository::mark_inactive(
                &shared.sea_orm_db,
                id,
                &now,
            )
            .await
            {
                eprintln!("conexus-backend: error marking claude session {id} inactive: {e}");
            }
        }

        state.known_sessions = sessions.into_iter().collect();
    }

    pub async fn run_periodically(shared: Arc<SharedState>, interval: Duration) {
        let mut state = MonitorState::default();
        loop {
            check_registry_changes(&shared, &mut state).await;
            tokio::time::sleep(interval).await;
        }
    }
}

/// Spawns every approved background maintenance loop. Called once from
/// `main()` after `SharedState` is constructed; each loop gets its own
/// detached task (never joined -- see this module's own doc on why no
/// shutdown coordination is needed).
pub fn spawn_all(shared: &Arc<SharedState>) {
    tokio::spawn(message_retention::run_periodically(
        shared.clone(),
        message_retention::DEFAULT_INTERVAL,
    ));
    tokio::spawn(subject_backfill::run_periodically(
        shared.clone(),
        |key: &str| std::env::var(key).ok(),
        subject_backfill::DEFAULT_INTERVAL,
    ));
    tokio::spawn(claude_session_monitor::run_periodically(
        shared.clone(),
        claude_session_monitor::DEFAULT_INTERVAL,
    ));
}

#[cfg(test)]
mod tests {
    use super::message_retention::prune_old_messages;
    use conexus_db::message_repository::{self, NewMessage};
    use conexus_db::schema::init_schema;
    use rusqlite::Connection;

    /// A real temp-file DB opened as BOTH a rusqlite `Connection` (for
    /// seeding/reading `agent_messages` via `message_repository`'s
    /// still-sync helpers) and a sea-orm `DatabaseConnection` (for
    /// `prune_old_messages`, fully sea-orm now -- Phase G,
    /// `message_repository`'s own PR 1/5). An in-memory `:memory:` DB
    /// can't be shared across two separate connection handles the way
    /// a real file can; mirrors `message_repository::tests::
    /// test_conn_with_sea_orm`.
    async fn test_db() -> (tempfile::TempDir, Connection, sea_orm::DatabaseConnection) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.db");
        let conn = Connection::open(&path).unwrap();
        init_schema(&conn).unwrap();
        let sea_orm_db = sea_orm::Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        (dir, conn, sea_orm_db)
    }

    async fn set_retention_days(db: &sea_orm::DatabaseConnection, days: i64) {
        conexus_db::project_settings_repository::upsert(
            db,
            "config_message_retention_days",
            &days.to_string(),
            None,
            false,
            "test",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();
    }

    fn seed_message(conn: &Connection, id: &str, sent_at: &str, read: bool) {
        // "admin" is the one recipient_exists() accepts unconditionally
        // (message_repository.rs), so tests don't need to seed a real
        // agents row just to send a message.
        message_repository::send(
            conn,
            NewMessage {
                message_id: id,
                sender_id: "alice",
                recipient_id: "admin",
                message_content: "hi",
                message_type: "direct",
                priority: "normal",
                timestamp: sent_at,
                delivered: true,
                read,
                subject: None,
                parent_message_id: None,
            },
        )
        .unwrap();
    }

    #[tokio::test]
    async fn disabled_by_default_prunes_nothing() {
        let (_dir, conn, sea_orm_db) = test_db().await;
        seed_message(&conn, "m1", "2020-01-01T00:00:00Z", true);
        let deleted = prune_old_messages(&sea_orm_db, chrono::Utc::now())
            .await
            .unwrap();
        assert_eq!(deleted, 0);
        assert!(message_repository::get_by_id(&conn, "m1")
            .unwrap()
            .is_some());
    }

    #[tokio::test]
    async fn prunes_only_read_messages_past_the_configured_window() {
        let (_dir, conn, sea_orm_db) = test_db().await;
        let now: chrono::DateTime<chrono::Utc> = "2026-06-01T00:00:00Z".parse().unwrap();
        seed_message(&conn, "old-read", "2026-01-01T00:00:00Z", true);
        seed_message(&conn, "old-unread", "2026-01-01T00:00:00Z", false);
        seed_message(&conn, "recent-read", "2026-05-30T00:00:00Z", true);
        set_retention_days(&sea_orm_db, 30).await;

        let deleted = prune_old_messages(&sea_orm_db, now).await.unwrap();

        assert_eq!(deleted, 1);
        assert!(message_repository::get_by_id(&conn, "old-read")
            .unwrap()
            .is_none());
        assert!(message_repository::get_by_id(&conn, "old-unread")
            .unwrap()
            .is_some());
        assert!(message_repository::get_by_id(&conn, "recent-read")
            .unwrap()
            .is_some());
    }

    #[tokio::test]
    async fn a_huge_retention_value_is_clamped_not_left_to_overflow() {
        // Verified this session (Phase F test_sec_r16 port) that
        // chrono::Duration panics well before 1e15 seconds -- this
        // proves the clamp actually engages for an operator typo
        // rather than assuming MAX_RETENTION_DAYS is merely decorative.
        // The seeded message must be older than the CLAMPED window
        // (MAX_RETENTION_DAYS = 3650 days, ~10y) or clamping correctly
        // means it's still within the retained window and never
        // pruned -- an explicit fixed `now` keeps this independent of
        // the wall clock, unlike an earlier draft that used
        // `chrono::Utc::now()` and silently stopped pruning once real
        // time passed 10 years past the seeded date.
        let (_dir, conn, sea_orm_db) = test_db().await;
        let now: chrono::DateTime<chrono::Utc> = "2026-01-01T00:00:00Z".parse().unwrap();
        seed_message(&conn, "m1", "2000-01-01T00:00:00Z", true);
        set_retention_days(&sea_orm_db, 999_999_999_999).await;
        // Must not panic.
        let deleted = prune_old_messages(&sea_orm_db, now).await.unwrap();
        assert_eq!(deleted, 1);
    }
}

#[cfg(test)]
mod subject_backfill_tests {
    use super::subject_backfill::backfill_null_subjects;
    use crate::server::SharedState;
    use conexus_db::message_repository::{self, NewMessage};
    use conexus_db::schema::init_schema;
    use conexus_wakeloop::file_map::FileMap;
    use conexus_wakeloop::waiter_registry::WaiterRegistry;
    use std::collections::HashMap;
    use std::sync::Arc;

    /// `shared.conn` (rusqlite) and `shared.sea_orm_db` must point at
    /// the SAME real temp-file DB, not two separate `:memory:`
    /// databases -- `backfill_null_subjects` reads/writes
    /// `agent_messages` exclusively through `shared.sea_orm_db` now
    /// (Phase G, `message_repository`'s own PR 1/5), while these tests
    /// still seed fixture rows through `shared.conn`'s still-sync
    /// `message_repository::send`.
    async fn test_shared() -> (tempfile::TempDir, Arc<SharedState>) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.db");
        let conn = rusqlite::Connection::open(&path).unwrap();
        init_schema(&conn).unwrap();
        let sea_orm_db = sea_orm::Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        let shared = Arc::new(SharedState {
            conn: tokio::sync::Mutex::new(conn),
            forwarding_hmac_key: None,
            waiter_registry: WaiterRegistry::new(),
            file_map: FileMap::new(),
            project_dir: std::env::temp_dir(),
            operator_events: crate::operator_events::OperatorEventsHub::new(),
            delivery_transport: crate::delivery_transport::DeliveryTransportHub::new(),
            sea_orm_db,
        });
        (dir, shared)
    }

    fn seed_root(conn: &rusqlite::Connection, id: &str, recipient_id: &str, content: &str) {
        message_repository::send(
            conn,
            NewMessage {
                message_id: id,
                sender_id: "alice",
                recipient_id,
                message_content: content,
                message_type: "direct",
                priority: "normal",
                timestamp: "2026-01-01T00:00:00Z",
                delivered: true,
                read: false,
                subject: None,
                parent_message_id: None,
            },
        )
        .unwrap();
    }

    fn env(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> + Clone {
        let map: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        move |key| map.get(key).cloned()
    }

    #[tokio::test]
    async fn model_unconfigured_is_a_no_op() {
        let (_dir, shared) = test_shared().await;
        {
            let conn = shared.conn.lock().await;
            seed_root(&conn, "m1", "admin", "hello there");
        }
        let titled = backfill_null_subjects(&shared, env(&[]), 25).await.unwrap();
        assert_eq!(titled, 0);
    }

    #[tokio::test]
    async fn nothing_to_backfill_is_a_no_op() {
        let (_dir, shared) = test_shared().await;
        let titled = backfill_null_subjects(
            &shared,
            env(&[("AGENT_MCP_SUBJECT_MODEL", "qwen2.5:3b-instruct")]),
            25,
        )
        .await
        .unwrap();
        assert_eq!(titled, 0);
    }

    #[tokio::test]
    async fn a_dead_model_leaves_every_root_null_and_titles_nothing() {
        // The model is "configured" but points at an unreachable
        // endpoint -- every suggest_subject call must degrade to None
        // rather than propagate an error, matching Python's own
        // per-row `continue` on a dead model.
        let (_dir, shared) = test_shared().await;
        {
            let conn = shared.conn.lock().await;
            seed_root(&conn, "m1", "admin", "hello there");
        }
        let titled = backfill_null_subjects(
            &shared,
            env(&[
                ("AGENT_MCP_SUBJECT_MODEL", "qwen2.5:3b-instruct"),
                ("AGENT_MCP_LLM_BASE_URL", "http://127.0.0.1:1/v1"),
                ("AGENT_MCP_MODEL_CONTEXT_WINDOW", "4096"),
            ]),
            25,
        )
        .await
        .unwrap();
        assert_eq!(titled, 0);
        let conn = shared.conn.lock().await;
        assert!(message_repository::get_by_id(&conn, "m1")
            .unwrap()
            .unwrap()
            .subject
            .is_none());
    }

    #[tokio::test]
    async fn titles_a_real_null_subject_root_and_wakes_the_recipient() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let handle = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut buf = [0u8; 8192];
            let _ = socket.read(&mut buf).await;
            let body = r#"{"choices":[{"message":{"content":"Deploy failed on staging"}}]}"#;
            let resp = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            let _ = socket.write_all(resp.as_bytes()).await;
            let _ = socket.shutdown().await;
        });

        let (_dir, shared) = test_shared().await;
        {
            let conn = shared.conn.lock().await;
            // Fixture data only (this test doesn't assert on `create()`
            // itself) -- a raw insert through `shared.conn` rather than
            // the sea-orm-backed `AgentRepository::create`; both land
            // in the same real temp-file DB `shared.sea_orm_db` also
            // points at.
            conn.execute(
                "INSERT INTO agents (token, agent_id, created_at, status, current_task, working_directory, color, agent_role) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
                (
                    "tok-bob",
                    "bob",
                    "2026-01-01T00:00:00Z",
                    "created",
                    Option::<String>::None,
                    "/tmp",
                    Option::<String>::None,
                    "worker",
                ),
            )
            .unwrap();
            seed_root(&conn, "m1", "bob", "the deploy to staging just failed");
        }
        // A parked waiter for "bob" -- proves the post-title wake
        // actually reaches the recipient's registry entry, not just
        // that notify() was called without an observable effect.
        let (_tx, mut rx) = shared.waiter_registry.register("bob");

        let base_url = format!("http://{addr}/v1");
        let titled = backfill_null_subjects(
            &shared,
            env(&[
                ("AGENT_MCP_SUBJECT_MODEL", "qwen2.5:3b-instruct"),
                ("AGENT_MCP_LLM_BASE_URL", &base_url),
                ("AGENT_MCP_MODEL_CONTEXT_WINDOW", "4096"),
            ]),
            25,
        )
        .await
        .unwrap();

        assert_eq!(titled, 1);
        let conn = shared.conn.lock().await;
        let row = message_repository::get_by_id(&conn, "m1").unwrap().unwrap();
        assert_eq!(row.subject.as_deref(), Some("Deploy failed on staging"));
        assert!(rx.try_recv().is_ok(), "recipient's waiter was not woken");
        handle.await.unwrap();
    }
}

#[cfg(test)]
mod claude_session_monitor_tests {
    use super::claude_session_monitor::{check_registry_changes, MonitorState};
    use crate::server::SharedState;
    use conexus_db::schema::init_schema;
    use conexus_wakeloop::file_map::FileMap;
    use conexus_wakeloop::waiter_registry::WaiterRegistry;
    use std::sync::Arc;

    async fn test_shared(project_dir: std::path::PathBuf) -> (tempfile::TempDir, Arc<SharedState>) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.db");
        let conn = rusqlite::Connection::open(&path).unwrap();
        init_schema(&conn).unwrap();
        let sea_orm_db = sea_orm::Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        let shared = Arc::new(SharedState {
            conn: tokio::sync::Mutex::new(conn),
            forwarding_hmac_key: None,
            waiter_registry: WaiterRegistry::new(),
            file_map: FileMap::new(),
            project_dir,
            operator_events: crate::operator_events::OperatorEventsHub::new(),
            delivery_transport: crate::delivery_transport::DeliveryTransportHub::new(),
            sea_orm_db,
        });
        (dir, shared)
    }

    fn scratch_dir(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "conexus-claude-session-monitor-test-{name}-{}",
            std::process::id()
        ));
        std::fs::create_dir_all(dir.join(".agent")).unwrap();
        dir
    }

    fn write_registry(project_dir: &std::path::Path, json: &str) {
        std::fs::write(project_dir.join(".agent").join("registry.json"), json).unwrap();
    }

    #[tokio::test]
    async fn no_registry_file_is_a_silent_no_op() {
        let dir = scratch_dir("missing");
        let (_db_dir, shared) = test_shared(dir.clone()).await;
        let mut state = MonitorState::default();
        check_registry_changes(&shared, &mut state).await;
        assert!(
            conexus_db::claude_code_session_repository::list_active(&shared.sea_orm_db)
                .await
                .unwrap()
                .is_empty()
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    #[tokio::test]
    async fn malformed_json_is_a_silent_no_op() {
        let dir = scratch_dir("malformed");
        write_registry(&dir, "not json");
        let (_db_dir, shared) = test_shared(dir.clone()).await;
        let mut state = MonitorState::default();
        check_registry_changes(&shared, &mut state).await;
        assert!(
            conexus_db::claude_code_session_repository::list_active(&shared.sea_orm_db)
                .await
                .unwrap()
                .is_empty()
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    #[tokio::test]
    async fn a_new_session_is_registered_and_audited() {
        let dir = scratch_dir("new");
        write_registry(
            &dir,
            r#"{"sessions": {"s1": {"pid": 111, "parent_pid": 222, "working_directory": "/repo"}}}"#,
        );
        let (_db_dir, shared) = test_shared(dir.clone()).await;
        let mut state = MonitorState::default();
        check_registry_changes(&shared, &mut state).await;

        assert!(state.known_sessions.contains_key("s1"));
        let row = conexus_db::claude_code_session_repository::get_by_id(&shared.sea_orm_db, "s1")
            .await
            .unwrap()
            .unwrap();
        assert_eq!(row.pid, 111);
        assert_eq!(row.parent_pid, 222);
        assert_eq!(row.working_directory.as_deref(), Some("/repo"));
        assert_eq!(row.status.as_deref(), Some("detected"));

        let conn = shared.conn.lock().await;
        let actions: Vec<String> = {
            let mut stmt = conn
                .prepare("SELECT action_type FROM agent_actions WHERE agent_id = 'system'")
                .unwrap();
            stmt.query_map([], |r| r.get::<_, String>(0))
                .unwrap()
                .collect::<Result<_, _>>()
                .unwrap()
        };
        assert_eq!(actions, vec!["claude_session_detected"]);
        drop(conn);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[tokio::test]
    async fn an_unchanged_mtime_skips_the_second_read_entirely() {
        let dir = scratch_dir("unchanged");
        write_registry(&dir, r#"{"sessions": {"s1": {"pid": 1, "parent_pid": 2}}}"#);
        let (_db_dir, shared) = test_shared(dir.clone()).await;
        let mut state = MonitorState::default();
        check_registry_changes(&shared, &mut state).await;
        assert_eq!(state.known_sessions.len(), 1);

        // Second sweep with the SAME mtime -- must be a no-op even
        // though the file (if re-read) would still parse fine; this
        // proves the mtime gate itself is doing the skipping, not
        // some other short-circuit.
        check_registry_changes(&shared, &mut state).await;
        assert_eq!(state.known_sessions.len(), 1);
    }

    #[tokio::test]
    async fn a_session_still_present_is_updated_not_re_registered() {
        let dir = scratch_dir("update");
        write_registry(
            &dir,
            r#"{"sessions": {"s1": {"pid": 1, "parent_pid": 2, "last_activity": "2026-01-01T00:00:00Z"}}}"#,
        );
        let (_db_dir, shared) = test_shared(dir.clone()).await;
        let mut state = MonitorState::default();
        check_registry_changes(&shared, &mut state).await;

        // Force a new mtime by re-writing with updated content.
        std::thread::sleep(std::time::Duration::from_millis(10));
        write_registry(
            &dir,
            r#"{"sessions": {"s1": {"pid": 1, "parent_pid": 2, "last_activity": "2026-01-02T00:00:00Z"}}}"#,
        );
        check_registry_changes(&shared, &mut state).await;

        let row = conexus_db::claude_code_session_repository::get_by_id(&shared.sea_orm_db, "s1")
            .await
            .unwrap()
            .unwrap();
        assert_eq!(row.last_activity, "2026-01-02T00:00:00Z");
        assert_eq!(row.status.as_deref(), Some("active"));
        // Only ONE detection audit row -- the second sweep updated,
        // it did not re-register/re-audit.
        let conn = shared.conn.lock().await;
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM agent_actions WHERE action_type = 'claude_session_detected'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
        drop(conn);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[tokio::test]
    async fn a_session_dropped_from_the_registry_is_marked_inactive() {
        let dir = scratch_dir("drop");
        write_registry(&dir, r#"{"sessions": {"s1": {"pid": 1, "parent_pid": 2}}}"#);
        let (_db_dir, shared) = test_shared(dir.clone()).await;
        let mut state = MonitorState::default();
        check_registry_changes(&shared, &mut state).await;

        std::thread::sleep(std::time::Duration::from_millis(10));
        write_registry(&dir, r#"{"sessions": {}}"#);
        check_registry_changes(&shared, &mut state).await;

        assert!(!state.known_sessions.contains_key("s1"));
        let row = conexus_db::claude_code_session_repository::get_by_id(&shared.sea_orm_db, "s1")
            .await
            .unwrap()
            .unwrap();
        assert_eq!(row.status.as_deref(), Some("inactive"));
        std::fs::remove_dir_all(&dir).ok();
    }
}
