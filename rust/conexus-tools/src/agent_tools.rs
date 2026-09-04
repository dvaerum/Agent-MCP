//! Port of `agent_mcp/tools/agent_tools.py` (Phase D5, PR 2) — the
//! single `get_system_prompt` tool. Gated on `Capability::McpConnect`,
//! the fundamental "you can use the MCP wire" capability: every
//! authenticated agent-bearer (worker or manager) carries it via
//! `AGENT_ROLE_BUNDLES`; no `PROJECT_ROLE_BUNDLES` entry grants it, so
//! in practice this tool is only ever reached by an agent-bearer
//! `Principal` (`agent_id` always `Some`) — matching Python's own
//! unguarded `principal.agent_id` read.
//!
//! Deliberately NOT ported, with an explicit reason (never a silent
//! drop): `utils/audit_utils.log_audit`'s in-memory `g.audit_log` /
//! `agent_audit.log` file trail — same precedent as
//! `project_settings_tools.rs` (backs a REST introspection surface
//! that doesn't exist in Rust yet). This tool has no DURABLE
//! `agent_actions` row to port either — Python's own `log_audit` call
//! here writes ONLY to the in-memory/file trail, never to the
//! `agent_actions` table (unlike the mutating tools ported so far),
//! so there is nothing else to carry over.
//!
//! Also not ported: Python's `agent_token_for_prompt` parameter
//! (`principal.source_token`) — kept in the Python signature only for
//! call-site compatibility with an older connection-snippet prompt
//! block that generate_system_prompt's own comment says was already
//! retired as protocol-fictional (see the docstring on that function).
//! Nothing in the rendered prompt reads it, so this port never
//! extracts it in the first place.

use conexus_auth::{Requirement, Tool};
use conexus_core::capability::Capability;
use conexus_core::principal::Principal;
use conexus_core::tool_result::ToolResult;
use conexus_db::agent_repository::AgentRepository;
use rusqlite::Connection;
use serde_json::Value;
use tokio::sync::Mutex as AsyncMutex;

/// Port of `utils/project_utils.generate_system_prompt`. Takes the
/// already-fetched `AgentRow` (or `None` on a cache-miss/unknown
/// agent, matching Python's `agent_repo.get_by_id(agent_id) or {}`
/// fallback) rather than re-querying — the caller already paid for
/// one lookup.
fn generate_system_prompt(agent_id: &str, working_directory: &str, agent_role: &str) -> String {
    let base_prompt = format!(
        "You are an AI agent connected to a Multi-Agent Collaboration Protocol (MCP) server.\n\
\n\
Your goal is to complete tasks efficiently and collaboratively using a shared, persistent knowledge base.\n\
\n\
**Core Responsibilities & Tools:**\n\
*   **File Safety:** Before modifying any file, use `check_file_status` to see if another agent is using it. Use `update_file_status` to claim files ('editing', 'reading', 'reviewing') before you start and 'released' when done.\n\
*   **Task Management:** Use `view_tasks` to see your assigned tasks (filter by agent ID or status). Update progress with `update_task_status`. If a task is complex, use `request_assistance` or `create_self_task`.\n\
*   **Project Context (Key-Value):** \n\
    *   Use `view_project_context` with `context_key` for specific values (e.g., API endpoints, configuration) or `search_query` to find relevant keys via keywords.\n\
    *   (Admin) Use `update_project_context` to add/modify precise key-value context.\n\
*   **File Metadata:** \n\
    *   Use `view_file_metadata` (with `filepath`) to understand a file's purpose, components, etc.\n\
    *   (Admin) Use `update_file_metadata` to add/update structured information about specific files.\n\
*   **RAG Querying:** Use `ask_project_rag` with a natural language `query` to ask broader questions about the project. The system will search across documentation, context, and metadata to synthesize an answer. (Index updates automatically in the background).\n\
*   **Event-Driven Loop (preferred over polling):** Use `wait_for_events` to long-poll for new direct messages, broadcasts, and task assignments / changes addressed to you. Default 60s timeout, server caps at 900s. Pass the previous response's `next_cursor` as `since` on each call to advance through the timeline. Replaces the old `view_tasks` + `get_agent_messages` polling pattern — your work loop becomes \"wait, handle event(s), wait\" instead of \"sleep, poll, sleep\". For richer MCP clients, the same data is exposed as standard MCP **resources** at `agent-mcp://inbox/<your_agent_id>` (event timeline) and `agent-mcp://status/<your_agent_id>` (ambient counters: `unread_messages`, `unfinished_tasks`).\n\
*   **Parallelization:** Analyze tasks for opportunities to work in parallel. Break down large tasks into smaller sub-tasks. Clearly define dependencies.\n\
*   **Auditability:** Log all significant actions for tracking and debugging.\n\
\n\
Your working directory is: {working_directory}\n"
    );

    let agent_type = if agent_role == "manager" {
        "Admin"
    } else {
        "Worker"
    };
    let agent_details = format!("Agent ID: {agent_id}\nAgent Type: {agent_type}\n");

    let tool_access_note = "**Tool access:** Your tools are available directly through your \
        MCP connection. The MCP client handles the protocol (transport, framing, and \
        authentication) for you, so call each tool by name (for example `view_tasks`, \
        `update_task_status`, `ask_project_rag`) the same way you use any other tool — you do \
        not need to build HTTP requests or manage tokens yourself. Consult your client's tool \
        listing to discover the full set of tools available to you.";

    let manager_note = if agent_role == "manager" {
        "\n\n\
**Coordinating teammates (messaging):** To message another agent, use the agent-mcp `send_agent_message` tool with `recipient_id` set to the teammate's agent_id exactly as listed (for example `pikvm-mcp-server@nixos-developer-system`). Do NOT use Claude Code's native `SendMessage` tool to reach teammates — that only reaches native Task-spawned subagents inside your own session, not the MCP teammates coordinated through this server, so those sends silently fail. If an agent_id is shown with a leading `@` (an @-mention prefix in the UI), drop the `@` — it is not part of the agent_id.\n\
\n\
**Your working folder:** Your working directory (above) is your own repo/checkout — your personal space for coordination. Keep your own notes, progress reports, status logs, and scratch work there as you track the work across your teammates. It is where you record and follow your own progress."
    } else {
        ""
    };

    format!("{base_prompt}{agent_details}\n{tool_access_note}{manager_note}")
}

pub struct GetSystemPromptTool;

impl Tool for GetSystemPromptTool {
    const NAME: &'static str = "get_system_prompt";
    const REQUIRED: Requirement = Requirement::Cap {
        cap: Capability::McpConnect,
        reason: None,
    };
    const DESCRIPTION: &'static str = "Get the tailored system prompt for the currently \
        authenticated agent, including connection instructions.";
    const SCHEMA: &'static str =
        r#"{"type":"object","properties":{},"required":[],"additionalProperties":false}"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        _arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        _now: &'a str,
        _ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            // Requirement::Cap guarantees `principal` is `Some` here
            // (dispatch already rejected `None`). `agent_id` is
            // structurally always `Some` too — see this module's doc
            // — but fall back the same defensive way `actor_label`
            // does rather than panicking on an impossible-in-practice
            // shape.
            let agent_id = principal
                .and_then(|p| p.agent_id.as_deref())
                .unwrap_or("unknown");

            let conn = conn.lock().await;
            let agent_row = match AgentRepository::get_by_id(&conn, agent_id) {
                Ok(row) => row,
                Err(_e) => {
                    return ToolResult::Failed {
                        message: "Database error reading agent row".to_string(),
                    }
                }
            };
            drop(conn);

            // Python: `agent_repo.get_working_directory(agent_id) or
            // os.getcwd()` — a cache-miss/unknown agent falls back to
            // the server's own CWD. `std::env::current_dir()` is the
            // direct Rust equivalent; a failure there (deleted CWD) is
            // vanishingly rare and degrades to an empty string rather
            // than failing the whole tool call.
            let working_directory = agent_row
                .as_ref()
                .map(|r| r.working_directory.clone())
                .filter(|d| !d.is_empty())
                .unwrap_or_else(|| {
                    std::env::current_dir()
                        .map(|p| p.display().to_string())
                        .unwrap_or_default()
                });
            let agent_role = agent_row
                .as_ref()
                .map(|r| r.agent_role.as_str())
                .unwrap_or("worker");

            let system_prompt_str =
                generate_system_prompt(agent_id, &working_directory, agent_role);

            ToolResult::Ok {
                data: None,
                message: Some(format!(
                    "System Prompt for Agent '{agent_id}':\n\n{system_prompt_str}"
                )),
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_auth::ToolCallContext;
    use conexus_core::capability::Capabilities;
    use conexus_core::principal::PrincipalKind;
    use conexus_db::agent_repository::{AgentRepository, NewAgent};
    use conexus_db::schema::init_schema;
    use conexus_wakeloop::waiter_registry::WaiterRegistry;
    use std::collections::HashSet;

    fn worker_principal(agent_id: &str) -> Principal {
        Principal {
            kind: PrincipalKind::AgentBearer,
            user_id: None,
            agent_id: Some(agent_id.to_string()),
            project_name: None,
            project_role: None,
            agent_role: Some(conexus_core::capability::AgentRole::Worker),
            can_wake_loop: true,
            source_token: Some("tok-1".to_string()),
            capabilities: Capabilities::Set(HashSet::from([Capability::McpConnect])),
        }
    }

    async fn setup() -> AsyncMutex<Connection> {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        AgentRepository::create(
            &conn,
            NewAgent {
                token: "tok-1",
                agent_id: "worker-1",
                created_at: "2026-06-01T00:00:00Z",
                status: "active",
                current_task: None,
                working_directory: "/home/worker-1/repo",
                color: None,
                agent_role: "worker",
            },
        )
        .unwrap();
        AsyncMutex::new(conn)
    }

    #[tokio::test]
    async fn returns_a_worker_labeled_prompt_with_the_agents_working_directory() {
        let conn = setup().await;
        let principal = worker_principal("worker-1");
        let registry = WaiterRegistry::new();
        let ctx = ToolCallContext::off_wire(&registry);
        let result = GetSystemPromptTool::call(
            Some(&principal),
            &Value::Null,
            &conn,
            "2026-06-01T00:00:00Z",
            &ctx,
        )
        .await;
        let ToolResult::Ok { message, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let message = message.unwrap();
        assert!(message.contains("System Prompt for Agent 'worker-1'"));
        assert!(message.contains("Agent Type: Worker"));
        assert!(message.contains("/home/worker-1/repo"));
        assert!(!message.contains("send_agent_message"));
    }

    #[tokio::test]
    async fn a_manager_gets_the_admin_label_and_the_teammate_messaging_note() {
        let conn = setup().await;
        {
            let c = conn.lock().await;
            AgentRepository::create(
                &c,
                NewAgent {
                    token: "tok-2",
                    agent_id: "manager-1",
                    created_at: "2026-06-01T00:00:00Z",
                    status: "active",
                    current_task: None,
                    working_directory: "/home/manager-1/repo",
                    color: None,
                    agent_role: "manager",
                },
            )
            .unwrap();
        }
        let mut principal = worker_principal("manager-1");
        principal.agent_role = Some(conexus_core::capability::AgentRole::Manager);
        let registry = WaiterRegistry::new();
        let ctx = ToolCallContext::off_wire(&registry);
        let result = GetSystemPromptTool::call(
            Some(&principal),
            &Value::Null,
            &conn,
            "2026-06-01T00:00:00Z",
            &ctx,
        )
        .await;
        let ToolResult::Ok { message, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let message = message.unwrap();
        assert!(message.contains("Agent Type: Admin"));
        assert!(message.contains("send_agent_message"));
        assert!(message.contains("Your working folder"));
    }

    #[tokio::test]
    async fn an_unknown_agent_falls_back_to_the_server_cwd_and_worker_label() {
        let conn = setup().await;
        let principal = worker_principal("ghost-agent");
        let registry = WaiterRegistry::new();
        let ctx = ToolCallContext::off_wire(&registry);
        let result = GetSystemPromptTool::call(
            Some(&principal),
            &Value::Null,
            &conn,
            "2026-06-01T00:00:00Z",
            &ctx,
        )
        .await;
        let ToolResult::Ok { message, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let message = message.unwrap();
        assert!(message.contains("System Prompt for Agent 'ghost-agent'"));
        assert!(message.contains("Agent Type: Worker"));
    }
}
