//! Per-project background maintenance loops (Phase F, prancy-napping-pie
//! -- 3 of the 4 originally-approved background loops (2026-09-07),
//! plus `rag_indexing` (the separately-flagged "6th finding" found
//! while porting `test_sec_r31_rag_watermark.py`, resumed 2026-09-11
//! once its design was confirmed already-decided). Every loop here
//! runs for the lifetime of the process;
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

/// Port of `agent_mcp/features/rag/indexing.py::run_rag_indexing_
/// periodically` (recovered from git history -- deleted in PR #1010's
/// bulk Python deletion, before this Rust replacement existed; see
/// `conexus_tools::rag_chunking`'s own module doc for the recovery
/// procedure this crate now shares).
///
/// **Simple-mode only, and that's a real, evidence-based scope cut,
/// not a corner cut**: Python's periodic cycle also scans code files
/// and tasks, but ONLY when `AGENT_MCP_EMBEDDING_DIMENSION`/`--advanced`
/// puts the deployment in "advanced" mode -- confirmed directly against
/// the real deploy repo's own config (`home-manager-config/common/
/// user/agent-mcp/default.nix`) that neither is ever set, so that
/// whole branch (and the ~584-LOC code-aware chunker it would need)
/// has zero real production call site today. Markdown files + project
/// context are scanned unconditionally in both modes and are the only
/// two sources this port covers.
///
/// **R10-F2 (Python's own WAL-write-lock-starvation fix) is
/// architecturally eliminated here, not re-derived**: Python's
/// `_delete_stale_chunks_and_commit` needed an explicit, unconditional
/// `conn.commit()` because its shared `sqlite3.Connection` opens an
/// implicit multi-statement transaction the instant any statement
/// executes, and that transaction (and the WAL write lock it holds)
/// would otherwise stay open across the embedding-await phase that
/// follows. This port never opens an explicit `rusqlite::Transaction`
/// spanning delete+embed+insert -- every repository call here runs in
/// rusqlite's own per-statement autocommit mode, so there is no
/// multi-statement transaction for an await to hold open in the first
/// place. The OTHER real hazard -- holding `shared.conn`'s own
/// `tokio::sync::Mutex` guard across the (slow, network-bound)
/// embedding call, which would stall every other tool call on this
/// process for the cycle's duration -- is avoided the ordinary way
/// this crate already uses elsewhere: the guard is taken fresh for
/// each synchronous DB phase (delete, then later insert) and dropped
/// before/around the `embed().await` in between.
mod rag_indexing {
    use super::*;
    use conexus_tools::embedding_client;
    use conexus_tools::rag_chunking::simple_chunker;
    use sha2::{Digest, Sha256};
    use std::path::{Path, PathBuf};

    /// Python's own default -- see this module's own doc for why the
    /// real cycle-to-cycle sleep is a fraction of this, not this value
    /// directly.
    pub const DEFAULT_INTERVAL: Duration = Duration::from_secs(300);

    /// Matches `simple_chunker(content)`'s own bare-call defaults --
    /// the ONLY chunker call real (simple-mode) production ever makes.
    const CHUNK_SIZE: usize = 500;
    const CHUNK_OVERLAP: usize = 50;

    const IGNORE_DIRS: &[&str] = &[
        "node_modules",
        "__pycache__",
        "venv",
        "env",
        ".venv",
        ".env",
        "dist",
        "build",
        "site-packages",
        ".git",
        ".idea",
        ".vscode",
        "bin",
        "obj",
        "target",
        ".pytest_cache",
        ".ipynb_checkpoints",
        ".agent",
    ];

    /// One `sources_to_check` entry -- a source row that was scanned
    /// this cycle, whether or not its hash turned out to have changed.
    struct ScannedSource {
        source_type: &'static str,
        source_ref: String,
        content: String,
        /// UTC epoch seconds this source was last modified. Both real
        /// source types (a file's real mtime, `project_context.
        /// updated_at` parsed) collapse to this one representation --
        /// a deliberate simplification over Python's own mixed
        /// float-epoch-for-files/ISO-string-for-context typing, valid
        /// because this port is the only reader AND writer of the
        /// `last_indexed_<type>` watermark it produces (see this
        /// module's own doc on the UTC-RFC3339 convention every other
        /// Rust-authored timestamp in this codebase already uses).
        mod_time: f64,
        hash: String,
    }

    fn sha256_hex(content: &str) -> String {
        let mut hasher = Sha256::new();
        hasher.update(content.as_bytes());
        format!("{:x}", hasher.finalize())
    }

    fn is_ignored_component(name: &str) -> bool {
        IGNORE_DIRS.contains(&name) || (name.starts_with('.') && name != "." && name != "..")
    }

    /// Recursively finds every `*.md` file under `project_dir`, skipping
    /// any path component that matches [`IGNORE_DIRS`] or looks hidden
    /// -- port of the real `glob.glob("**/*.md", recursive=True)` +
    /// per-component ignore filter Python's own scan loop applies.
    /// Read errors on an individual subdirectory are skipped, not
    /// fatal to the whole scan -- matches Python's own per-file
    /// `try/except` tolerance one level up.
    fn find_markdown_files(project_dir: &Path) -> Vec<PathBuf> {
        let mut found = Vec::new();
        let mut stack = vec![project_dir.to_path_buf()];
        while let Some(dir) = stack.pop() {
            let Ok(entries) = std::fs::read_dir(&dir) else {
                continue;
            };
            for entry in entries.flatten() {
                let path = entry.path();
                let Some(name) = path.file_name().and_then(|n| n.to_str()) else {
                    continue;
                };
                if is_ignored_component(name) {
                    continue;
                }
                if path.is_dir() {
                    stack.push(path);
                } else if path.extension().and_then(|e| e.to_str()) == Some("md") {
                    found.push(path);
                }
            }
        }
        found
    }

    /// Port of the context-scan branch's `content_for_embedding`
    /// formatting -- byte-for-byte the same three-line shape Python
    /// builds, since it's both hashed AND embedded and must stay
    /// identical between the two source-type branches' shared
    /// treatment downstream.
    fn format_context_for_embedding(key: &str, description: &str, value_json: &str) -> String {
        format!("Context Key: {key}\nDescription: {description}\nValue: {value_json}")
    }

    /// Everything a completed cycle needs to report -- purely for
    /// `eprintln!`/test-assertion purposes, not consumed by any other
    /// code path.
    #[derive(Debug, Default, PartialEq, Eq)]
    pub struct CycleReport {
        pub skipped_no_vss: bool,
        pub sources_scanned: usize,
        pub sources_updated: usize,
        pub chunks_indexed: i64,
        pub sources_failed: usize,
    }

    /// One indexing cycle. See this module's own doc for the R10-F2/
    /// lock-scope reasoning and the simple-mode-only scope cut.
    pub async fn run_indexing_cycle(
        conn: &tokio::sync::Mutex<rusqlite::Connection>,
        sea_orm_db: &sea_orm::DatabaseConnection,
        project_dir: &Path,
        get_env: &impl Fn(&str) -> Option<String>,
        now: chrono::DateTime<chrono::Utc>,
    ) -> anyhow::Result<CycleReport> {
        if !conexus_db::rag_repository::embeddings_table_exists(sea_orm_db).await? {
            return Ok(CycleReport {
                skipped_no_vss: true,
                ..Default::default()
            });
        }

        let meta = conexus_db::rag_repository::get_all_meta(sea_orm_db).await?;
        let epoch_iso = "1970-01-01T00:00:00Z";
        let last_md_watermark = meta
            .get("last_indexed_markdown")
            .cloned()
            .unwrap_or_else(|| epoch_iso.to_string());
        let last_ctx_watermark = meta
            .get("last_indexed_context")
            .cloned()
            .unwrap_or_else(|| epoch_iso.to_string());
        let last_md_epoch = chrono::DateTime::parse_from_rfc3339(&last_md_watermark)
            .map(|dt| dt.timestamp() as f64)
            .unwrap_or(0.0);
        let last_ctx_epoch = chrono::DateTime::parse_from_rfc3339(&last_ctx_watermark)
            .map(|dt| dt.timestamp() as f64)
            .unwrap_or(0.0);

        let mut sources: Vec<ScannedSource> = Vec::new();
        let mut max_md_epoch = last_md_epoch;
        let mut max_ctx_epoch = last_ctx_epoch;

        // 1. Markdown files -- gated the same way Python's own
        // DISABLE_AUTO_INDEXING flag does, resolved fresh each cycle
        // so a runtime env change is honoured immediately.
        let auto_indexing_disabled = get_env("AGENT_MCP_DISABLE_AUTO_INDEXING")
            .map(|v| matches!(v.trim().to_ascii_lowercase().as_str(), "1" | "true" | "yes"))
            .unwrap_or(false);
        if !auto_indexing_disabled {
            for path in find_markdown_files(project_dir) {
                let Ok(metadata) = std::fs::metadata(&path) else {
                    continue;
                };
                let Ok(modified) = metadata.modified() else {
                    continue;
                };
                let mod_epoch = chrono::DateTime::<chrono::Utc>::from(modified).timestamp() as f64;
                let Ok(content) = std::fs::read_to_string(&path) else {
                    continue;
                };
                let Ok(rel_path) = path.strip_prefix(project_dir) else {
                    continue;
                };
                let source_ref = rel_path.to_string_lossy().replace('\\', "/");
                let hash = sha256_hex(&content);
                if mod_epoch > max_md_epoch {
                    max_md_epoch = mod_epoch;
                }
                sources.push(ScannedSource {
                    source_type: "markdown",
                    source_ref,
                    content,
                    mod_time: mod_epoch,
                    hash,
                });
            }
        }

        // 2. Project context -- always scanned, both modes.
        let ctx_rows = conexus_db::project_context_repository::list_all(sea_orm_db).await?;
        for row in &ctx_rows {
            if row.updated_at.as_str() <= last_ctx_watermark.as_str() {
                continue;
            }
            let row_epoch = chrono::DateTime::parse_from_rfc3339(&row.updated_at)
                .map(|dt| dt.timestamp() as f64)
                .unwrap_or(last_ctx_epoch);
            if row_epoch > max_ctx_epoch {
                max_ctx_epoch = row_epoch;
            }
            let content = format_context_for_embedding(
                &row.context_key,
                row.description.as_deref().unwrap_or(""),
                &row.value,
            );
            let hash = sha256_hex(&content);
            sources.push(ScannedSource {
                source_type: "context",
                source_ref: row.context_key.clone(),
                content,
                mod_time: row_epoch,
                hash,
            });
        }

        let sources_scanned = sources.len();

        // 3. Filter by hash comparison against the stored watermark.
        let to_process: Vec<ScannedSource> = sources
            .into_iter()
            .filter(|s| {
                let key = format!("hash_{}_{}", s.source_type, s.source_ref);
                meta.get(&key) != Some(&s.hash)
            })
            .collect();

        if to_process.is_empty() {
            advance_watermarks(
                conn,
                &meta,
                last_md_watermark_epoch_pair(last_md_epoch, max_md_epoch, auto_indexing_disabled),
                (last_ctx_epoch, max_ctx_epoch),
                &[],
                &[],
            )
            .await?;
            return Ok(CycleReport {
                sources_scanned,
                ..Default::default()
            });
        }

        // 4. Delete stale chunks for every source about to be
        // reprocessed -- each `delete_chunks_for` call autocommits on
        // its own (see this module's own doc); the guard is dropped
        // the instant this block ends, well before the embedding call.
        {
            let guard = conn.lock().await;
            for s in &to_process {
                conexus_db::rag_repository::delete_chunks_for(
                    &guard,
                    s.source_type,
                    &s.source_ref,
                )?;
            }
        }

        // 5. Chunk every source (simple_chunker only -- see module
        // doc), then embed everything in one pass. Sequential batches,
        // not Python's 25-way-concurrent task group: this deployment's
        // real Ollama endpoint runs `-np 1` (hard single-request
        // concurrency), so concurrent batches would only queue FIFO on
        // the server side anyway -- sequential batching gets the exact
        // same real throughput with far simpler code, not a corner cut.
        struct ChunkEntry {
            source_idx: usize,
            text: String,
        }
        let mut chunk_entries: Vec<ChunkEntry> = Vec::new();
        for (idx, s) in to_process.iter().enumerate() {
            for chunk in simple_chunker(&s.content, CHUNK_SIZE, CHUNK_OVERLAP) {
                if chunk.trim().is_empty() {
                    continue;
                }
                chunk_entries.push(ChunkEntry {
                    source_idx: idx,
                    text: chunk,
                });
            }
        }

        let client = embedding_client::resolve(get_env);
        const EMBED_BATCH_SIZE: usize = 50;
        let mut embeddings: Vec<Option<Vec<f32>>> = vec![None; chunk_entries.len()];
        for batch_start in (0..chunk_entries.len()).step_by(EMBED_BATCH_SIZE) {
            let batch_end = (batch_start + EMBED_BATCH_SIZE).min(chunk_entries.len());
            let batch_texts: Vec<String> = chunk_entries[batch_start..batch_end]
                .iter()
                .map(|e| e.text.clone())
                .collect();
            match client.embed(&batch_texts).await {
                Ok(vectors) => {
                    for (i, v) in vectors.into_iter().enumerate() {
                        embeddings[batch_start + i] = Some(v);
                    }
                }
                Err(e) => {
                    eprintln!("conexus-backend: RAG embedding batch failed: {e}");
                    // Left as None -- this batch's sources are marked
                    // failed below (BL-R31-1), never silently dropped.
                }
            }
        }

        // 6. Per-source outcome (BL-R31-1): a source counts as fully
        // embedded only when EVERY one of its chunks produced a
        // vector.
        let mut source_failed = vec![false; to_process.len()];
        for (chunk_idx, entry) in chunk_entries.iter().enumerate() {
            if embeddings[chunk_idx].is_none() {
                source_failed[entry.source_idx] = true;
            }
        }

        // 7. Insert successfully-embedded chunks + advance hashes only
        // for fully-embedded sources.
        let now_iso = now.to_rfc3339();
        let mut chunks_indexed = 0i64;
        {
            let guard = conn.lock().await;
            for (chunk_idx, entry) in chunk_entries.iter().enumerate() {
                let Some(embedding) = &embeddings[chunk_idx] else {
                    continue;
                };
                let source = &to_process[entry.source_idx];
                let n = conexus_db::rag_repository::bulk_index_chunks(
                    &guard,
                    source.source_type,
                    &source.source_ref,
                    &[conexus_db::NewChunk {
                        chunk_text: &entry.text,
                        metadata: None,
                        embedding: Some(embedding),
                    }],
                    &now_iso,
                )?;
                chunks_indexed += n;
            }
            // Group hash updates by source_type -- `set_meta` writes
            // one source_type's worth of hashes per call.
            let mut by_type: std::collections::HashMap<&'static str, Vec<(&str, &str)>> =
                std::collections::HashMap::new();
            for (idx, source) in to_process.iter().enumerate() {
                if !source_failed[idx] {
                    by_type
                        .entry(source.source_type)
                        .or_default()
                        .push((source.source_ref.as_str(), source.hash.as_str()));
                }
            }
            for (source_type, hashes) in &by_type {
                conexus_db::rag_repository::set_meta(&guard, source_type, None, Some(hashes))?;
            }
        }

        let sources_failed = source_failed.iter().filter(|&&f| f).count();

        // 8. Watermark advance, capped below the earliest failed
        // source (BL-R31-1) so a failed row is re-scanned next cycle
        // instead of being skipped forever.
        let failed_refs: Vec<(&str, f64)> = to_process
            .iter()
            .enumerate()
            .filter(|(idx, _)| source_failed[*idx])
            .map(|(_, s)| (s.source_type, s.mod_time))
            .collect();
        let fully_embedded_refs: Vec<(&str, f64)> = to_process
            .iter()
            .enumerate()
            .filter(|(idx, _)| !source_failed[*idx])
            .map(|(_, s)| (s.source_type, s.mod_time))
            .collect();

        advance_watermarks(
            conn,
            &meta,
            last_md_watermark_epoch_pair(last_md_epoch, max_md_epoch, auto_indexing_disabled),
            (last_ctx_epoch, max_ctx_epoch),
            &failed_refs,
            &fully_embedded_refs,
        )
        .await?;

        Ok(CycleReport {
            skipped_no_vss: false,
            sources_scanned,
            sources_updated: to_process.len(),
            chunks_indexed,
            sources_failed,
        })
    }

    /// `None` when auto-indexing is disabled -- the markdown watermark
    /// must not advance for a source type this cycle never scanned.
    fn last_md_watermark_epoch_pair(last: f64, max: f64, disabled: bool) -> Option<(f64, f64)> {
        if disabled {
            None
        } else {
            Some((last, max))
        }
    }

    /// Port of `_watermark_after_failures`. When this source_type has
    /// no failures this cycle, the uncapped max passes straight
    /// through. Otherwise the watermark can advance no further than
    /// `max(old_watermark, every fully-embedded same-type source's
    /// mod_time strictly below the earliest failure)` -- the exact
    /// same three-way `candidates` reduction Python's own function
    /// performs, not a simplified approximation of it (an earlier
    /// draft of this port used an ad hoc "earliest_failed - 1s"
    /// formula instead; caught by re-deriving directly against
    /// Python's real source rather than trusting the paraphrase).
    fn watermark_after_failures(
        old_watermark: f64,
        uncapped_max: f64,
        source_type: &str,
        failed_refs: &[(&str, f64)],
        fully_embedded_refs: &[(&str, f64)],
    ) -> f64 {
        let earliest_failed = failed_refs
            .iter()
            .filter(|(t, _)| *t == source_type)
            .map(|(_, mt)| *mt)
            .fold(f64::INFINITY, f64::min);
        if !earliest_failed.is_finite() {
            return uncapped_max;
        }
        fully_embedded_refs
            .iter()
            .filter(|(t, mt)| *t == source_type && *mt < earliest_failed)
            .map(|(_, mt)| *mt)
            .fold(old_watermark, f64::max)
    }

    /// Caps `max` below the earliest failed same-type source's
    /// `mod_time` (BL-R31-1), then writes `last_indexed_<type>` via
    /// [`conexus_db::rag_repository::set_meta`]. A no-op for a `None`
    /// markdown pair (auto-indexing disabled this cycle).
    async fn advance_watermarks(
        conn: &tokio::sync::Mutex<rusqlite::Connection>,
        _meta: &std::collections::HashMap<String, String>,
        markdown: Option<(f64, f64)>,
        context: (f64, f64),
        failed_refs: &[(&str, f64)],
        fully_embedded_refs: &[(&str, f64)],
    ) -> anyhow::Result<()> {
        let cap = |source_type: &str, last: f64, max: f64| -> f64 {
            watermark_after_failures(last, max, source_type, failed_refs, fully_embedded_refs)
        };

        let guard = conn.lock().await;
        if let Some((last, max)) = markdown {
            let capped = cap("markdown", last, max);
            let iso = chrono::DateTime::<chrono::Utc>::from_timestamp(capped as i64, 0)
                .unwrap_or_default()
                .to_rfc3339();
            conexus_db::rag_repository::set_meta(&guard, "markdown", Some(&iso), None)?;
        }
        let (ctx_last, ctx_max) = context;
        let ctx_capped = cap("context", ctx_last, ctx_max);
        let ctx_iso = chrono::DateTime::<chrono::Utc>::from_timestamp(ctx_capped as i64, 0)
            .unwrap_or_default()
            .to_rfc3339();
        conexus_db::rag_repository::set_meta(&guard, "context", Some(&ctx_iso), None)?;
        Ok(())
    }

    pub async fn run_periodically(shared: Arc<SharedState>, interval: Duration) {
        let get_env = |key: &str| std::env::var(key).ok();
        // Python's own real cycle-to-cycle sleep -- NOT `interval`
        // itself, a 1/5th fraction of it with a 30s floor. Preserved
        // exactly, not "fixed" into something more intuitive; see this
        // module's own `DEFAULT_INTERVAL` doc.
        let sleep_duration = (interval / 5).max(Duration::from_secs(30));
        loop {
            match run_indexing_cycle(
                &shared.conn,
                &shared.sea_orm_db,
                &shared.project_dir,
                &get_env,
                chrono::Utc::now(),
            )
            .await
            {
                Ok(report) if report.skipped_no_vss => {}
                Ok(report) if report.sources_updated == 0 => {}
                Ok(report) => {
                    eprintln!(
                        "conexus-backend: RAG index cycle: {} source(s) updated, {} chunk(s) indexed, {} failed",
                        report.sources_updated, report.chunks_indexed, report.sources_failed
                    );
                }
                Err(e) => {
                    eprintln!("conexus-backend: RAG indexing cycle failed: {e}");
                }
            }
            tokio::time::sleep(sleep_duration).await;
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn sha256_hex_is_stable_and_content_sensitive() {
            let a = sha256_hex("hello");
            let b = sha256_hex("hello");
            let c = sha256_hex("hellp");
            assert_eq!(a, b);
            assert_ne!(a, c);
            assert_eq!(a.len(), 64, "hex-encoded sha256 is 64 chars");
        }

        #[test]
        fn is_ignored_component_matches_the_real_ignore_list_and_hidden_dirs() {
            assert!(is_ignored_component("node_modules"));
            assert!(is_ignored_component(".git"));
            assert!(is_ignored_component(".hidden"));
            assert!(!is_ignored_component("."));
            assert!(!is_ignored_component(".."));
            assert!(!is_ignored_component("docs"));
            assert!(!is_ignored_component("README.md"));
        }

        #[test]
        fn find_markdown_files_recurses_and_skips_ignored_dirs() {
            let dir = tempfile::tempdir().unwrap();
            std::fs::write(dir.path().join("root.md"), "root").unwrap();
            let sub = dir.path().join("docs");
            std::fs::create_dir(&sub).unwrap();
            std::fs::write(sub.join("nested.md"), "nested").unwrap();
            let ignored = dir.path().join("node_modules");
            std::fs::create_dir(&ignored).unwrap();
            std::fs::write(ignored.join("skip.md"), "skip").unwrap();
            std::fs::write(dir.path().join("not-markdown.txt"), "nope").unwrap();

            let mut found: Vec<String> = find_markdown_files(dir.path())
                .into_iter()
                .map(|p| {
                    p.strip_prefix(dir.path())
                        .unwrap()
                        .to_string_lossy()
                        .into_owned()
                })
                .collect();
            found.sort();
            assert_eq!(
                found,
                vec!["docs/nested.md".to_string(), "root.md".to_string()]
            );
        }

        #[test]
        fn format_context_for_embedding_matches_the_real_three_line_shape() {
            assert_eq!(
                format_context_for_embedding("k", "d", "\"v\""),
                "Context Key: k\nDescription: d\nValue: \"v\""
            );
        }

        #[test]
        fn last_md_watermark_epoch_pair_is_none_when_auto_indexing_disabled() {
            assert_eq!(last_md_watermark_epoch_pair(1.0, 2.0, true), None);
            assert_eq!(
                last_md_watermark_epoch_pair(1.0, 2.0, false),
                Some((1.0, 2.0))
            );
        }

        #[test]
        fn watermark_after_failures_passes_through_uncapped_max_with_no_failures() {
            assert_eq!(
                watermark_after_failures(0.0, 100.0, "markdown", &[], &[("markdown", 50.0)]),
                100.0
            );
        }

        #[test]
        fn watermark_after_failures_caps_below_the_earliest_same_type_failure() {
            // fully-embedded sources at 10/40/90; failure at 50 -- must
            // cap at 40 (the highest fully-embedded mod_time strictly
            // below the earliest failure), not the old watermark or the
            // uncapped max.
            let fully_embedded = [("markdown", 10.0), ("markdown", 40.0), ("markdown", 90.0)];
            let failed = [("markdown", 50.0)];
            assert_eq!(
                watermark_after_failures(0.0, 100.0, "markdown", &failed, &fully_embedded),
                40.0
            );
        }

        #[test]
        fn watermark_after_failures_falls_back_to_old_watermark_when_nothing_qualifies() {
            // Every fully-embedded source is at/after the earliest
            // failure -- nothing to advance to, so the OLD watermark
            // wins, never regressing below it.
            let fully_embedded = [("markdown", 60.0)];
            let failed = [("markdown", 50.0)];
            assert_eq!(
                watermark_after_failures(5.0, 100.0, "markdown", &failed, &fully_embedded),
                5.0
            );
        }

        #[test]
        fn watermark_after_failures_ignores_other_source_types() {
            // A "context" failure must never cap the "markdown"
            // watermark -- each source_type's watermark is independent.
            let fully_embedded = [("markdown", 40.0)];
            let failed = [("context", 10.0)];
            assert_eq!(
                watermark_after_failures(0.0, 100.0, "markdown", &failed, &fully_embedded),
                100.0
            );
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
    tokio::spawn(rag_indexing::run_periodically(
        shared.clone(),
        rag_indexing::DEFAULT_INTERVAL,
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

#[cfg(test)]
mod rag_indexing_tests {
    use super::rag_indexing::{run_indexing_cycle, CycleReport};
    use conexus_db::schema::{init_rag_embeddings_table, init_schema};
    use rusqlite::Connection;
    use std::collections::HashMap;
    use std::sync::Once;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    /// `register_sqlite_vec` is a real, process-wide, one-way
    /// registration -- safe to call more than once, pointless to
    /// repeat per test. Same precedent as `rag_repository`'s own test
    /// module.
    static VEC_REGISTERED: Once = Once::new();

    /// A real temp-file DB, both as a rusqlite `Connection` (what
    /// `run_indexing_cycle` locks for its delete/insert phases) and a
    /// sea-orm `DatabaseConnection` (what it reads meta/context rows
    /// through) -- `:memory:` can't be shared across two connection
    /// handles, same rationale as every other Phase G test fixture in
    /// this crate.
    async fn test_db(
        with_vec: bool,
    ) -> (
        tempfile::TempDir,
        tokio::sync::Mutex<Connection>,
        sea_orm::DatabaseConnection,
    ) {
        if with_vec {
            VEC_REGISTERED.call_once(|| {
                assert!(
                    conexus_vec::register_sqlite_vec(),
                    "sqlite-vec must be loadable in the test environment"
                );
            });
        }
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.db");
        let conn = Connection::open(&path).unwrap();
        init_schema(&conn).unwrap();
        if with_vec {
            init_rag_embeddings_table(&conn, 3).unwrap();
        }
        let sea_orm_db = sea_orm::Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        (dir, tokio::sync::Mutex::new(conn), sea_orm_db)
    }

    fn env(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> + Clone {
        let map: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        move |key| map.get(key).cloned()
    }

    /// A throwaway `/embeddings` server returning a fixed 3-dim vector
    /// for every request, matching this crate's own established
    /// real-bound-TCP-listener precedent (Phase D2's RAG clients,
    /// `subject_backfill_tests`) over mocking `reqwest` itself.
    async fn embed_server() -> (String, tokio::task::JoinHandle<()>) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let handle = tokio::spawn(async move {
            loop {
                let Ok((mut socket, _)) = listener.accept().await else {
                    return;
                };
                let mut buf = [0u8; 65536];
                let Ok(n) = socket.read(&mut buf).await else {
                    continue;
                };
                // Reply once per request with as many 3-dim vectors as
                // the request's "input" array asked for -- parsed as
                // real JSON (not a string-splitting guess) so a batch
                // spanning multiple chunk texts gets the correct count.
                let body_str = String::from_utf8_lossy(&buf[..n]);
                let json_start = body_str.find('{').unwrap_or(0);
                let request: serde_json::Value =
                    serde_json::from_str(&body_str[json_start..]).unwrap();
                let input_count = request["input"]
                    .as_array()
                    .map(|a| a.len())
                    .unwrap_or(1)
                    .max(1);
                let items: Vec<String> = (0..input_count)
                    .map(|_| r#"{"embedding":[0.1,0.2,0.3]}"#.to_string())
                    .collect();
                let body = format!(r#"{{"data":[{}]}}"#, items.join(","));
                let resp = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    body.len(),
                    body
                );
                let _ = socket.write_all(resp.as_bytes()).await;
                let _ = socket.shutdown().await;
            }
        });
        (format!("http://{addr}/v1"), handle)
    }

    fn embed_env(base_url: &str) -> Vec<(&'static str, String)> {
        vec![
            ("AGENT_MCP_LLM_BASE_URL", base_url.to_string()),
            ("AGENT_MCP_EMBEDDING_DIMENSION", "3".to_string()),
        ]
    }

    #[tokio::test]
    async fn skipped_when_no_rag_embeddings_table() {
        let (_dir, conn, sea_orm_db) = test_db(false).await;
        let project_dir = tempfile::tempdir().unwrap();
        let report = run_indexing_cycle(
            &conn,
            &sea_orm_db,
            project_dir.path(),
            &env(&[]),
            chrono::Utc::now(),
        )
        .await
        .unwrap();
        assert_eq!(
            report,
            CycleReport {
                skipped_no_vss: true,
                ..Default::default()
            }
        );
    }

    #[tokio::test]
    async fn indexes_a_markdown_file_and_a_context_row_end_to_end() {
        let (_dir, conn, sea_orm_db) = test_db(true).await;
        let project_dir = tempfile::tempdir().unwrap();
        std::fs::write(
            project_dir.path().join("notes.md"),
            "hello from a real markdown file",
        )
        .unwrap();
        conexus_db::project_context_repository::create_new(
            &sea_orm_db,
            "config_deploy_target",
            "\"prod\"",
            Some("where we deploy"),
            "tester",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap()
        .unwrap();

        let (base_url, server) = embed_server().await;
        let pairs = embed_env(&base_url);
        let get_env = env(&pairs
            .iter()
            .map(|(k, v)| (*k, v.as_str()))
            .collect::<Vec<_>>());

        let report = run_indexing_cycle(
            &conn,
            &sea_orm_db,
            project_dir.path(),
            &get_env,
            chrono::Utc::now(),
        )
        .await
        .unwrap();

        assert!(!report.skipped_no_vss);
        assert_eq!(
            report.sources_scanned, 2,
            "one markdown file + one context row"
        );
        assert_eq!(report.sources_updated, 2);
        assert_eq!(report.sources_failed, 0);
        assert!(report.chunks_indexed >= 2, "at least one chunk per source");

        let guard = conn.lock().await;
        let chunk_count: i64 = guard
            .query_row("SELECT COUNT(*) FROM rag_chunks", [], |r| r.get(0))
            .unwrap();
        assert_eq!(chunk_count, report.chunks_indexed);
        let markdown_chunks: i64 = guard
            .query_row(
                "SELECT COUNT(*) FROM rag_chunks WHERE source_type = 'markdown' AND source_ref = 'notes.md'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(markdown_chunks >= 1);
        drop(guard);

        let meta = conexus_db::rag_repository::get_all_meta(&sea_orm_db)
            .await
            .unwrap();
        assert!(meta.contains_key("last_indexed_markdown"));
        assert!(meta.contains_key("last_indexed_context"));
        assert!(meta.contains_key("hash_markdown_notes.md"));
        assert!(meta.contains_key("hash_context_config_deploy_target"));

        server.abort();
    }

    #[tokio::test]
    async fn a_second_cycle_with_unchanged_content_is_a_no_op() {
        let (_dir, conn, sea_orm_db) = test_db(true).await;
        let project_dir = tempfile::tempdir().unwrap();
        std::fs::write(project_dir.path().join("notes.md"), "stable content").unwrap();

        let (base_url, server) = embed_server().await;
        let pairs = embed_env(&base_url);
        let get_env = env(&pairs
            .iter()
            .map(|(k, v)| (*k, v.as_str()))
            .collect::<Vec<_>>());

        let first = run_indexing_cycle(
            &conn,
            &sea_orm_db,
            project_dir.path(),
            &get_env,
            chrono::Utc::now(),
        )
        .await
        .unwrap();
        assert_eq!(first.sources_updated, 1);

        let second = run_indexing_cycle(
            &conn,
            &sea_orm_db,
            project_dir.path(),
            &get_env,
            chrono::Utc::now(),
        )
        .await
        .unwrap();
        assert_eq!(
            second,
            CycleReport {
                sources_scanned: 1,
                ..Default::default()
            },
            "unchanged content hashes identically, so nothing is reprocessed"
        );

        server.abort();
    }

    #[tokio::test]
    async fn disabled_auto_indexing_skips_markdown_but_still_scans_context() {
        let (_dir, conn, sea_orm_db) = test_db(true).await;
        let project_dir = tempfile::tempdir().unwrap();
        std::fs::write(project_dir.path().join("notes.md"), "should be skipped").unwrap();
        conexus_db::project_context_repository::create_new(
            &sea_orm_db,
            "config_still_scanned",
            "\"yes\"",
            None,
            "tester",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap()
        .unwrap();

        let (base_url, server) = embed_server().await;
        let mut pairs = embed_env(&base_url);
        pairs.push(("AGENT_MCP_DISABLE_AUTO_INDEXING", "true".to_string()));
        let get_env = env(&pairs
            .iter()
            .map(|(k, v)| (*k, v.as_str()))
            .collect::<Vec<_>>());

        let report = run_indexing_cycle(
            &conn,
            &sea_orm_db,
            project_dir.path(),
            &get_env,
            chrono::Utc::now(),
        )
        .await
        .unwrap();

        assert_eq!(report.sources_scanned, 1, "markdown scan skipped entirely");
        assert_eq!(report.sources_updated, 1);

        let meta = conexus_db::rag_repository::get_all_meta(&sea_orm_db)
            .await
            .unwrap();
        assert!(
            !meta.contains_key("last_indexed_markdown"),
            "the markdown watermark must not advance for a source type this cycle never scanned"
        );
        assert!(meta.contains_key("last_indexed_context"));

        server.abort();
    }

    #[tokio::test]
    async fn a_failed_embedding_batch_leaves_the_source_reindexable_next_cycle() {
        // No server bound at all -- every embed call fails, matching
        // the real "batch failed" degrade path (BL-R31-1): the source
        // is counted failed, its hash watermark is NOT advanced, so a
        // later cycle will genuinely retry it rather than skipping it
        // forever.
        let (_dir, conn, sea_orm_db) = test_db(true).await;
        let project_dir = tempfile::tempdir().unwrap();
        std::fs::write(project_dir.path().join("notes.md"), "will fail to embed").unwrap();

        let get_env = env(&[
            ("AGENT_MCP_LLM_BASE_URL", "http://127.0.0.1:1/v1"),
            ("AGENT_MCP_EMBEDDING_DIMENSION", "3"),
        ]);

        let report = run_indexing_cycle(
            &conn,
            &sea_orm_db,
            project_dir.path(),
            &get_env,
            chrono::Utc::now(),
        )
        .await
        .unwrap();

        assert_eq!(report.sources_updated, 1);
        assert_eq!(report.sources_failed, 1);
        assert_eq!(report.chunks_indexed, 0);

        let guard = conn.lock().await;
        let chunk_count: i64 = guard
            .query_row("SELECT COUNT(*) FROM rag_chunks", [], |r| r.get(0))
            .unwrap();
        assert_eq!(chunk_count, 0);
        drop(guard);

        let meta = conexus_db::rag_repository::get_all_meta(&sea_orm_db)
            .await
            .unwrap();
        assert!(
            !meta.contains_key("hash_markdown_notes.md"),
            "a failed source's hash must not be recorded, so it's re-scanned next cycle"
        );
    }
}
