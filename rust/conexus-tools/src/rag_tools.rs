//! Port of `agent_mcp/tools/rag_tools.py`'s lone tool (`ask_project_rag`)
//! plus the slice of `agent_mcp/features/rag/query.py::query_rag_system`
//! it needs. First Phase D2 tool -- wires together every prerequisite
//! this phase ported: `conexus_auth`'s async `Tool` (needed here, not
//! optional -- this is the first tool with real network I/O),
//! `conexus_core::task_ownership` (R4-F4 scoping), and
//! `crate::{embedding_client, completion_client, context_window}`.
//!
//! ## SEC Wave-B / R4-F4: agent-only AND `rag.query`-gated
//!
//! [`is_rag_capable_agent`] ports `core.authorize.
//! agent_bearer_with_capability("rag.query")` verbatim: the `kind`
//! half keeps operator-session callers out (operators DO carry
//! `rag.query` in their project bundle -- this tool is agent-only by
//! design); the capability half keeps an `agent_role`-less bearer
//! (empty bundle) out. Neither half is droppable -- see Python's own
//! docstring for the SEC history.
//!
//! Live-task retrieval (both the keyword search and the
//! post-vector-search chunk filter) is scoped to the caller's
//! `view_tasks` visibility via [`conexus_core::task_ownership`] so
//! search can't surface a task the caller couldn't read directly
//! (R4-F4) -- `can_view_all_tasks` is the caller's own `tasks.assign`
//! capability (an agent-bearer manager/sysadmin role can carry it,
//! same as Python), `include_foreign` reads the
//! `config_allow_worker_view_foreign_tasks` project setting (default
//! `true`, matching the Python schema default, since no generic
//! `PolicySource`-backed settings lookup exists yet for a tool that
//! already holds `conn` directly -- see `conexus_auth::requirement`'s
//! own doc on why `PolicySource` stays a trait until a real `Policy`-
//! gated tool needs the DB wiring).
//!
//! ## ADR-0017: no assembly-seam secret scrub
//!
//! Retrieved context (memory/tasks/code/markdown) is assembled AS-IS,
//! same as Python -- protection is by authorization (the scoping
//! above), not content-based secret detection.
//!
//! ## Deliberately NOT ported
//!
//! - The transient `log_audit` in-memory/file trail -- Python's
//!   `ask_project_rag_tool_impl` calls only `log_audit` (no durable
//!   `agent_actions` row), so there is nothing to port to the durable
//!   audit table either; same reasoning as `project_settings_tools`'s
//!   own "no Rust reader yet" note.
//! - `query_rag_system_with_model` (the sibling function used by
//!   `features/task_placement`, not `ask_project_rag`) -- out of scope
//!   until that feature's own phase.
//! - The Python outer function's `RAG_ERR_UNEXPECTED`/generic-Exception
//!   catch-all: every earlier pipeline stage here degrades in place
//!   (a DB/embed/search error swallows to an empty result, matching
//!   Python's own per-stage try/except blocks) rather than needing a
//!   final catch-all -- `Result` makes each stage's failure mode
//!   explicit instead of relying on an outer blanket `except Exception`.

use std::collections::HashSet;

use conexus_auth::{BoxFuture, Requirement, Tool};
use conexus_core::capability::Capability;
use conexus_core::principal::{Principal, PrincipalKind};
use conexus_core::task_ownership;
use conexus_core::tool_result::ToolResult;
use conexus_db::project_settings_repository;
use conexus_db::rag_repository::{self, RagSearchResult};
use rusqlite::{Connection, ToSql};
use serde_json::Value;
use tokio::sync::Mutex as AsyncMutex;

use crate::completion_client;
use crate::context_window;
use crate::embedding_client;

const RAG_DENIED: &str =
    "Unauthorized: agent token with rag.query capability required to query project RAG";

fn is_rag_capable_agent(principal: Option<&Principal>) -> bool {
    matches!(
        principal,
        Some(p) if p.kind == PrincipalKind::AgentBearer && p.has_capability(Capability::RagQuery)
    )
}

fn env_from_process(key: &str) -> Option<String> {
    std::env::var(key).ok()
}

/// `config_allow_worker_view_foreign_tasks` -- an explicit
/// `project_settings` row wins; absent/unparseable defaults to `true`
/// (Python's schema default). This tool reads it directly (it already
/// holds `conn`) rather than through a generic `PolicySource`.
fn config_allow_worker_view_foreign_tasks(conn: &Connection) -> bool {
    project_settings_repository::get(conn, "config_allow_worker_view_foreign_tasks")
        .ok()
        .flatten()
        .and_then(|row| serde_json::from_str::<bool>(&row.value).ok())
        .unwrap_or(true)
}

/// Why [`query_rag_system`] failed to produce an answer. Both variants
/// render to the IDENTICAL generic `Failed` message at the tool
/// boundary (SD-R9-1: never leak provider detail to a worker) --
/// kept as a real enum, not a bare error unit, only so a future
/// logging pass (this workspace has no logging/tracing crate yet --
/// see `project_settings_tools`'s own note) has something to log
/// distinctly. No live behavioral difference between variants today.
#[derive(Debug)]
enum RagQueryError {
    CompletionNotConfigured,
    CompletionUnavailable,
}

const SYSTEM_PROMPT_GENERAL: &str = "You are an AI assistant answering questions about a software project. \
Use the provided context, which may include recently updated live data (like project context keys or tasks) and information retrieved from an indexed knowledge base (like documentation or code summaries), to answer the user's query. \
Prioritize information from the 'Live' sections if available and relevant for time-sensitive data. \
Answer using *only* the information given in the context. If the context doesn't contain the answer, state that clearly.

Be VERBOSE and comprehensive in your responses. It's better to give too much context than too little. \
When answering, please also suggest additional context entries and queries that might be helpful for understanding this topic better.
For example, suggest related files to examine, related project context keys to check, or follow-up questions that could provide more insight.
Always err on the side of providing more detailed explanations and comprehensive information rather than brief responses.";

/// The 6x-duplicated (three sections) accumulation-loop body from
/// Python's `_append_within_budget`. An entry that would bring the
/// running count to exactly `limit` is rejected (strict `<`, not
/// `<=`) -- preserved exactly.
fn append_within_budget(
    parts: &mut Vec<String>,
    entry_text: String,
    count: u64,
    limit: u64,
) -> Option<u64> {
    let entry_tokens = entry_text.split_whitespace().count() as u64;
    if count + entry_tokens < limit {
        parts.push(entry_text);
        Some(count + entry_tokens)
    } else {
        None
    }
}

fn render_chunk(i: usize, item: &RagSearchResult) -> String {
    let chunk = &item.chunk;
    let mut source_info = format!(
        "Source Type: {}, Reference: {}",
        chunk.source_type, chunk.source_ref
    );
    if let Some(metadata) = &chunk.metadata {
        if chunk.source_type == "code" || chunk.source_type == "code_summary" {
            if let Some(language) = metadata.get("language").and_then(Value::as_str) {
                source_info += &format!(", Language: {language}");
            }
            if let Some(section_type) = metadata.get("section_type").and_then(Value::as_str) {
                source_info += &format!(", Section: {section_type}");
            }
            if let Some(entities) = metadata.get("entities").and_then(Value::as_array) {
                if !entities.is_empty() {
                    let names: Vec<String> = entities
                        .iter()
                        .map(|e| {
                            e.get("name")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_string()
                        })
                        .collect();
                    let shown = names.iter().take(3).cloned().collect::<Vec<_>>().join(", ");
                    source_info += &format!(", Contains: {shown}");
                    if names.len() > 3 {
                        source_info += &format!(" (+{} more)", names.len() - 3);
                    }
                }
            }
        }
    }
    format!(
        "Retrieved Chunk {} (Similarity/Distance: {}):\n{source_info}\nContent:\n{}\n",
        i + 1,
        item.distance,
        chunk.chunk_text
    )
}

struct LiveTaskRow {
    task_id: String,
    title: String,
    status: String,
    description: Option<String>,
    updated_at: String,
}

/// Live-task keyword search (title/description `LIKE`), ownership-
/// scoped to the caller's `view_tasks` visibility (R4-F4). Matches
/// Python's raw-SQL shape exactly, including wrapping the `OR`
/// disjunction in parens before `AND`ing the ownership clause --
/// `a OR b AND owner` would bind as `a OR (b AND owner)` and leak on a
/// title match.
fn fetch_live_tasks(
    conn: &Connection,
    query_text: &str,
    requesting_agent_id: Option<&str>,
    can_view_all_tasks: bool,
    include_foreign: bool,
) -> rusqlite::Result<Vec<LiveTaskRow>> {
    let keywords: Vec<String> = query_text
        .split_whitespace()
        .filter(|w| w.trim().len() > 2)
        .map(|w| format!("%{}%", w.to_lowercase()))
        .collect();
    if keywords.is_empty() {
        return Ok(Vec::new());
    }

    let mut conditions = Vec::with_capacity(keywords.len() * 2);
    let mut params: Vec<String> = Vec::with_capacity(keywords.len() * 2);
    for kw in &keywords {
        conditions.push("LOWER(title) LIKE ?");
        params.push(kw.clone());
        conditions.push("LOWER(description) LIKE ?");
        params.push(kw.clone());
    }
    let where_clause = conditions.join(" OR ");

    let (ownership_sql, ownership_params) =
        task_ownership::sql_fragment(requesting_agent_id, can_view_all_tasks, include_foreign);
    params.extend(ownership_params);

    let sql = format!(
        "SELECT task_id, title, status, description, updated_at FROM tasks \
         WHERE ({where_clause}){ownership_sql} ORDER BY updated_at DESC LIMIT 5"
    );

    let mut stmt = conn.prepare(&sql)?;
    let param_refs: Vec<&dyn ToSql> = params.iter().map(|p| p as &dyn ToSql).collect();
    let rows = stmt.query_map(param_refs.as_slice(), |row| {
        Ok(LiveTaskRow {
            task_id: row.get(0)?,
            title: row.get(1)?,
            status: row.get(2)?,
            description: row.get(3)?,
            updated_at: row.get(4)?,
        })
    })?;
    rows.collect()
}

/// Drop vector-search chunks sourced from a task the caller cannot
/// read directly (R4-F4). Only `source_type == "task"` chunks are
/// ownership-scoped; project-wide context/code/markdown chunks are
/// always kept. A `tasks.assign` caller (`can_view_all_tasks`) keeps
/// every chunk.
fn drop_unowned_task_chunks(
    conn: &Connection,
    results: Vec<RagSearchResult>,
    requesting_agent_id: Option<&str>,
    can_view_all_tasks: bool,
    include_foreign: bool,
) -> Vec<RagSearchResult> {
    if can_view_all_tasks {
        return results;
    }
    let task_refs: HashSet<&str> = results
        .iter()
        .filter(|r| r.chunk.source_type == "task")
        .map(|r| r.chunk.source_ref.as_str())
        .collect();
    if task_refs.is_empty() {
        return results;
    }

    let refs: Vec<&str> = task_refs.iter().copied().collect();
    let placeholders = refs.iter().map(|_| "?").collect::<Vec<_>>().join(",");
    let sql = format!("SELECT task_id, assigned_to FROM tasks WHERE task_id IN ({placeholders})");

    let mut visible: HashSet<String> = HashSet::new();
    if let Ok(mut stmt) = conn.prepare(&sql) {
        let param_refs: Vec<&dyn ToSql> = refs.iter().map(|r| r as &dyn ToSql).collect();
        if let Ok(rows) = stmt.query_map(param_refs.as_slice(), |row| {
            let task_id: String = row.get(0)?;
            let assigned_to: Option<String> = row.get(1)?;
            Ok((task_id, assigned_to))
        }) {
            for (task_id, assigned_to) in rows.flatten() {
                if task_ownership::can_access_task(
                    assigned_to.as_deref(),
                    None,
                    requesting_agent_id,
                    false,
                    false,
                    false,
                    include_foreign,
                ) {
                    visible.insert(task_id);
                }
            }
        }
    }

    results
        .into_iter()
        .filter(|r| r.chunk.source_type != "task" || visible.contains(&r.chunk.source_ref))
        .collect()
}

/// The 5-stage pipeline: live context -> live tasks -> vector search
/// -> token-budgeted assembly -> chat completion. Port of
/// `query_rag_system` (the `ask_project_rag`-only call shape; the
/// `query_rag_system_with_model` sibling is out of scope).
///
/// Holds `conn` locked for the whole call, including the network
/// stages -- matches Python's own `query_rag_system`, which holds its
/// single sqlite3 connection open from entry to its `finally:
/// conn.close()`, spanning the identical network calls. Not a new
/// regression this port introduces; a real future refinement (release
/// the lock before the network stage) applies equally to both
/// languages and is out of scope here.
///
/// Takes `&AsyncMutex<Connection>`, not a bare `&Connection` (locks it
/// itself, once, as the very first line) -- this function's own body
/// spans two real `.await` points (the embedding call, the chat call)
/// with DB reads both before AND after, so a bare `&Connection`
/// captured across those awaits would make this async fn's own
/// generated future `!Send` (same root cause as `conexus_auth::tool`'s
/// `BoxFuture` doc). The `MutexGuard` obtained here is `Send` (only
/// needs `Connection: Send`), so holding IT across the awaits is fine.
async fn query_rag_system(
    conn: &AsyncMutex<Connection>,
    query_text: &str,
    requesting_agent_id: Option<&str>,
    can_view_all_tasks: bool,
) -> Result<String, RagQueryError> {
    let conn = conn.lock().await;
    let include_foreign = config_allow_worker_view_foreign_tasks(&conn);

    // --- 1. Live context ---
    let last_indexed = rag_repository::get_last_indexed(&conn, "context")
        .ok()
        .flatten()
        .unwrap_or_else(|| "1970-01-01T00:00:00Z".to_string());
    let live_context =
        rag_repository::fetch_recent_context(&conn, &last_indexed, Some(5)).unwrap_or_default();

    // --- 2. Live tasks (keyword search) ---
    let live_tasks = fetch_live_tasks(
        &conn,
        query_text,
        requesting_agent_id,
        can_view_all_tasks,
        include_foreign,
    )
    .unwrap_or_default();

    // --- 3. Vector search --- (skips the embedding HTTP call
    // entirely when RAG isn't set up, mirroring Python's
    // `is_vss_loadable()` pre-check).
    let mut vector_results: Vec<RagSearchResult> = Vec::new();
    if rag_repository::embeddings_table_exists(&conn).unwrap_or(false) {
        let client = embedding_client::resolve_from_process_env();
        if let Ok(mut vectors) = client.embed(&[query_text.to_string()]).await {
            if let Some(query_embedding) = vectors.pop() {
                if let Ok(results) =
                    rag_repository::search_similar(&conn, &query_embedding, 13, None)
                {
                    vector_results = drop_unowned_task_chunks(
                        &conn,
                        results,
                        requesting_agent_id,
                        can_view_all_tasks,
                        include_foreign,
                    );
                }
            }
        }
        // An embed()/search_similar() failure degrades to "no vector
        // results" (matches Python's own per-stage try/except here --
        // a provider hiccup shouldn't fail the whole query when live
        // context/tasks may already answer it).
    }

    // --- 4. Combine contexts under a token budget ---
    let base_url = completion_client::resolve_chat_base_url(env_from_process);
    let context_budget =
        context_window::resolve_max_context_tokens(env_from_process, base_url.as_deref()).await;

    let mut parts: Vec<String> = Vec::new();
    let mut count: u64 = 0;

    if !live_context.is_empty() {
        parts.push("--- Recently Updated Project Context (Live) ---".to_string());
        for item in &live_context {
            let description = item.description.as_deref().unwrap_or("N/A");
            let entry = format!(
                "Key: {}\nValue: {}\nDescription: {}\n(Updated: {})\n",
                item.context_key, item.value, description, item.updated_at
            );
            match append_within_budget(&mut parts, entry, count, context_budget) {
                Some(c) => count = c,
                None => break,
            }
        }
        parts.push("---------------------------------------------".to_string());
    }

    if !live_tasks.is_empty() {
        parts.push("--- Potentially Relevant Tasks (Live) ---".to_string());
        for task in &live_tasks {
            let description = task.description.as_deref().unwrap_or("N/A");
            let entry = format!(
                "Task ID: {}\nTitle: {}\nStatus: {}\nDescription: {}\n(Updated: {})\n",
                task.task_id, task.title, task.status, description, task.updated_at
            );
            match append_within_budget(&mut parts, entry, count, context_budget) {
                Some(c) => count = c,
                None => break,
            }
        }
        parts.push("---------------------------------------".to_string());
    }

    if !vector_results.is_empty() {
        parts.push("--- Indexed Project Knowledge (Vector Search Results) ---".to_string());
        for (i, item) in vector_results.iter().enumerate() {
            let entry = render_chunk(i, item);
            match append_within_budget(&mut parts, entry, count, context_budget) {
                Some(c) => count = c,
                None => {
                    parts.push(
                        "--- [Indexed knowledge truncated due to token limit] ---".to_string(),
                    );
                    break;
                }
            }
        }
        parts.push("-------------------------------------------------------".to_string());
    }

    if parts.is_empty() {
        return Ok(
            "No relevant information found in the project knowledge base or live data for your query."
                .to_string(),
        );
    }

    let combined_context_str = parts.join("\n\n");
    let user_message = format!(
        "CONTEXT:\n{combined_context_str}\n\nQUERY:\n{query_text}\n\n\
         Based *only* on the CONTEXT provided above, please answer the QUERY."
    );

    // --- 5. Chat completion ---
    let client = completion_client::resolve_from_process_env()
        .map_err(|_| RagQueryError::CompletionNotConfigured)?;
    client
        .chat(
            &[("system", SYSTEM_PROMPT_GENERAL), ("user", &user_message)],
            0.4,
        )
        .await
        .map_err(|_| RagQueryError::CompletionUnavailable)
}

// --- ask_project_rag tool -------------------------------------------

pub struct AskProjectRagTool;

impl Tool for AskProjectRagTool {
    const NAME: &'static str = "ask_project_rag";
    const REQUIRED: Requirement = Requirement::Predicate {
        check: is_rag_capable_agent,
        reason: RAG_DENIED,
    };
    const DESCRIPTION: &'static str = "Ask a natural language question about the project. The system uses RAG (Retrieval Augmented Generation) to find relevant information from indexed documentation, context, and metadata to synthesize an answer.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The natural language question to ask about the project."
            }
        },
        "required": ["query"],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        _now: &'a str,
        _ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let query_text = match arguments.get("query").and_then(Value::as_str) {
                Some(q) if !q.is_empty() => q,
                _ => {
                    return ToolResult::Invalid {
                        field: Some("query".to_string()),
                        message: "query text is required and must be a string.".to_string(),
                    }
                }
            };

            let requesting_agent_id = principal.and_then(|p| p.agent_id.as_deref());
            // The task-visibility marker (operator/manager/sysadmin) --
            // an agent-bearer caller CAN carry this via its role
            // bundle, same as Python.
            let can_view_all_tasks =
                principal.is_some_and(|p| p.has_capability(Capability::TasksAssign));

            match query_rag_system(conn, query_text, requesting_agent_id, can_view_all_tasks).await
            {
                Ok(answer_text) => ToolResult::Ok {
                    data: Some(serde_json::json!({"answer": answer_text})),
                    message: Some(answer_text),
                },
                // SD-R9-1: static, category-only message -- no
                // provider names, URLs, or exception text.
                Err(_) => ToolResult::Failed {
                    message: "RAG is temporarily unavailable (provider or index error); retry \
                        shortly, or ask an operator to check RAG configuration."
                        .to_string(),
                },
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_core::capability::{Capabilities, ProjectRole};
    use conexus_db::schema::init_schema;

    fn test_conn() -> AsyncMutex<Connection> {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        AsyncMutex::new(conn)
    }

    const NOW: &str = "2026-01-01T00:00:00Z";

    fn agent_bearer(agent_id: &str, caps: Capabilities) -> Principal {
        Principal {
            kind: PrincipalKind::AgentBearer,
            user_id: None,
            agent_id: Some(agent_id.to_string()),
            project_name: None,
            project_role: None,
            agent_role: None,
            can_wake_loop: false,
            source_token: None,
            capabilities: caps,
        }
    }

    fn operator_session() -> Principal {
        Principal {
            kind: PrincipalKind::OperatorSession,
            user_id: Some("op1".to_string()),
            agent_id: None,
            project_name: None,
            project_role: Some(ProjectRole::Operator),
            agent_role: None,
            can_wake_loop: false,
            source_token: None,
            capabilities: Capabilities::Sysadmin,
        }
    }

    // ── is_rag_capable_agent ─────────────────────────────────────────

    #[test]
    fn admits_an_agent_bearer_with_rag_query() {
        let p = agent_bearer("a1", Capabilities::from_iter([Capability::RagQuery]));
        assert!(is_rag_capable_agent(Some(&p)));
    }

    #[test]
    fn denies_an_agent_bearer_without_rag_query() {
        let p = agent_bearer("a1", Capabilities::from_iter([]));
        assert!(!is_rag_capable_agent(Some(&p)));
    }

    #[test]
    fn denies_an_operator_session_even_though_it_carries_the_capability() {
        // Sysadmin operator sessions carry every capability in their
        // bundle -- the `kind` half of the predicate is what keeps
        // them out (this tool is agent-only by design).
        let p = operator_session();
        assert!(p.has_capability(Capability::RagQuery));
        assert!(!is_rag_capable_agent(Some(&p)));
    }

    #[test]
    fn denies_a_missing_principal() {
        assert!(!is_rag_capable_agent(None));
    }

    // ── AskProjectRagTool::call -- validation + gating ───────────────

    #[tokio::test]
    async fn rejects_a_missing_query() {
        let conn = test_conn();
        let registry = conexus_wakeloop::waiter_registry::WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
        let p = agent_bearer("a1", Capabilities::from_iter([Capability::RagQuery]));
        let result =
            AskProjectRagTool::call(Some(&p), &serde_json::json!({}), &conn, NOW, &ctx).await;
        assert!(
            matches!(result, ToolResult::Invalid { field, .. } if field.as_deref() == Some("query"))
        );
    }

    #[tokio::test]
    async fn rejects_an_empty_query() {
        let conn = test_conn();
        let registry = conexus_wakeloop::waiter_registry::WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
        let p = agent_bearer("a1", Capabilities::from_iter([Capability::RagQuery]));
        let result = AskProjectRagTool::call(
            Some(&p),
            &serde_json::json!({"query": ""}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(matches!(result, ToolResult::Invalid { .. }));
    }

    #[tokio::test]
    async fn dispatch_denies_a_bearer_without_rag_query() {
        let conn = test_conn();
        let registry = conexus_wakeloop::waiter_registry::WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
        let p = agent_bearer("a1", Capabilities::from_iter([]));
        let descriptor = conexus_auth::ToolDescriptor::of::<AskProjectRagTool>();
        let result = conexus_auth::dispatch(
            &descriptor,
            Some(&p),
            &conexus_auth::NoPolicyOverrides,
            &serde_json::json!({"query": "what is this project"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert_eq!(
            result,
            ToolResult::PermissionDenied {
                reason: RAG_DENIED.to_string()
            }
        );
    }

    // ── query_rag_system -- degrade paths (no completion provider) ───
    //
    // With no OPENAI_API_KEY / OLLAMA endpoint reachable in the test
    // sandbox, an empty knowledge base + no live matches returns the
    // "no relevant information" success text WITHOUT ever needing a
    // real completion call -- proving the empty-context early-return
    // actually short-circuits before touching the network.

    #[tokio::test]
    async fn empty_project_returns_the_no_relevant_information_answer_without_calling_completion() {
        let conn = test_conn();
        let result = query_rag_system(&conn, "anything", Some("a1"), false).await;
        assert_eq!(
            result.unwrap(),
            "No relevant information found in the project knowledge base or live data for your \
             query."
        );
    }

    // ── fetch_live_tasks ──────────────────────────────────────────────

    fn insert_task(
        conn: &Connection,
        task_id: &str,
        title: &str,
        assigned_to: Option<&str>,
        created_by: &str,
    ) {
        conn.execute(
            "INSERT INTO tasks (task_id, title, description, status, priority, assigned_to, \
             created_by, created_at, updated_at) \
             VALUES (?1, ?2, 'desc', 'pending', 'medium', ?3, ?4, ?5, ?5)",
            rusqlite::params![task_id, title, assigned_to, created_by, NOW],
        )
        .unwrap();
    }

    #[tokio::test]
    async fn fetch_live_tasks_matches_by_title_keyword() {
        let conn = test_conn();
        let guard = conn.lock().await;
        insert_task(&guard, "task-1", "Fix the login bug", Some("a1"), "a1");
        let rows = fetch_live_tasks(&guard, "login bug", Some("a1"), true, false).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].task_id, "task-1");
    }

    #[tokio::test]
    async fn fetch_live_tasks_scopes_to_the_requesters_own_tasks_by_default() {
        let conn = test_conn();
        let guard = conn.lock().await;
        insert_task(
            &guard,
            "task-1",
            "Fix the login bug",
            Some("someone-else"),
            "op1",
        );
        let rows = fetch_live_tasks(&guard, "login bug", Some("a1"), false, false).unwrap();
        assert!(
            rows.is_empty(),
            "a worker must not see another agent's task via RAG search"
        );
    }

    #[tokio::test]
    async fn fetch_live_tasks_include_foreign_widens_the_scope() {
        let conn = test_conn();
        let guard = conn.lock().await;
        insert_task(
            &guard,
            "task-1",
            "Fix the login bug",
            Some("someone-else"),
            "op1",
        );
        let rows = fetch_live_tasks(&guard, "login bug", Some("a1"), false, true).unwrap();
        assert_eq!(rows.len(), 1);
    }

    #[tokio::test]
    async fn fetch_live_tasks_ignores_short_words() {
        let conn = test_conn();
        let guard = conn.lock().await;
        insert_task(&guard, "task-1", "Fix the login bug", Some("a1"), "a1");
        // "in", "on", "at" etc. (len <= 2) are dropped, matching
        // Python's `len(word.strip()) > 2` filter.
        let rows = fetch_live_tasks(&guard, "in on at", Some("a1"), true, false).unwrap();
        assert!(rows.is_empty());
    }

    // ── drop_unowned_task_chunks ──────────────────────────────────────

    fn chunk_result(source_type: &str, source_ref: &str) -> RagSearchResult {
        RagSearchResult {
            chunk: conexus_db::RagChunkRow {
                chunk_id: 1,
                source_type: source_type.to_string(),
                source_ref: source_ref.to_string(),
                chunk_text: "text".to_string(),
                indexed_at: NOW.to_string(),
                metadata: None,
            },
            distance: 0.1,
        }
    }

    #[tokio::test]
    async fn drop_unowned_task_chunks_keeps_non_task_chunks_unconditionally() {
        let conn = test_conn();
        let guard = conn.lock().await;
        let results = vec![chunk_result("markdown", "README.md")];
        let filtered = drop_unowned_task_chunks(&guard, results, Some("a1"), false, false);
        assert_eq!(filtered.len(), 1);
    }

    #[tokio::test]
    async fn drop_unowned_task_chunks_drops_a_task_chunk_the_caller_cannot_access() {
        let conn = test_conn();
        let guard = conn.lock().await;
        insert_task(&guard, "task-1", "Secret task", Some("someone-else"), "op1");
        let results = vec![chunk_result("task", "task-1")];
        let filtered = drop_unowned_task_chunks(&guard, results, Some("a1"), false, false);
        assert!(filtered.is_empty());
    }

    #[tokio::test]
    async fn drop_unowned_task_chunks_keeps_a_task_chunk_the_caller_owns() {
        let conn = test_conn();
        let guard = conn.lock().await;
        insert_task(&guard, "task-1", "My task", Some("a1"), "a1");
        let results = vec![chunk_result("task", "task-1")];
        let filtered = drop_unowned_task_chunks(&guard, results, Some("a1"), false, false);
        assert_eq!(filtered.len(), 1);
    }

    #[tokio::test]
    async fn drop_unowned_task_chunks_keeps_everything_for_a_view_all_caller() {
        let conn = test_conn();
        let guard = conn.lock().await;
        insert_task(
            &guard,
            "task-1",
            "Someone else's task",
            Some("someone-else"),
            "op1",
        );
        let results = vec![chunk_result("task", "task-1")];
        let filtered = drop_unowned_task_chunks(&guard, results, Some("a1"), true, false);
        assert_eq!(filtered.len(), 1);
    }

    // ── config_allow_worker_view_foreign_tasks ────────────────────────

    #[tokio::test]
    async fn config_defaults_true_when_no_row_is_set() {
        let conn = test_conn();
        let guard = conn.lock().await;
        assert!(config_allow_worker_view_foreign_tasks(&guard));
    }

    #[tokio::test]
    async fn config_respects_an_explicit_false_row() {
        let conn = test_conn();
        let guard = conn.lock().await;
        project_settings_repository::upsert(
            &guard,
            "config_allow_worker_view_foreign_tasks",
            "false",
            None,
            false,
            "op1",
            NOW,
        )
        .unwrap();
        assert!(!config_allow_worker_view_foreign_tasks(&guard));
    }

    // ── append_within_budget boundary ─────────────────────────────────

    #[test]
    fn append_within_budget_rejects_an_entry_that_would_hit_the_limit_exactly() {
        let mut parts = Vec::new();
        // "one two" = 2 tokens; count(0) + 2 == limit(2) -> rejected (strict <).
        let result = append_within_budget(&mut parts, "one two".to_string(), 0, 2);
        assert_eq!(result, None);
        assert!(parts.is_empty());
    }

    #[test]
    fn append_within_budget_accepts_an_entry_strictly_under_the_limit() {
        let mut parts = Vec::new();
        let result = append_within_budget(&mut parts, "one two".to_string(), 0, 3);
        assert_eq!(result, Some(2));
        assert_eq!(parts, vec!["one two".to_string()]);
    }
}
