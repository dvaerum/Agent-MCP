//! Port of `agent_mcp/tools/admin_tools.py` (Phase D5, PR 9 -- the
//! final Phase D5 module, 2627 LOC). 9 registered tools:
//! `register_agent`, `view_status`, `terminate_agent`,
//! `rotate_agent_token`, `restore_agent`, `edit_agent`, `purge_agent`,
//! `view_audit_log`, `get_agent_tokens`. See
//! `/home/dennis/.claude/plans/prancy-napping-pie.md`'s "Phase D5
//! (admin_tools.py)" section for the full scoping report and the
//! 9-PR breakdown this module follows.
//!
//! **Confirmed out of scope**: `disconnect_agent`/`reconnect_agent`/
//! `disconnect_all_agents`/`reconnect_all_agents` are defined in
//! Python's `admin_tools.py` but never `register_tool`'d as MCP
//! tools -- their only callers are `agent_mcp/app/routers/agents.py`
//! (REST). Phase E1 territory (`agent_mcp/app/*`), not this port.
//!
//! ## PR1: pure helpers + new primitives
//!
//! No tools registered -- matches this migration's own "PR1 pure
//! helpers, no tools" precedent (`project_context_tools.rs`,
//! `task_tools.rs`).
//!
//! ## PR2: `view_audit_log`
//!
//! **Re-derived, not ported at face value** (per the plan's own
//! "things to explicitly re-derive" discipline): Python's
//! `view_audit_log` reads `g.audit_log`, a process-local, non-durable
//! in-memory list every OTHER tool port in this migration has already
//! deliberately dropped in favor of the durable `agent_actions` table
//! alone (PRs #793, #823, #824). This tool is the first whose whole
//! job is reading that trail back -- resolved (plan file, "Phase D5
//! (admin_tools.py)") by porting against `agent_actions` instead of
//! building a new in-memory ring buffer just to preserve a trail every
//! other port treats as disposable. Real, deliberate consequence: the
//! rendered `action` strings are `agent_actions.action_type` values
//! (e.g. `"updated_context"`), NOT necessarily identical to whatever
//! string Python's in-memory sink used for the same event (the two
//! trails' action-name vocabularies were never unified in Python
//! either -- e.g. `register_agent` writes `"registered_agent"` to the
//! DB sink and `"register_agent"` to the in-memory sink).

use conexus_auth::{Requirement, Tool};
use conexus_core::capability::Capability;
use conexus_core::principal::{is_confirmed_operator_tier, Principal};
use conexus_core::tool_result::ToolResult;
use conexus_db::agent_action_repository;
use conexus_db::agent_repository::{
    parse_agent_sort_by, parse_sort_order, AgentQueryFilters, AgentRepository,
};
use rusqlite::Connection;
use serde_json::Value;
use std::sync::LazyLock;
use tokio::sync::Mutex as AsyncMutex;

/// Port of `core/auth.py::generate_token` -- `secrets.token_hex(16)`,
/// 128 bits of OS-CSPRNG entropy, hex-encoded. `getrandom` (not a
/// general-purpose PRNG) is the direct equivalent of Python's
/// `secrets` module, which is itself backed by `os.urandom` --
/// deliberately not reached for a faster non-cryptographic generator
/// for a value that gates bearer authentication.
pub fn generate_token() -> String {
    let mut bytes = [0u8; 16];
    getrandom::fill(&mut bytes).expect("OS CSPRNG must be available to mint an agent bearer token");
    hex::encode(bytes)
}

/// Seeded onto every `manager`-role agent at registration. Port of
/// `core/agent_profile_defaults.py::MANAGER_DEFAULT_PROFILE`, kept
/// verbatim -- this is user-facing copy, not logic, so there's
/// nothing to re-derive.
pub const MANAGER_DEFAULT_PROFILE: &str = "You are a manager. Your role:\n\
    - Break down and assign work to the workers on your team, and review \
    what they deliver.\n\
    - Curate your team's profiles: keep each worker's `profile` accurate \
    (who does what, what tools they have, what to ask them about) so the \
    team can find the right person.\n\
    - Coordinate across the team — route questions, unblock workers, and \
    keep shared context current.\n\
    \n\
    Replace this charter with a description of how YOU actually operate: \
    your focus areas, the parts of the system you own, and what peers \
    should come to you for. Call `update_agent_profile` to update it, or \
    to confirm it is still accurate.";

/// Port of `core/config.py::AGENT_COLORS` -- the round-robin color
/// palette assigned to newly registered agents. The round-robin INDEX
/// itself is process-wide mutable state (Python: `g.agent_color_index`)
/// -- threaded onto `SharedState` in PR6 (`register_agent`) the same
/// explicit way `waiter_registry`/`file_map`/`project_dir` were; this
/// module only owns the pure palette + the pure lookup.
pub const AGENT_COLORS: &[&str] = &[
    "#FF5733", "#33FF57", "#3357FF", "#FF33A1", "#A133FF", "#33FFA1", "#FFBD33", "#33FFBD",
    "#BD33FF", "#FF3333", "#33FF33", "#3333FF", "#FF8C00", "#00CED1", "#9400D3", "#FF1493",
    "#7FFF00", "#1E90FF",
];

/// The next color for round-robin index `index` (any value; wraps via
/// modulo, matching Python's `AGENT_COLORS[g.agent_color_index %
/// len(AGENT_COLORS)]`).
pub fn next_agent_color(index: usize) -> &'static str {
    AGENT_COLORS[index % AGENT_COLORS.len()]
}

/// `agents` columns holding a credential, not a display value --
/// port of `core/agent_secrets.py::agent_secret_columns()`, which
/// Python derives dynamically from the ORM model's
/// `info={"secret": True}` column metadata. Rust's `AgentRow` has no
/// per-field-metadata mechanism to derive this from, so it's hardcoded
/// here instead (matching this migration's own established closed-list
/// precedent -- `CRITICAL_KEY_PATTERNS`, `PUBLIC_TOOL_ALLOWLIST`) --
/// paired with `every_agents_table_column_is_accounted_for` below
/// (this module's own tests) to close the "a new secret column ships
/// unredacted" risk structurally rather than by convention alone.
pub const AGENT_SECRET_FIELDS: &[&str] = &["token", "aoe_session_id"];

/// Full mask for a withheld bearer. Full, not a prefix/suffix elision
/// -- port of `core/agent_secrets.py::REDACTED_TOKEN`; the previous
/// `token[:4] + "..." + token[-4:]` form disclosed 8 characters of a
/// secret to a non-operator caller (viewer-read-gating finding 3).
pub const REDACTED_TOKEN: &str = "***";

/// Port of `core/agent_secrets.py::redact_agent_row`. A confirmed
/// operator-tier caller gets `row` unchanged; anyone else gets every
/// [`AGENT_SECRET_FIELDS`] key masked to [`REDACTED_TOKEN`]. Keys stay
/// present either way, so a client can tell a masked value from an
/// absent one. Operates on the JSON projection (`AgentRow` already
/// derives `Serialize`) rather than the typed struct, since masking a
/// `String` field to a fixed sentinel needs no other field access.
pub fn redact_agent_row(
    row: &conexus_db::agent_repository::AgentRow,
    confirmed_operator_tier: bool,
) -> Value {
    let mut value = serde_json::to_value(row).expect("AgentRow always serializes");
    if confirmed_operator_tier {
        return value;
    }
    if let Value::Object(map) = &mut value {
        for field in AGENT_SECRET_FIELDS {
            if map.contains_key(*field) {
                map.insert(
                    (*field).to_string(),
                    Value::String(REDACTED_TOKEN.to_string()),
                );
            }
        }
    }
    value
}

/// Last-resort host for the `.mcp.json` snippet when neither the
/// caller's request nor `$AGENT_MCP_EXTERNAL_URL` says where this
/// deployment is reachable from. Obviously fake so an operator who
/// pastes the snippet realizes they need to substitute the real host.
const DEFAULT_REGISTER_AGENT_URL_BASE: &str = "https://REPLACE_WITH_YOUR_AGENT_MCP_HOST";

/// Port of `_resolve_snippet_host`. `get_env` is an explicit lookup
/// (not a direct `std::env::var` read) matching this crate's own
/// Phase D2 RAG-clients convention -- sidesteps `cargo test`'s
/// parallel-thread env-var-race hazard.
pub fn resolve_snippet_host(
    host_arg: Option<&str>,
    get_env: impl Fn(&str) -> Option<String>,
) -> String {
    if let Some(raw) = host_arg {
        let trimmed = raw.trim();
        if !trimmed.is_empty() {
            return trimmed.trim_end_matches('/').to_string();
        }
    }
    if let Some(env_host) = get_env("AGENT_MCP_EXTERNAL_URL") {
        let trimmed = env_host.trim();
        if !trimmed.is_empty() {
            return trimmed.trim_end_matches('/').to_string();
        }
    }
    DEFAULT_REGISTER_AGENT_URL_BASE.to_string()
}

/// Port of `_resolve_snippet_project`. `principal`'s `project_name` is
/// the router-populated fallback when the caller's arguments don't
/// carry an explicit override.
pub fn resolve_snippet_project(
    project_name_arg: Option<&str>,
    principal: Option<&Principal>,
) -> Option<String> {
    if let Some(raw) = project_name_arg {
        let trimmed = raw.trim();
        if !trimmed.is_empty() {
            return Some(trimmed.to_string());
        }
    }
    principal.and_then(|p| p.project_name.clone())
}

/// Port of `_build_mcp_config_snippet`. Pretty-printed (`indent=2`,
/// matching Python's `json.dumps(..., indent=2)`) so a caller can drop
/// the result straight into a `<pre>` block. The server key is the
/// fixed string `"agent-mcp"` regardless of `project` (see Python's
/// own doc: a namespaced key would produce an ugly
/// `agent-mcp-<project>:` slash-command prefix; project scoping lives
/// in the URL, not the key).
pub fn build_mcp_config_snippet(
    project: Option<&str>,
    token: &str,
    host: &str,
    mount_prefix: &str,
) -> String {
    let url = match project {
        Some(p) => format!("{host}{mount_prefix}/mcp/{p}"),
        None => format!("{host}/mcp"),
    };
    let snippet = serde_json::json!({
        "mcpServers": {
            "agent-mcp": {
                "type": "http",
                "url": url,
                "headers": {"Authorization": format!("Bearer {token}")},
            }
        }
    });
    serde_json::to_string_pretty(&snippet).expect("a snippet of only strings always serializes")
}

/// Clamp an incoming `limit` argument the same way Python's
/// `view_audit_log_tool_impl` does: any value that doesn't parse as an
/// integer in `[1, 200]` silently falls back to the default (50),
/// rather than erroring -- a malformed limit degrading to "just show
/// me the recent activity" is treated as more useful than a 400.
fn clamp_audit_log_limit(arguments: &Value) -> i64 {
    const DEFAULT: i64 = 50;
    match arguments.get("limit") {
        None => DEFAULT,
        Some(v) => match v.as_i64() {
            Some(n) if (1..=200).contains(&n) => n,
            _ => DEFAULT,
        },
    }
}

pub struct ViewAuditLogTool;

impl Tool for ViewAuditLogTool {
    const NAME: &'static str = "view_audit_log";
    const REQUIRED: Requirement = Requirement::Cap {
        // SECURITY (viewer-read-gating, matches Python's own comment):
        // system.config.write (operator-only), NOT system.view -- the
        // audit log discloses operator user_ids and every agent
        // action, and system.view is in the VIEWER bundle.
        cap: Capability::SystemConfigWrite,
        reason: None,
    };
    const DESCRIPTION: &'static str = "Read recent audit-log entries. Operator-only.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "Optional: filter by agent ID"
            },
            "action": {
                "type": "string",
                "description": "Optional: filter by action type"
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of entries to return (default: 50, max: 200)",
                "default": 50
            }
        },
        "required": [],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        _principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        _now: &'a str,
        _ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let filter_agent_id = arguments.get("agent_id").and_then(Value::as_str);
            let filter_action = arguments.get("action").and_then(Value::as_str);
            let limit = clamp_audit_log_limit(arguments);

            let guard = conn.lock().await;
            let rows = match agent_action_repository::list_recent(
                &guard,
                filter_agent_id,
                filter_action,
                limit,
            ) {
                Ok(rows) => rows,
                Err(_e) => {
                    return ToolResult::Failed {
                        message: "A database error occurred; it has been logged. Retry, or ask \
                            an operator to check logs."
                            .to_string(),
                    }
                }
            };
            drop(guard);

            let entries: Vec<Value> = rows
                .iter()
                .map(|row| {
                    serde_json::json!({
                        "timestamp": row.timestamp,
                        "agent_id": row.agent_id,
                        "action": row.action_type,
                        "details": row.details,
                    })
                })
                .collect();
            let log_json = serde_json::to_string_pretty(&entries)
                .expect("every field here is already a plain JSON value");

            ToolResult::Ok {
                data: Some(serde_json::json!({
                    "entries": entries,
                    "count": entries.len(),
                    "filter_agent_id": filter_agent_id,
                    "filter_action": filter_action,
                    "limit": limit,
                })),
                message: Some(format!(
                    "Audit Log ({} entries displayed, filtered by agent: {}, action: {}):\n{}",
                    entries.len(),
                    filter_agent_id.unwrap_or("Any"),
                    filter_action.unwrap_or("Any"),
                    log_json,
                )),
            }
        })
    }
}

/// One process-wide instance, matching Python's module-level
/// `agent_repo` (an imported module is itself a singleton) -- the
/// pagination cache must survive across calls the same way
/// `task_tools.rs`'s `VIEW_TASKS_ENGINE` does.
static GET_AGENT_TOKENS_REPO: LazyLock<AgentRepository> = LazyLock::new(AgentRepository::new);

/// The canonical `sort_by` string this crate's `AgentQueryFilters`
/// will actually use -- mirrors `parse_agent_sort_by`'s own
/// allowlist-with-fallback so the tool's REPORTED `sort.sort_by`
/// matches what was really applied (a caller passing `"updated_at"`,
/// which Python's tool-layer pre-check allows but the repository's OWN
/// allowlist does not, silently falls back to `created_at` -- a real,
/// preserved Python inconsistency between the tool's validation and
/// the repository's, not reconciled here).
fn effective_agent_sort_by(raw: &str) -> &'static str {
    match raw {
        "agent_id" => "agent_id",
        "status" => "status",
        "terminated_at" => "terminated_at",
        _ => "created_at",
    }
}

fn effective_sort_order(raw: &str) -> &'static str {
    if raw.eq_ignore_ascii_case("ASC") {
        "ASC"
    } else {
        "DESC"
    }
}

pub struct GetAgentTokensTool;

impl Tool for GetAgentTokensTool {
    const NAME: &'static str = "get_agent_tokens";
    const REQUIRED: Requirement = Requirement::Cap {
        // SECURITY (FINDING 2, ported verbatim): agent bearer tokens
        // are operator-tier secrets. agents.register is the operation
        // that MINTS + returns a bearer, so viewing existing bearers
        // belongs to the same tier -- NOT agents.view (viewer bundle),
        // which would leak plaintext tokens to read-only viewers.
        cap: Capability::AgentsRegister,
        reason: None,
    };
    const DESCRIPTION: &'static str = "Retrieve agent tokens with advanced filtering. \
        Operator-only.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "filter_status": {"type": "string"},
            "filter_agent_id_pattern": {"type": "string"},
            "filter_created_after": {"type": "string"},
            "filter_created_before": {"type": "string"},
            "include_terminated": {"type": "boolean", "default": false},
            "include_sensitive_data": {"type": "boolean", "default": false},
            "limit": {"type": "integer", "default": 50},
            "offset": {"type": "integer", "default": 0},
            "sort_by": {"type": "string", "default": "created_at"},
            "sort_order": {"type": "string", "default": "DESC"}
        },
        "required": [],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        now: &'a str,
        _ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let filter_status = arguments.get("filter_status").and_then(Value::as_str);
            let filter_agent_id_pattern = arguments
                .get("filter_agent_id_pattern")
                .and_then(Value::as_str);
            let filter_created_after = arguments
                .get("filter_created_after")
                .and_then(Value::as_str);
            let filter_created_before = arguments
                .get("filter_created_before")
                .and_then(Value::as_str);
            let include_terminated = arguments
                .get("include_terminated")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            let include_sensitive_data = arguments
                .get("include_sensitive_data")
                .and_then(Value::as_bool)
                .unwrap_or(false);

            let limit = match arguments.get("limit").and_then(Value::as_i64) {
                Some(n) if (1..=500).contains(&n) => n,
                _ => 50,
            };
            let offset = match arguments.get("offset").and_then(Value::as_i64) {
                Some(n) if n >= 0 => n,
                _ => 0,
            };
            let sort_by_raw = arguments
                .get("sort_by")
                .and_then(Value::as_str)
                .unwrap_or("created_at");
            let sort_order_raw = arguments
                .get("sort_order")
                .and_then(Value::as_str)
                .unwrap_or("DESC");
            let effective_sort_by = effective_agent_sort_by(sort_by_raw);
            let effective_sort_order = effective_sort_order(sort_order_raw);

            let guard = conn.lock().await;
            let (rows, total_count) = match GET_AGENT_TOKENS_REPO.query(
                &guard,
                AgentQueryFilters {
                    status: filter_status,
                    agent_id_pattern: filter_agent_id_pattern,
                    include_terminated,
                    created_after: filter_created_after,
                    created_before: filter_created_before,
                    sort_by: parse_agent_sort_by(sort_by_raw),
                    sort_order: parse_sort_order(sort_order_raw),
                    limit,
                    offset,
                },
            ) {
                Ok(result) => result,
                Err(_e) => {
                    return ToolResult::Failed {
                        message: "A database error occurred; it has been logged. Retry, or ask \
                            an operator to check logs."
                            .to_string(),
                    }
                }
            };
            drop(guard);

            // SECURITY (FINDING 2): plaintext tokens surface ONLY when
            // the caller both explicitly opted in AND is confirmed
            // operator tier -- a viewer whose group grant let it pass
            // the coarse Cap gate above is still masked.
            let expose_tokens =
                include_sensitive_data && principal.is_some_and(is_confirmed_operator_tier);
            let agents_data: Vec<Value> = rows
                .iter()
                .map(|row| redact_agent_row(row, expose_tokens))
                .collect();

            let requesting_agent_id = principal.map(Principal::actor_label).unwrap_or("operator");
            let guard = conn.lock().await;
            let _ = agent_action_repository::log_agent_action(
                &guard,
                requesting_agent_id,
                "get_agent_tokens",
                None,
                Some(&serde_json::json!({
                    "filter_status": filter_status,
                    "filter_agent_id_pattern": filter_agent_id_pattern,
                    "agents_returned": agents_data.len(),
                    "total_matching": total_count,
                    "include_sensitive_data": include_sensitive_data,
                    "tokens_exposed": expose_tokens,
                })),
                now,
            );
            drop(guard);

            let has_more = offset + (agents_data.len() as i64) < total_count;
            let response_data = serde_json::json!({
                "agents": agents_data,
                "pagination": {
                    "offset": offset,
                    "limit": limit,
                    "total_count": total_count,
                    "returned_count": agents_data.len(),
                    "has_more": has_more,
                },
                "filters_applied": {
                    "filter_status": filter_status,
                    "filter_agent_id_pattern": filter_agent_id_pattern,
                    "filter_created_after": filter_created_after,
                    "filter_created_before": filter_created_before,
                    "include_terminated": include_terminated,
                    "include_sensitive_data": expose_tokens,
                },
                "sort": {"sort_by": effective_sort_by, "sort_order": effective_sort_order},
            });
            let response_json = serde_json::to_string_pretty(&response_data)
                .expect("every field here is already a plain JSON value");

            ToolResult::Ok {
                data: Some(response_data),
                message: Some(format!(
                    "Agent Tokens ({} of {} total):\n{}",
                    agents_data.len(),
                    total_count,
                    response_json,
                )),
            }
        })
    }
}

/// Port of `view_status_tool_impl` -- **re-derived, not ported at face
/// value** (per the plan's "things to explicitly re-derive"
/// discipline, and the scoping report's own finding): Python's payload
/// is almost entirely a snapshot of in-memory caches
/// (`g.active_agents`/`g.agent_working_dirs`/`g.connections`) this
/// migration deliberately never built, since every Rust repository
/// reads fresh from SQLite on every call. Substitutions, each
/// documented at its own field below:
///
/// - `agents_details`/`active_agents_count`: `AgentRepository::
///   list_active` (DB-fresh) replaces `g.active_agents`. One filter
///   difference kept intentionally: `list_active`'s own 2-status SQL
///   exclusion (`terminated`, `tombstone`) does NOT exclude the
///   synthetic `"system"` pseudo-agent row, but Python's cache-based
///   `view_status` never sees it either (only `register_agent_tool_
///   impl` ever inserts into `g.active_agents`, and `"system"` is a
///   DB-only bootstrap row, never registered that way) -- so this
///   port filters `status != "system"` explicitly to match the real
///   observed behavior, not `list_active`'s literal SQL alone.
/// - `working_directory`/`color`/`current_task`: read directly off
///   each `AgentRow` -- no separate `g.agent_working_dirs` cache
///   needed, the repository row already carries them.
/// - `file_map_size`/`file_map_preview`: `ctx.file_map` (already
///   real, process-wide state via `ToolCallContext`, ported PR #825)
///   replaces `g.file_map` directly -- a like-for-like substitution,
///   not a re-derivation.
/// - `active_connections`: **dropped, not re-derived.** No live
///   MCP session/stream registry exists in this workspace yet (see
///   the plan's "Phase D5 (admin_tools.py)" decision 2 -- live stream
///   teardown itself is deferred for the same reason). Reported as
///   `0` with a documented, tracked gap rather than inventing a
///   registry speculatively for one diagnostic field.
/// - `server_uptime`: **hardcoded to `"N/A"`.** Python's own code
///   comment ("Server uptime was N/A in original... For now, keeping
///   it N/A for 1-to-1") shows this was the ORIGINAL migration's own
///   stance before a later, apparently uncoordinated change wired in
///   `g.server_start_time`. Threading a server-boot timestamp onto
///   `ToolCallContext`/`SharedState` (a 4th mechanical sweep of the
///   same shape as `waiter_registry`/`file_map`/`project_dir`) for one
///   cosmetic diagnostic string is a real cost or a real, tracked
///   simplification for now -- not silently dropped, documented here.
pub struct ViewStatusTool;

impl Tool for ViewStatusTool {
    const NAME: &'static str = "view_status";
    const REQUIRED: Requirement = Requirement::Cap {
        // SECURITY (viewer-read-gating, ported verbatim): system.
        // config.write (operator-only), NOT system.view (VIEWER
        // bundle) -- this data includes every agent's absolute
        // working directory.
        cap: Capability::SystemConfigWrite,
        reason: None,
    };
    const DESCRIPTION: &'static str = "Report active agents + server status. Operator-only.";
    const SCHEMA: &'static str =
        r#"{"type": "object", "properties": {}, "required": [], "additionalProperties": false}"#;

    fn call<'a>(
        _principal: Option<&'a Principal>,
        _arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        _now: &'a str,
        ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let guard = conn.lock().await;
            let rows = match conexus_db::agent_repository::AgentRepository::list_active(&guard) {
                Ok(rows) => rows,
                Err(_e) => {
                    return ToolResult::Failed {
                        message: "A database error occurred; it has been logged. Retry, or ask \
                            an operator to check logs."
                            .to_string(),
                    }
                }
            };
            drop(guard);

            let live_agents: Vec<_> = rows.iter().filter(|r| r.status != "system").collect();
            let agents_details: serde_json::Map<String, Value> = live_agents
                .iter()
                .map(|row| {
                    (
                        row.agent_id.clone(),
                        serde_json::json!({
                            "status": row.status,
                            "current_task": row.current_task,
                            "working_directory": row.working_directory,
                            "color": row.color.clone().unwrap_or_else(|| "N/A".to_string()),
                        }),
                    )
                })
                .collect();
            let file_map_preview: serde_json::Map<String, Value> = ctx
                .file_map
                .preview(5)
                .into_iter()
                .map(|(path, entry)| (path, serde_json::json!(entry)))
                .collect();

            let status_payload = serde_json::json!({
                "active_connections": 0,
                "active_agents_count": live_agents.len(),
                "agents_details": agents_details,
                "server_uptime": "N/A",
                "file_map_size": ctx.file_map.len(),
                "file_map_preview": file_map_preview,
            });
            let status_json = serde_json::to_string_pretty(&status_payload)
                .expect("every field here is already a plain JSON value");

            ToolResult::Ok {
                data: Some(status_payload),
                message: Some(format!("MCP Server Status:\n{status_json}")),
            }
        })
    }
}

/// Process-wide round-robin index for [`next_agent_color`] -- Python's
/// `g.agent_color_index`. A private atomic counter local to this
/// module rather than a `ToolCallContext`/`SharedState` field: nothing
/// outside `register_agent` ever needs to read or write it, so
/// threading it through the same explicit-shared-state mechanism as
/// `waiter_registry`/`file_map`/`project_dir` (each needed by MULTIPLE
/// tools) would be over-engineering for a single tool's own internal
/// counter -- a plain process-wide `static` gives the identical
/// "persists across calls, one instance per process" semantics.
static AGENT_COLOR_INDEX: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

/// `[`/`]` are reserved for the purge-cascade tombstone format
/// (`[deleted-<id>]`) -- checked up front for a precise operator-
/// facing reason; `AgentRepository::create`'s own regex would also
/// reject them (defense in depth, not the only gate).
fn contains_reserved_bracket(agent_id: &str) -> bool {
    agent_id.contains('[') || agent_id.contains(']')
}

/// Port of `agent_repo._is_reserved_agent_id` -- checked up front here
/// too (same precise-reason rationale as the bracket guard above);
/// `AgentRepository::create`'s own prefix check is the real,
/// load-bearing gate.
fn is_reserved_agent_id(agent_id: &str) -> bool {
    agent_id.to_lowercase().starts_with("admin")
}

pub struct RegisterAgentTool;

impl Tool for RegisterAgentTool {
    const NAME: &'static str = "register_agent";
    const REQUIRED: Requirement = Requirement::Cap {
        cap: Capability::AgentsRegister,
        reason: None,
    };
    const DESCRIPTION: &'static str = "Register an agent identity. Operator-only. No spawning \
        -- returns a bearer token + a ready-to-paste .mcp.json snippet.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "agent_id for the new row"},
            "agent_id": {"type": "string", "description": "Back-compat alias for name"},
            "role": {"type": "string", "description": "'worker' or 'manager', default worker"},
            "agent_role": {"type": "string", "description": "Back-compat alias for role"},
            "project_name": {"type": "string"},
            "host": {"type": "string"},
            "mount_prefix": {"type": "string"}
        },
        "required": [],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        now: &'a str,
        ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let name = arguments
                .get("name")
                .and_then(Value::as_str)
                .or_else(|| arguments.get("agent_id").and_then(Value::as_str))
                .map(str::trim)
                .filter(|s| !s.is_empty());
            let Some(agent_id) = name else {
                return ToolResult::Invalid {
                    field: Some("name".to_string()),
                    message: "`name` (agent_id) is required and must be a non-empty string."
                        .to_string(),
                };
            };

            let role = arguments
                .get("role")
                .and_then(Value::as_str)
                .or_else(|| arguments.get("agent_role").and_then(Value::as_str))
                .unwrap_or("worker");
            if role != "worker" && role != "manager" {
                return ToolResult::Invalid {
                    field: Some("role".to_string()),
                    message: "`role` must be 'worker' or 'manager'.".to_string(),
                };
            }

            if contains_reserved_bracket(agent_id) {
                return ToolResult::Invalid {
                    field: Some("name".to_string()),
                    message: format!(
                        "invalid name {agent_id:?}: `[` and `]` are reserved characters \
                         (used by the purge-cascade tombstone format `[deleted-<id>]`)."
                    ),
                };
            }
            if is_reserved_agent_id(agent_id) {
                return ToolResult::Invalid {
                    field: Some("name".to_string()),
                    message: format!(
                        "reserved name {agent_id:?}: names beginning with 'admin' are \
                         reserved for privileged / built-in identities and cannot be \
                         assigned to an agent."
                    ),
                };
            }

            // Note: Python also checks `agent_id in g.agent_working_dirs`
            // (an in-memory cache) for a "(in active memory)" Conflict
            // BEFORE the DB check -- no Rust equivalent cache exists
            // (every tool port in this workspace reads fresh from
            // SQLite), so only the DB-level Conflict below is reachable
            // here; the in-memory-duplicate branch has no Rust analogue
            // to port, not a dropped behavior.
            let new_agent_token = generate_token();
            let working_directory = ctx.project_dir.display().to_string();
            let color_index = AGENT_COLOR_INDEX.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            let agent_color = next_agent_color(color_index);

            let guard = conn.lock().await;
            let created_row = match conexus_db::agent_repository::AgentRepository::create(
                &guard,
                conexus_db::agent_repository::NewAgent {
                    token: &new_agent_token,
                    agent_id,
                    created_at: now,
                    status: "created",
                    current_task: None,
                    working_directory: &working_directory,
                    color: Some(agent_color),
                    agent_role: role,
                },
            ) {
                Ok(row) => row,
                Err(conexus_db::agent_repository::CreateAgentError::InvalidAgentId(_)) => {
                    return ToolResult::Invalid {
                        field: Some("name".to_string()),
                        message: format!("invalid name {agent_id:?}."),
                    }
                }
                Err(conexus_db::agent_repository::CreateAgentError::Conflict(_)) => {
                    return ToolResult::Conflict {
                        reason: format!("Agent '{agent_id}' already exists (in database)."),
                    }
                }
                Err(conexus_db::agent_repository::CreateAgentError::Db(_)) => {
                    return ToolResult::Failed {
                        message: "A database error occurred; it has been logged. Retry, or ask \
                            an operator to check logs."
                            .to_string(),
                    }
                }
            };

            if role == "manager" {
                let seed_ts = created_row.created_at.clone();
                let _ = conexus_db::agent_repository::AgentRepository::seed_manager_profile(
                    &guard,
                    agent_id,
                    MANAGER_DEFAULT_PROFILE,
                    &seed_ts,
                );
            }

            let requesting_agent_id = principal.map(Principal::actor_label).unwrap_or("operator");
            let _ = agent_action_repository::log_agent_action(
                &guard,
                requesting_agent_id,
                "registered_agent",
                None,
                Some(&serde_json::json!({"agent_id": agent_id, "role": role})),
                now,
            );
            drop(guard);

            let project_name_arg = arguments.get("project_name").and_then(Value::as_str);
            let project_for_snippet = resolve_snippet_project(project_name_arg, principal);
            let host_arg = arguments.get("host").and_then(Value::as_str);
            let host_for_snippet = resolve_snippet_host(host_arg, |key| std::env::var(key).ok());
            let mount_prefix = arguments
                .get("mount_prefix")
                .and_then(Value::as_str)
                .unwrap_or("/agent-mcp");
            let snippet = build_mcp_config_snippet(
                project_for_snippet.as_deref(),
                &new_agent_token,
                &host_for_snippet,
                mount_prefix,
            );

            ToolResult::Ok {
                data: Some(serde_json::json!({
                    "agent_id": agent_id,
                    "token": new_agent_token,
                    "agent_role": role,
                    "mcp_snippet": snippet,
                    "project_name": project_for_snippet,
                })),
                message: Some(format!(
                    "Agent '{agent_id}' registered. Paste the snippet into the user's claude \
                     .mcp.json — agent-mcp no longer spawns the claude session itself."
                )),
            }
        })
    }
}

/// `agents` statuses a bearer rotation/restore refuses to act past.
/// Port of `agent_repository.TERMINAL_AGENT_STATUSES` -- kept inline
/// (not re-exported from `conexus-db`) since only this module's three
/// lifecycle tools need it.
const TERMINAL_AGENT_STATUSES: &[&str] = &["terminated", "tombstone"];

pub struct RotateAgentTokenTool;

impl Tool for RotateAgentTokenTool {
    const NAME: &'static str = "rotate_agent_token";
    const REQUIRED: Requirement = Requirement::Cap {
        cap: Capability::AgentsRotateToken,
        reason: None,
    };
    const DESCRIPTION: &'static str = "Replace an agent's bearer token, preserving its \
        identity. The new token is shown ONCE.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {"agent_id": {"type": "string"}},
        "required": ["agent_id"],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        now: &'a str,
        _ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let Some(agent_id) = arguments.get("agent_id").and_then(Value::as_str) else {
                return ToolResult::Invalid {
                    field: Some("agent_id".to_string()),
                    message: "`agent_id` is required.".to_string(),
                };
            };

            let guard = conn.lock().await;
            let Some(row) =
                (match conexus_db::agent_repository::AgentRepository::get_by_id(&guard, agent_id) {
                    Ok(row) => row,
                    Err(_e) => {
                        return ToolResult::Failed {
                            message:
                                "A database error occurred; it has been logged. Retry, or ask \
                            an operator to check logs."
                                    .to_string(),
                        }
                    }
                })
            else {
                return ToolResult::NotFound {
                    resource: "agent".to_string(),
                    identifier: agent_id.to_string(),
                    hint: None,
                };
            };

            if TERMINAL_AGENT_STATUSES.contains(&row.status.as_str()) {
                return ToolResult::Conflict {
                    reason: format!(
                        "Agent '{agent_id}' is {}; restore it before rotating its token",
                        row.status
                    ),
                };
            }

            let old_token = row.token;
            let new_token = generate_token();
            match conexus_db::agent_repository::AgentRepository::rotate_token(
                &guard, agent_id, &new_token, now,
            ) {
                Ok(true) => {}
                Ok(false) => {
                    return ToolResult::Failed {
                        message: format!("Failed to rotate token for agent '{agent_id}'."),
                    }
                }
                Err(_e) => {
                    return ToolResult::Failed {
                        message: "A database error occurred; it has been logged. Retry, or ask \
                            an operator to check logs."
                            .to_string(),
                    }
                }
            }

            let requesting_agent_id = principal.map(Principal::actor_label).unwrap_or("operator");
            // SECURITY: suffixes only, never a plaintext bearer -- same
            // discipline the durable audit trail applies elsewhere.
            let old_suffix = old_token.get(old_token.len().saturating_sub(4)..);
            let new_suffix = &new_token[new_token.len() - 4..];
            let _ = agent_action_repository::log_agent_action(
                &guard,
                requesting_agent_id,
                "rotated_agent_token",
                None,
                Some(&serde_json::json!({
                    "agent_id": agent_id,
                    "old_token_suffix": old_suffix,
                    "new_token_suffix": new_suffix,
                })),
                now,
            );
            drop(guard);

            ToolResult::Ok {
                data: Some(serde_json::json!({"agent_id": agent_id, "token": new_token})),
                message: Some(format!(
                    "Agent '{agent_id}' token rotated. The previous token is revoked \
                     immediately — hand the new one to the agent's claude session and \
                     relaunch it. This is the only time the new token is shown."
                )),
            }
        })
    }
}

pub struct RestoreAgentTool;

impl Tool for RestoreAgentTool {
    const NAME: &'static str = "restore_agent";
    const REQUIRED: Requirement = Requirement::Cap {
        cap: Capability::AgentsTerminate,
        reason: None,
    };
    const DESCRIPTION: &'static str = "Reverse a soft-delete: flip a terminated agent's status \
        back to 'created'.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {"agent_id": {"type": "string"}},
        "required": ["agent_id"],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        now: &'a str,
        _ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let Some(agent_id) = arguments.get("agent_id").and_then(Value::as_str) else {
                return ToolResult::Invalid {
                    field: Some("agent_id".to_string()),
                    message: "`agent_id` is required.".to_string(),
                };
            };

            let guard = conn.lock().await;
            let Some(row) =
                (match conexus_db::agent_repository::AgentRepository::get_by_id(&guard, agent_id) {
                    Ok(row) => row,
                    Err(_e) => {
                        return ToolResult::Failed {
                            message:
                                "A database error occurred; it has been logged. Retry, or ask \
                            an operator to check logs."
                                    .to_string(),
                        }
                    }
                })
            else {
                return ToolResult::NotFound {
                    resource: "agent".to_string(),
                    identifier: agent_id.to_string(),
                    hint: None,
                };
            };

            if row.status != "terminated" {
                return ToolResult::Conflict {
                    reason: format!(
                        "Agent '{agent_id}' is not terminated (status={:?}); nothing to restore",
                        row.status
                    ),
                };
            }

            use conexus_db::agent_repository::{AgentField, AgentRepository, FieldValue};
            if AgentRepository::update_field(
                &guard,
                agent_id,
                AgentField::Status,
                FieldValue::Text("created".to_string()),
                now,
            )
            .is_err()
            {
                return ToolResult::Failed {
                    message: "A database error occurred; it has been logged. Retry, or ask an \
                        operator to check logs."
                        .to_string(),
                };
            }
            let _ = AgentRepository::update_field(
                &guard,
                agent_id,
                AgentField::TerminatedAt,
                FieldValue::OptionalText(None),
                now,
            );

            let requesting_agent_id = principal.map(Principal::actor_label).unwrap_or("operator");
            let _ = agent_action_repository::log_agent_action(
                &guard,
                requesting_agent_id,
                "restored_agent",
                None,
                Some(&serde_json::json!({"agent_id": agent_id})),
                now,
            );
            drop(guard);

            ToolResult::Ok {
                data: Some(serde_json::json!({"agent_id": agent_id, "status": "created"})),
                message: Some(format!("Agent '{agent_id}' restored")),
            }
        })
    }
}

/// Whitelisted editable agent fields -- port of
/// `EDITABLE_AGENT_FIELDS`. Anything outside this list is silently
/// ignored (defense in depth: status/agent_id/token must never flow
/// through the edit surface).
const EDITABLE_AGENT_FIELDS: &[&str] = &[
    "color",
    "working_directory",
    "aoe_session_id",
    "auto_event_loop",
    "agent_role",
];

pub struct EditAgentTool;

impl Tool for EditAgentTool {
    const NAME: &'static str = "edit_agent";
    const REQUIRED: Requirement = Requirement::Cap {
        cap: Capability::AgentsTerminate,
        reason: None,
    };
    const DESCRIPTION: &'static str = "Update mutable agent fields (color, working_directory, \
        aoe_session_id, auto_event_loop, agent_role).";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "agent_id": {"type": "string"},
            "color": {"type": "string"},
            "working_directory": {"type": "string"},
            "aoe_session_id": {"type": "string"},
            "auto_event_loop": {"type": "boolean"},
            "agent_role": {"type": "string"}
        },
        "required": ["agent_id"],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        now: &'a str,
        ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let Some(agent_id) = arguments.get("agent_id").and_then(Value::as_str) else {
                return ToolResult::Invalid {
                    field: Some("agent_id".to_string()),
                    message: "`agent_id` is required.".to_string(),
                };
            };

            let Value::Object(args_map) = arguments else {
                return ToolResult::Invalid {
                    field: None,
                    message: "No editable fields supplied.".to_string(),
                };
            };
            let updates: Vec<&str> = EDITABLE_AGENT_FIELDS
                .iter()
                .filter(|f| args_map.contains_key(**f))
                .copied()
                .collect();
            if updates.is_empty() {
                return ToolResult::Invalid {
                    field: None,
                    message: format!(
                        "No editable fields supplied. Accepts any of: {}",
                        EDITABLE_AGENT_FIELDS.join(", ")
                    ),
                };
            }
            if let Some(role) = arguments.get("agent_role").and_then(Value::as_str) {
                if role != "worker" && role != "manager" {
                    return ToolResult::Invalid {
                        field: Some("agent_role".to_string()),
                        message: format!(
                            "Invalid agent_role {role:?}: must be 'worker' or 'manager'."
                        ),
                    };
                }
            }

            use conexus_db::agent_repository::{AgentField, AgentRepository, FieldValue};

            let guard = conn.lock().await;
            match AgentRepository::get_by_id(&guard, agent_id) {
                Ok(Some(_)) => {}
                Ok(None) => {
                    return ToolResult::NotFound {
                        resource: "agent".to_string(),
                        identifier: agent_id.to_string(),
                        hint: None,
                    }
                }
                Err(_e) => {
                    return ToolResult::Failed {
                        message: "A database error occurred; it has been logged. Retry, or ask \
                            an operator to check logs."
                            .to_string(),
                    }
                }
            }

            let mut applied: Vec<&str> = Vec::with_capacity(updates.len());
            for field in &updates {
                let (agent_field, value) = match *field {
                    "color" => (
                        AgentField::Color,
                        FieldValue::OptionalText(
                            arguments
                                .get("color")
                                .and_then(Value::as_str)
                                .map(str::to_string),
                        ),
                    ),
                    "working_directory" => (
                        AgentField::WorkingDirectory,
                        FieldValue::Text(
                            arguments
                                .get("working_directory")
                                .and_then(Value::as_str)
                                .unwrap_or_default()
                                .to_string(),
                        ),
                    ),
                    "aoe_session_id" => {
                        // Clear sentinel: an explicit "" means clear
                        // (Python's REST adapter normalizes None to ""
                        // since a raw None gets stripped by the
                        // dispatch layer before reaching the impl).
                        let raw = arguments.get("aoe_session_id").and_then(Value::as_str);
                        let cleared = raw.map(|s| {
                            if s.is_empty() {
                                None
                            } else {
                                Some(s.to_string())
                            }
                        });
                        (
                            AgentField::AoeSessionId,
                            FieldValue::OptionalText(cleared.flatten()),
                        )
                    }
                    "auto_event_loop" => (
                        AgentField::AutoEventLoop,
                        FieldValue::Bool(
                            arguments
                                .get("auto_event_loop")
                                .and_then(Value::as_bool)
                                .unwrap_or(false),
                        ),
                    ),
                    "agent_role" => (
                        AgentField::AgentRole,
                        FieldValue::Text(
                            arguments
                                .get("agent_role")
                                .and_then(Value::as_str)
                                .unwrap_or_default()
                                .to_string(),
                        ),
                    ),
                    _ => unreachable!("filtered to EDITABLE_AGENT_FIELDS above"),
                };
                if AgentRepository::update_field(&guard, agent_id, agent_field, value, now).is_err()
                {
                    return ToolResult::Failed {
                        message: format!("Failed to update field {field:?}"),
                    };
                }
                applied.push(field);
            }

            let requesting_agent_id = principal.map(Principal::actor_label).unwrap_or("operator");
            let _ = agent_action_repository::log_agent_action(
                &guard,
                requesting_agent_id,
                "edited_agent",
                None,
                Some(&serde_json::json!({"agent_id": agent_id, "fields": applied})),
                now,
            );
            drop(guard);

            if applied.contains(&"auto_event_loop") {
                ctx.waiter_registry.notify(agent_id);
            }

            ToolResult::Ok {
                data: Some(serde_json::json!({"agent_id": agent_id, "updated": applied})),
                message: Some(format!(
                    "Agent '{agent_id}' updated: {}",
                    applied.join(", ")
                )),
            }
        })
    }
}

pub struct TerminateAgentTool;

impl Tool for TerminateAgentTool {
    const NAME: &'static str = "terminate_agent";
    const REQUIRED: Requirement = Requirement::Cap {
        cap: Capability::AgentsTerminate,
        reason: None,
    };
    const DESCRIPTION: &'static str = "Soft-terminate an agent: revoke its token and flip its \
        status. Operator-only.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {"agent_id": {"type": "string"}},
        "required": ["agent_id"],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        _principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        now: &'a str,
        ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let Some(agent_id) = arguments.get("agent_id").and_then(Value::as_str) else {
                return ToolResult::Invalid {
                    field: Some("agent_id".to_string()),
                    message: "`agent_id` to terminate is required.".to_string(),
                };
            };

            use conexus_db::agent_repository::AgentRepository;
            use conexus_wakeloop::event_feed::UNASSIGNED_TASK_TERMINAL_STATUSES as TERMINAL_TASK_STATUSES;

            let guard = conn.lock().await;
            // `terminate()`'s own NOT_TERMINAL_SQL guard covers BOTH
            // "no such agent" and "already terminal" with the same
            // `false` -- matches Python's own combined outcome here
            // (no in-memory cache to distinguish the two cases either,
            // and no separate Conflict branch exists for terminate,
            // unlike rotate_agent_token's explicit check).
            match AgentRepository::terminate(&guard, agent_id, now) {
                Ok(true) => {}
                Ok(false) => {
                    return ToolResult::NotFound {
                        resource: "agent".to_string(),
                        identifier: agent_id.to_string(),
                        hint: None,
                    }
                }
                Err(_e) => {
                    return ToolResult::Failed {
                        message: "A database error occurred; it has been logged. Retry, or ask \
                            an operator to check logs."
                            .to_string(),
                    }
                }
            }

            // Wave-B: reconcile tasks so no ACTIVE task is stranded on
            // the now-terminated agent. Terminal tasks keep their
            // attribution -- terminate is a soft-delete, and reverting
            // a completed task would destroy completion history.
            let reassigned: Vec<String> =
                match conexus_db::task_repository::list_by_agent(&guard, agent_id, None, None) {
                    Ok(rows) => rows
                        .into_iter()
                        .filter(|t| !TERMINAL_TASK_STATUSES.contains(&t.status.as_str()))
                        .map(|t| t.task_id)
                        .collect(),
                    Err(_e) => Vec::new(),
                };
            for task_id in &reassigned {
                let _ = conexus_db::task_repository::update_fields(
                    &guard,
                    task_id,
                    &conexus_db::task_repository::TaskFields {
                        assigned_to:
                            conexus_db::scheduled_directive_repository::NullableUpdate::Clear,
                        status: Some("unassigned"),
                        ..Default::default()
                    },
                    now,
                );
            }

            // Found, documented (not silently reconciled): Python's DB
            // audit row for this action hardcodes the actor as
            // `"admin"` rather than the real caller's actor_label --
            // unlike every sibling lifecycle tool in this file, which
            // logs the real principal. Preserved as-is (this migration's
            // "re-derive documented behavior, don't smuggle in a fix"
            // discipline) since it's an audit-completeness quirk, not a
            // security-relevant one -- the capability gate above already
            // requires agents.terminate regardless of who is logged.
            let _ = agent_action_repository::log_agent_action(
                &guard,
                "admin",
                "terminated_agent",
                None,
                Some(&serde_json::json!({"agent_id": agent_id})),
                now,
            );
            drop(guard);

            // BL-R10-2: wake every worker so a live wait_for_events
            // waiter picks up the newly-unassigned task(s) immediately
            // -- same broadcast-to-every-active-agent pattern
            // create_task's own unassigned-task path already
            // establishes (task_tools.rs).
            if !reassigned.is_empty() {
                let guard = conn.lock().await;
                if let Ok(active) = AgentRepository::list_active(&guard) {
                    for agent in active {
                        ctx.waiter_registry.notify(&agent.agent_id);
                    }
                }
            }

            ToolResult::Ok {
                data: Some(serde_json::json!({"agent_id": agent_id, "status": "terminated"})),
                message: Some(format!(
                    "Agent '{agent_id}' terminated. The token is revoked, but your local \
                     claude session is still running — close it manually if you want it to \
                     stop."
                )),
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generate_token_is_32_lowercase_hex_chars() {
        let token = generate_token();
        assert_eq!(token.len(), 32);
        assert!(token
            .chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
    }

    #[test]
    fn generate_token_is_not_a_constant() {
        // Not a rigorous entropy test -- just a sanity check that two
        // calls don't return the same value (would catch an
        // accidentally-fixed seed / stubbed RNG).
        let a = generate_token();
        let b = generate_token();
        assert_ne!(a, b);
    }

    #[test]
    fn next_agent_color_wraps_around_the_palette() {
        assert_eq!(next_agent_color(0), AGENT_COLORS[0]);
        assert_eq!(next_agent_color(AGENT_COLORS.len()), AGENT_COLORS[0]);
        assert_eq!(next_agent_color(AGENT_COLORS.len() + 1), AGENT_COLORS[1]);
    }

    fn sample_row() -> conexus_db::agent_repository::AgentRow {
        conexus_db::agent_repository::AgentRow {
            token: "secret-token-value".to_string(),
            agent_id: "alice".to_string(),
            created_at: "2026-06-01T00:00:00Z".to_string(),
            status: "active".to_string(),
            current_task: None,
            working_directory: "/tmp".to_string(),
            color: Some("#FF5733".to_string()),
            terminated_at: None,
            updated_at: None,
            aoe_session_id: Some("secret-session-id".to_string()),
            auto_event_loop: true,
            last_event_seen_at: None,
            last_activity_at: None,
            agent_role: "worker".to_string(),
            profile: None,
            profile_updated_at: None,
            profile_reviewed_at: None,
            profile_updated_by: None,
        }
    }

    #[test]
    fn redact_agent_row_masks_both_secret_fields_for_a_non_operator() {
        let row = sample_row();
        let redacted = redact_agent_row(&row, false);
        assert_eq!(redacted["token"], REDACTED_TOKEN);
        assert_eq!(redacted["aoe_session_id"], REDACTED_TOKEN);
        // Non-secret fields pass through unchanged.
        assert_eq!(redacted["agent_id"], "alice");
        assert_eq!(redacted["color"], "#FF5733");
    }

    #[test]
    fn redact_agent_row_passes_through_unchanged_for_a_confirmed_operator() {
        let row = sample_row();
        let full = redact_agent_row(&row, true);
        assert_eq!(full["token"], "secret-token-value");
        assert_eq!(full["aoe_session_id"], "secret-session-id");
    }

    #[test]
    fn redact_agent_row_masks_a_null_aoe_session_id_key_too() {
        // Keys stay present (masked, not dropped) even when the
        // underlying value was already null -- matches Python's "key
        // stays present so a client can tell masked from absent".
        let mut row = sample_row();
        row.aoe_session_id = None;
        let redacted = redact_agent_row(&row, false);
        assert_eq!(redacted["aoe_session_id"], REDACTED_TOKEN);
    }

    #[test]
    fn every_agents_table_column_is_accounted_for_as_secret_or_safe() {
        // Real regression signal against DRIFT: if the `agents` table
        // ever gains a new column, this fails until it's explicitly
        // classified as secret or safe -- the Rust equivalent of
        // Python's "derive the secret set from the model" safety
        // property, checked against the real schema rather than a
        // hand-typed struct.
        const AGENT_SAFE_FIELDS: &[&str] = &[
            "agent_id",
            "created_at",
            "status",
            "current_task",
            "working_directory",
            "color",
            "terminated_at",
            "updated_at",
            "auto_event_loop",
            "last_event_seen_at",
            "last_activity_at",
            "agent_role",
            "profile",
            "profile_updated_at",
            "profile_reviewed_at",
            "profile_updated_by",
        ];
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        conexus_db::schema::init_schema(&conn).unwrap();
        let mut stmt = conn.prepare("PRAGMA table_info(agents)").unwrap();
        let columns: Vec<String> = stmt
            .query_map([], |row| row.get::<_, String>(1))
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        assert!(!columns.is_empty());
        for column in &columns {
            assert!(
                AGENT_SECRET_FIELDS.contains(&column.as_str())
                    || AGENT_SAFE_FIELDS.contains(&column.as_str()),
                "agents.{column} is not classified as secret or safe -- \
                 add it to AGENT_SECRET_FIELDS or AGENT_SAFE_FIELDS"
            );
        }
    }

    #[test]
    fn snippet_host_prefers_the_explicit_argument() {
        assert_eq!(
            resolve_snippet_host(Some("https://example.com/"), |_| None),
            "https://example.com"
        );
    }

    #[test]
    fn snippet_host_falls_back_to_env_then_the_placeholder() {
        assert_eq!(
            resolve_snippet_host(None, |k| if k == "AGENT_MCP_EXTERNAL_URL" {
                Some("https://from-env.example/".to_string())
            } else {
                None
            }),
            "https://from-env.example"
        );
        assert_eq!(
            resolve_snippet_host(None, |_| None),
            DEFAULT_REGISTER_AGENT_URL_BASE
        );
    }

    #[test]
    fn snippet_host_ignores_a_blank_argument() {
        assert_eq!(
            resolve_snippet_host(Some("   "), |_| Some(
                "https://from-env.example".to_string()
            )),
            "https://from-env.example"
        );
    }

    fn principal_with_project(project_name: Option<&str>) -> Principal {
        use conexus_core::capability::Capabilities;
        use conexus_core::principal::PrincipalKind;
        Principal {
            kind: PrincipalKind::ForwardingHeader,
            user_id: Some("op-1".to_string()),
            agent_id: None,
            project_name: project_name.map(str::to_string),
            project_role: None,
            agent_role: None,
            can_wake_loop: false,
            source_token: None,
            capabilities: Capabilities::Set(Default::default()),
        }
    }

    #[test]
    fn snippet_project_prefers_the_explicit_argument() {
        let p = principal_with_project(Some("router-project"));
        assert_eq!(
            resolve_snippet_project(Some("explicit-project"), Some(&p)),
            Some("explicit-project".to_string())
        );
    }

    #[test]
    fn snippet_project_falls_back_to_the_principal_then_none() {
        let p = principal_with_project(Some("router-project"));
        assert_eq!(
            resolve_snippet_project(None, Some(&p)),
            Some("router-project".to_string())
        );
        assert_eq!(resolve_snippet_project(None, None), None);
        let p_no_project = principal_with_project(None);
        assert_eq!(resolve_snippet_project(None, Some(&p_no_project)), None);
    }

    #[test]
    fn mcp_config_snippet_includes_the_project_segment_when_present() {
        let snippet = build_mcp_config_snippet(
            Some("demo"),
            "tok-123",
            "https://host.example",
            "/agent-mcp",
        );
        let parsed: Value = serde_json::from_str(&snippet).unwrap();
        assert_eq!(
            parsed["mcpServers"]["agent-mcp"]["url"],
            "https://host.example/agent-mcp/mcp/demo"
        );
        assert_eq!(
            parsed["mcpServers"]["agent-mcp"]["headers"]["Authorization"],
            "Bearer tok-123"
        );
    }

    #[test]
    fn mcp_config_snippet_drops_the_project_segment_when_absent() {
        let snippet =
            build_mcp_config_snippet(None, "tok-123", "https://host.example", "/agent-mcp");
        let parsed: Value = serde_json::from_str(&snippet).unwrap();
        assert_eq!(
            parsed["mcpServers"]["agent-mcp"]["url"],
            "https://host.example/mcp"
        );
    }

    // ── ViewAuditLogTool ─────────────────────────────────────────────

    use conexus_core::capability::Capabilities;
    use conexus_core::principal::PrincipalKind;
    use conexus_db::schema::init_schema;
    use conexus_wakeloop::file_map::FileMap;
    use conexus_wakeloop::waiter_registry::WaiterRegistry;
    use std::collections::HashSet;

    fn operator() -> Principal {
        Principal {
            kind: PrincipalKind::ForwardingHeader,
            user_id: Some("op-1".to_string()),
            agent_id: None,
            project_name: None,
            project_role: None,
            agent_role: None,
            can_wake_loop: false,
            source_token: None,
            capabilities: Capabilities::Set(HashSet::from([Capability::SystemConfigWrite])),
        }
    }

    fn worker() -> Principal {
        use conexus_core::capability::AgentRole;
        Principal {
            kind: PrincipalKind::AgentBearer,
            user_id: None,
            agent_id: Some("alice".to_string()),
            project_name: None,
            project_role: None,
            agent_role: Some(AgentRole::Worker),
            can_wake_loop: true,
            source_token: None,
            capabilities: Capabilities::Set(HashSet::new()),
        }
    }

    async fn setup() -> AsyncMutex<Connection> {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        AsyncMutex::new(conn)
    }

    fn ctx<'a>(
        registry: &'a WaiterRegistry,
        file_map: &'a FileMap,
    ) -> conexus_auth::ToolCallContext<'a> {
        conexus_auth::ToolCallContext::off_wire(registry, file_map, std::path::Path::new("/tmp"))
    }

    async fn seed(conn: &AsyncMutex<Connection>, agent_id: &str, action_type: &str, ts: &str) {
        let guard = conn.lock().await;
        agent_action_repository::log_agent_action(&guard, agent_id, action_type, None, None, ts)
            .unwrap();
    }

    #[tokio::test]
    async fn view_audit_log_denies_a_plain_worker() {
        let alice = worker();
        let denied =
            ViewAuditLogTool::REQUIRED.check(Some(&alice), &conexus_auth::NoPolicyOverrides);
        assert!(denied.is_err());
    }

    #[tokio::test]
    async fn view_audit_log_returns_recent_entries_for_an_operator() {
        let conn = setup().await;
        seed(&conn, "alice", "created_task", "2026-06-01T00:00:00Z").await;
        seed(&conn, "bob", "deleted_task", "2026-06-01T00:00:01Z").await;
        let op = operator();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = ViewAuditLogTool::call(
            Some(&op),
            &serde_json::json!({}),
            &conn,
            "2026-06-01T00:00:02Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, message } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let data = data.unwrap();
        assert_eq!(data["count"], 2);
        assert_eq!(data["entries"][0]["action"], "created_task");
        assert_eq!(data["entries"][1]["action"], "deleted_task");
        assert!(message.unwrap().contains("2 entries displayed"));
    }

    #[tokio::test]
    async fn view_audit_log_filters_by_agent_id_and_action() {
        let conn = setup().await;
        seed(&conn, "alice", "created_task", "2026-06-01T00:00:00Z").await;
        seed(&conn, "alice", "deleted_task", "2026-06-01T00:00:01Z").await;
        seed(&conn, "bob", "deleted_task", "2026-06-01T00:00:02Z").await;
        let op = operator();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = ViewAuditLogTool::call(
            Some(&op),
            &serde_json::json!({"agent_id": "alice", "action": "deleted_task"}),
            &conn,
            "2026-06-01T00:00:03Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert_eq!(data.unwrap()["count"], 1);
    }

    #[test]
    fn clamp_audit_log_limit_falls_back_to_50_on_anything_out_of_range() {
        assert_eq!(clamp_audit_log_limit(&serde_json::json!({})), 50);
        assert_eq!(clamp_audit_log_limit(&serde_json::json!({"limit": 10})), 10);
        assert_eq!(clamp_audit_log_limit(&serde_json::json!({"limit": 0})), 50);
        assert_eq!(
            clamp_audit_log_limit(&serde_json::json!({"limit": 201})),
            50
        );
        assert_eq!(
            clamp_audit_log_limit(&serde_json::json!({"limit": "not-a-number"})),
            50
        );
    }

    #[tokio::test]
    async fn view_audit_log_on_an_empty_table_reports_zero_entries() {
        let conn = setup().await;
        let op = operator();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = ViewAuditLogTool::call(
            Some(&op),
            &serde_json::json!({}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert_eq!(data.unwrap()["count"], 0);
    }

    // ── GetAgentTokensTool ───────────────────────────────────────────

    use conexus_core::capability::ProjectRole;

    /// Passes the Cap(agents.register) gate AND is confirmed operator
    /// tier (project_role Operator) -- the "real admin" case.
    fn confirmed_operator() -> Principal {
        Principal {
            kind: PrincipalKind::ForwardingHeader,
            user_id: Some("op-1".to_string()),
            agent_id: None,
            project_name: None,
            project_role: Some(ProjectRole::Operator),
            agent_role: None,
            can_wake_loop: false,
            source_token: None,
            capabilities: Capabilities::Set(HashSet::from([Capability::AgentsRegister])),
        }
    }

    /// Passes the Cap(agents.register) gate (e.g. via a group grant)
    /// but is NOT confirmed operator tier (project_role Viewer, no
    /// Sysadmin) -- Finding 2's second, defense-in-depth layer: this
    /// principal must still be masked even with include_sensitive_data
    /// set, since the coarse Cap gate alone isn't sufficient proof of
    /// operator tier.
    fn cap_only_non_confirmed() -> Principal {
        Principal {
            kind: PrincipalKind::ForwardingHeader,
            user_id: Some("op-2".to_string()),
            agent_id: None,
            project_name: None,
            project_role: Some(ProjectRole::Viewer),
            agent_role: None,
            can_wake_loop: false,
            source_token: None,
            capabilities: Capabilities::Set(HashSet::from([Capability::AgentsRegister])),
        }
    }

    async fn seed_agent(
        conn: &AsyncMutex<Connection>,
        agent_id: &str,
        token: &str,
        created_at: &str,
    ) {
        let guard = conn.lock().await;
        conexus_db::agent_repository::AgentRepository::create(
            &guard,
            conexus_db::agent_repository::NewAgent {
                token,
                agent_id,
                created_at,
                status: "active",
                current_task: None,
                working_directory: "/tmp",
                color: None,
                agent_role: "worker",
            },
        )
        .unwrap();
    }

    #[tokio::test]
    async fn get_agent_tokens_denies_a_plain_worker() {
        let alice = worker();
        let denied =
            GetAgentTokensTool::REQUIRED.check(Some(&alice), &conexus_auth::NoPolicyOverrides);
        assert!(denied.is_err());
    }

    #[tokio::test]
    async fn get_agent_tokens_masks_tokens_by_default_even_for_a_confirmed_operator() {
        let conn = setup().await;
        seed_agent(&conn, "alice", "tok-alice-secret", "2026-06-01T00:00:00Z").await;
        let op = confirmed_operator();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = GetAgentTokensTool::call(
            Some(&op),
            &serde_json::json!({}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let data = data.unwrap();
        assert_eq!(data["agents"][0]["token"], REDACTED_TOKEN);
        assert_eq!(data["filters_applied"]["include_sensitive_data"], false);
    }

    #[tokio::test]
    async fn get_agent_tokens_exposes_tokens_only_for_a_confirmed_operator_who_opts_in() {
        let conn = setup().await;
        seed_agent(&conn, "alice", "tok-alice-secret", "2026-06-01T00:00:00Z").await;
        let op = confirmed_operator();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = GetAgentTokensTool::call(
            Some(&op),
            &serde_json::json!({"include_sensitive_data": true}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let data = data.unwrap();
        assert_eq!(data["agents"][0]["token"], "tok-alice-secret");
        assert_eq!(data["filters_applied"]["include_sensitive_data"], true);
    }

    #[tokio::test]
    async fn get_agent_tokens_masks_a_cap_only_non_confirmed_caller_even_with_opt_in() {
        // Finding 2's second layer: passing the coarse Cap gate is not
        // enough on its own -- confirmed operator tier is required too.
        let conn = setup().await;
        seed_agent(&conn, "alice", "tok-alice-secret", "2026-06-01T00:00:00Z").await;
        let op = cap_only_non_confirmed();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = GetAgentTokensTool::call(
            Some(&op),
            &serde_json::json!({"include_sensitive_data": true}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert_eq!(data.unwrap()["agents"][0]["token"], REDACTED_TOKEN);
    }

    #[tokio::test]
    async fn get_agent_tokens_pagination_reports_has_more() {
        let conn = setup().await;
        seed_agent(&conn, "alice", "tok-a", "2026-06-01T00:00:00Z").await;
        seed_agent(&conn, "bob", "tok-b", "2026-06-01T00:00:01Z").await;
        seed_agent(&conn, "carol", "tok-c", "2026-06-01T00:00:02Z").await;
        let op = confirmed_operator();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = GetAgentTokensTool::call(
            Some(&op),
            &serde_json::json!({"limit": 2}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let data = data.unwrap();
        assert_eq!(data["pagination"]["total_count"], 3);
        assert_eq!(data["pagination"]["returned_count"], 2);
        assert_eq!(data["pagination"]["has_more"], true);
    }

    #[tokio::test]
    async fn get_agent_tokens_filters_by_status() {
        let conn = setup().await;
        seed_agent(&conn, "alice", "tok-a", "2026-06-01T00:00:00Z").await;
        {
            let guard = conn.lock().await;
            conexus_db::agent_repository::AgentRepository::terminate(
                &guard,
                "alice",
                "2026-06-01T00:00:01Z",
            )
            .unwrap();
        }
        let op = confirmed_operator();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = GetAgentTokensTool::call(
            Some(&op),
            // include_terminated must ALSO be true -- Python's own
            // repository doc: "when False, excludes status='terminated'
            // rows" regardless of an explicit filter_status value.
            &serde_json::json!({"filter_status": "terminated", "include_terminated": true}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert_eq!(data.unwrap()["pagination"]["total_count"], 1);
    }

    #[test]
    fn effective_agent_sort_by_falls_back_to_created_at_matching_the_repositorys_own_allowlist() {
        // A real, preserved Python inconsistency: "updated_at" passes
        // the tool's OWN (wider) pre-validation in Python but the
        // repository's real allowlist doesn't include it, so it
        // silently becomes created_at either way -- pin the Rust port
        // reproduces the REPOSITORY's real behavior, not the wider
        // tool-layer allowlist.
        assert_eq!(effective_agent_sort_by("updated_at"), "created_at");
        assert_eq!(effective_agent_sort_by("agent_id"), "agent_id");
        assert_eq!(effective_agent_sort_by("garbage"), "created_at");
    }

    #[test]
    fn effective_sort_order_only_asc_is_case_insensitively_recognized() {
        assert_eq!(effective_sort_order("asc"), "ASC");
        assert_eq!(effective_sort_order("ASC"), "ASC");
        assert_eq!(effective_sort_order("desc"), "DESC");
        assert_eq!(effective_sort_order("garbage"), "DESC");
    }

    #[tokio::test]
    async fn get_agent_tokens_writes_a_durable_audit_row() {
        let conn = setup().await;
        seed_agent(&conn, "alice", "tok-a", "2026-06-01T00:00:00Z").await;
        let op = confirmed_operator();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = GetAgentTokensTool::call(
            Some(&op),
            &serde_json::json!({}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        let count: i64 = guard
            .query_row(
                "SELECT COUNT(*) FROM agent_actions WHERE action_type = 'get_agent_tokens'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }

    // ── ViewStatusTool ───────────────────────────────────────────────

    #[tokio::test]
    async fn view_status_denies_a_plain_worker() {
        let alice = worker();
        let denied = ViewStatusTool::REQUIRED.check(Some(&alice), &conexus_auth::NoPolicyOverrides);
        assert!(denied.is_err());
    }

    #[tokio::test]
    async fn view_status_reports_live_agents_and_excludes_the_system_pseudo_agent() {
        let conn = setup().await;
        seed_agent(&conn, "alice", "tok-a", "2026-06-01T00:00:00Z").await;
        {
            let guard = conn.lock().await;
            conexus_db::agent_repository::AgentRepository::create(
                &guard,
                conexus_db::agent_repository::NewAgent {
                    token: "tok-system",
                    agent_id: "system",
                    created_at: "2026-06-01T00:00:00Z",
                    status: "system",
                    current_task: None,
                    working_directory: "/tmp",
                    color: None,
                    agent_role: "worker",
                },
            )
            .unwrap();
        }
        let op = operator();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = ViewStatusTool::call(
            Some(&op),
            &serde_json::json!({}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, message } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let data = data.unwrap();
        assert_eq!(data["active_agents_count"], 1);
        assert!(data["agents_details"].get("alice").is_some());
        assert!(data["agents_details"].get("system").is_none());
        assert!(message.unwrap().contains("MCP Server Status"));
    }

    #[tokio::test]
    async fn view_status_excludes_a_terminated_agent() {
        let conn = setup().await;
        seed_agent(&conn, "alice", "tok-a", "2026-06-01T00:00:00Z").await;
        {
            let guard = conn.lock().await;
            conexus_db::agent_repository::AgentRepository::terminate(
                &guard,
                "alice",
                "2026-06-01T00:00:01Z",
            )
            .unwrap();
        }
        let op = operator();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = ViewStatusTool::call(
            Some(&op),
            &serde_json::json!({}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert_eq!(data.unwrap()["active_agents_count"], 0);
    }

    #[tokio::test]
    async fn view_status_reports_the_file_map_size_and_preview() {
        let conn = setup().await;
        let op = operator();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        file_map.claim("/tmp/a.txt", "alice", "editing", "2026-06-01T00:00:00Z");
        let c = ctx(&registry, &file_map);
        let result = ViewStatusTool::call(
            Some(&op),
            &serde_json::json!({}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let data = data.unwrap();
        assert_eq!(data["file_map_size"], 1);
        assert!(data["file_map_preview"].get("/tmp/a.txt").is_some());
    }

    // ── RegisterAgentTool ────────────────────────────────────────────

    #[test]
    fn contains_reserved_bracket_matches_either_bracket() {
        assert!(contains_reserved_bracket("deleted[1]"));
        assert!(contains_reserved_bracket("a[b"));
        assert!(contains_reserved_bracket("a]b"));
        assert!(!contains_reserved_bracket("alice"));
    }

    #[test]
    fn is_reserved_agent_id_is_case_insensitive() {
        assert!(is_reserved_agent_id("admin"));
        assert!(is_reserved_agent_id("Admin-bob"));
        assert!(is_reserved_agent_id("ADMINISTRATOR"));
        assert!(!is_reserved_agent_id("alice"));
    }

    #[tokio::test]
    async fn register_agent_denies_a_plain_worker() {
        let alice = worker();
        let denied =
            RegisterAgentTool::REQUIRED.check(Some(&alice), &conexus_auth::NoPolicyOverrides);
        assert!(denied.is_err());
    }

    #[tokio::test]
    async fn register_agent_creates_a_worker_with_default_role() {
        let conn = setup().await;
        let op = confirmed_operator();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = RegisterAgentTool::call(
            Some(&op),
            &serde_json::json!({"name": "alice"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, message } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let data = data.unwrap();
        assert_eq!(data["agent_id"], "alice");
        assert_eq!(data["agent_role"], "worker");
        let token = data["token"].as_str().unwrap().to_string();
        assert_eq!(token.len(), 32);
        assert!(data["mcp_snippet"].as_str().unwrap().contains(&token));
        assert!(message.unwrap().contains("registered"));

        let guard = conn.lock().await;
        let row = conexus_db::agent_repository::AgentRepository::get_by_id(&guard, "alice")
            .unwrap()
            .unwrap();
        assert_eq!(row.status, "created");
        assert_eq!(row.profile, None);
    }

    #[tokio::test]
    async fn register_agent_seeds_the_manager_default_profile() {
        let conn = setup().await;
        let op = confirmed_operator();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = RegisterAgentTool::call(
            Some(&op),
            &serde_json::json!({"name": "boss", "role": "manager"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        let row = conexus_db::agent_repository::AgentRepository::get_by_id(&guard, "boss")
            .unwrap()
            .unwrap();
        assert_eq!(row.profile.as_deref(), Some(MANAGER_DEFAULT_PROFILE));
        assert_eq!(row.profile_updated_by, None);
    }

    #[tokio::test]
    async fn register_agent_rejects_an_invalid_role() {
        let conn = setup().await;
        let op = confirmed_operator();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = RegisterAgentTool::call(
            Some(&op),
            &serde_json::json!({"name": "alice", "role": "superadmin"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Invalid { .. }));
    }

    #[tokio::test]
    async fn register_agent_rejects_a_bracketed_name() {
        let conn = setup().await;
        let op = confirmed_operator();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = RegisterAgentTool::call(
            Some(&op),
            &serde_json::json!({"name": "deleted-alice]"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Invalid { .. }));
    }

    #[tokio::test]
    async fn register_agent_rejects_a_reserved_admin_prefixed_name() {
        let conn = setup().await;
        let op = confirmed_operator();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = RegisterAgentTool::call(
            Some(&op),
            &serde_json::json!({"name": "admin-bob"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Invalid { .. }));
    }

    #[tokio::test]
    async fn register_agent_duplicate_name_is_a_conflict() {
        let conn = setup().await;
        seed_agent(&conn, "alice", "tok-a", "2026-06-01T00:00:00Z").await;
        let op = confirmed_operator();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = RegisterAgentTool::call(
            Some(&op),
            &serde_json::json!({"name": "alice"}),
            &conn,
            "2026-06-01T00:00:01Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Conflict { .. }));
    }

    #[tokio::test]
    async fn register_agent_snippet_includes_the_project_and_host() {
        let conn = setup().await;
        let op = confirmed_operator();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = RegisterAgentTool::call(
            Some(&op),
            &serde_json::json!({
                "name": "alice",
                "project_name": "demo",
                "host": "https://host.example",
            }),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let data = data.unwrap();
        assert_eq!(data["project_name"], "demo");
        let snippet: Value = serde_json::from_str(data["mcp_snippet"].as_str().unwrap()).unwrap();
        assert_eq!(
            snippet["mcpServers"]["agent-mcp"]["url"],
            "https://host.example/agent-mcp/mcp/demo"
        );
    }

    #[tokio::test]
    async fn register_agent_writes_a_durable_audit_row() {
        let conn = setup().await;
        let op = confirmed_operator();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = RegisterAgentTool::call(
            Some(&op),
            &serde_json::json!({"name": "alice"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        let count: i64 = guard
            .query_row(
                "SELECT COUNT(*) FROM agent_actions WHERE action_type = 'registered_agent'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }

    // ── RotateAgentTokenTool / RestoreAgentTool / EditAgentTool ───────

    fn operator_with(caps: &[Capability]) -> Principal {
        Principal {
            kind: PrincipalKind::ForwardingHeader,
            user_id: Some("op-1".to_string()),
            agent_id: None,
            project_name: None,
            project_role: Some(ProjectRole::Operator),
            agent_role: None,
            can_wake_loop: false,
            source_token: None,
            capabilities: Capabilities::Set(caps.iter().copied().collect()),
        }
    }

    #[tokio::test]
    async fn rotate_agent_token_denies_a_plain_worker() {
        let alice = worker();
        let denied =
            RotateAgentTokenTool::REQUIRED.check(Some(&alice), &conexus_auth::NoPolicyOverrides);
        assert!(denied.is_err());
    }

    #[tokio::test]
    async fn rotate_agent_token_replaces_the_bearer_and_returns_it_once() {
        let conn = setup().await;
        seed_agent(&conn, "alice", "old-token-value", "2026-06-01T00:00:00Z").await;
        let op = operator_with(&[Capability::AgentsRotateToken]);
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = RotateAgentTokenTool::call(
            Some(&op),
            &serde_json::json!({"agent_id": "alice"}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let new_token = data.unwrap()["token"].as_str().unwrap().to_string();
        assert_ne!(new_token, "old-token-value");
        assert_eq!(new_token.len(), 32);
        let guard = conn.lock().await;
        let row = conexus_db::agent_repository::AgentRepository::get_by_id(&guard, "alice")
            .unwrap()
            .unwrap();
        assert_eq!(row.token, new_token);
    }

    #[tokio::test]
    async fn rotate_agent_token_missing_agent_is_not_found() {
        let conn = setup().await;
        let op = operator_with(&[Capability::AgentsRotateToken]);
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = RotateAgentTokenTool::call(
            Some(&op),
            &serde_json::json!({"agent_id": "ghost"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::NotFound { .. }));
    }

    #[tokio::test]
    async fn rotate_agent_token_refuses_a_terminated_agent() {
        let conn = setup().await;
        seed_agent(&conn, "alice", "tok-a", "2026-06-01T00:00:00Z").await;
        {
            let guard = conn.lock().await;
            conexus_db::agent_repository::AgentRepository::terminate(
                &guard,
                "alice",
                "2026-06-01T00:00:01Z",
            )
            .unwrap();
        }
        let op = operator_with(&[Capability::AgentsRotateToken]);
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = RotateAgentTokenTool::call(
            Some(&op),
            &serde_json::json!({"agent_id": "alice"}),
            &conn,
            "2026-06-01T00:02:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Conflict { .. }));
    }

    #[tokio::test]
    async fn rotate_agent_token_writes_a_durable_audit_row_with_suffixes_only() {
        let conn = setup().await;
        seed_agent(&conn, "alice", "old-token-value", "2026-06-01T00:00:00Z").await;
        let op = operator_with(&[Capability::AgentsRotateToken]);
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = RotateAgentTokenTool::call(
            Some(&op),
            &serde_json::json!({"agent_id": "alice"}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        let details: String = guard
            .query_row(
                "SELECT details FROM agent_actions WHERE action_type = 'rotated_agent_token'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert!(!details.contains("old-token-value"));
        assert!(details.contains("alue")); // the 4-char suffix of the old token
    }

    #[tokio::test]
    async fn restore_agent_denies_a_plain_worker() {
        let alice = worker();
        let denied =
            RestoreAgentTool::REQUIRED.check(Some(&alice), &conexus_auth::NoPolicyOverrides);
        assert!(denied.is_err());
    }

    #[tokio::test]
    async fn restore_agent_flips_a_terminated_agent_back_to_created() {
        let conn = setup().await;
        seed_agent(&conn, "alice", "tok-a", "2026-06-01T00:00:00Z").await;
        {
            let guard = conn.lock().await;
            conexus_db::agent_repository::AgentRepository::terminate(
                &guard,
                "alice",
                "2026-06-01T00:00:01Z",
            )
            .unwrap();
        }
        let op = operator_with(&[Capability::AgentsTerminate]);
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = RestoreAgentTool::call(
            Some(&op),
            &serde_json::json!({"agent_id": "alice"}),
            &conn,
            "2026-06-01T00:02:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        let row = conexus_db::agent_repository::AgentRepository::get_by_id(&guard, "alice")
            .unwrap()
            .unwrap();
        assert_eq!(row.status, "created");
        assert_eq!(row.terminated_at, None);
    }

    #[tokio::test]
    async fn restore_agent_refuses_a_non_terminated_agent() {
        let conn = setup().await;
        seed_agent(&conn, "alice", "tok-a", "2026-06-01T00:00:00Z").await;
        let op = operator_with(&[Capability::AgentsTerminate]);
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = RestoreAgentTool::call(
            Some(&op),
            &serde_json::json!({"agent_id": "alice"}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Conflict { .. }));
    }

    #[tokio::test]
    async fn restore_agent_missing_agent_is_not_found() {
        let conn = setup().await;
        let op = operator_with(&[Capability::AgentsTerminate]);
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = RestoreAgentTool::call(
            Some(&op),
            &serde_json::json!({"agent_id": "ghost"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::NotFound { .. }));
    }

    #[tokio::test]
    async fn edit_agent_denies_a_plain_worker() {
        let alice = worker();
        let denied = EditAgentTool::REQUIRED.check(Some(&alice), &conexus_auth::NoPolicyOverrides);
        assert!(denied.is_err());
    }

    #[tokio::test]
    async fn edit_agent_updates_the_color() {
        let conn = setup().await;
        seed_agent(&conn, "alice", "tok-a", "2026-06-01T00:00:00Z").await;
        let op = operator_with(&[Capability::AgentsTerminate]);
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = EditAgentTool::call(
            Some(&op),
            &serde_json::json!({"agent_id": "alice", "color": "#123456"}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert_eq!(data.unwrap()["updated"], serde_json::json!(["color"]));
        let guard = conn.lock().await;
        let row = conexus_db::agent_repository::AgentRepository::get_by_id(&guard, "alice")
            .unwrap()
            .unwrap();
        assert_eq!(row.color.as_deref(), Some("#123456"));
    }

    #[tokio::test]
    async fn edit_agent_no_editable_fields_is_invalid() {
        let conn = setup().await;
        seed_agent(&conn, "alice", "tok-a", "2026-06-01T00:00:00Z").await;
        let op = operator_with(&[Capability::AgentsTerminate]);
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = EditAgentTool::call(
            Some(&op),
            &serde_json::json!({"agent_id": "alice"}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Invalid { .. }));
    }

    #[tokio::test]
    async fn edit_agent_rejects_an_invalid_agent_role() {
        let conn = setup().await;
        seed_agent(&conn, "alice", "tok-a", "2026-06-01T00:00:00Z").await;
        let op = operator_with(&[Capability::AgentsTerminate]);
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = EditAgentTool::call(
            Some(&op),
            &serde_json::json!({"agent_id": "alice", "agent_role": "overlord"}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Invalid { .. }));
    }

    #[tokio::test]
    async fn edit_agent_clears_aoe_session_id_with_an_empty_string() {
        let conn = setup().await;
        seed_agent(&conn, "alice", "tok-a", "2026-06-01T00:00:00Z").await;
        {
            let guard = conn.lock().await;
            conexus_db::agent_repository::AgentRepository::update_field(
                &guard,
                "alice",
                conexus_db::agent_repository::AgentField::AoeSessionId,
                conexus_db::agent_repository::FieldValue::OptionalText(Some(
                    "old-session".to_string(),
                )),
                "2026-06-01T00:00:01Z",
            )
            .unwrap();
        }
        let op = operator_with(&[Capability::AgentsTerminate]);
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = EditAgentTool::call(
            Some(&op),
            &serde_json::json!({"agent_id": "alice", "aoe_session_id": ""}),
            &conn,
            "2026-06-01T00:02:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        let row = conexus_db::agent_repository::AgentRepository::get_by_id(&guard, "alice")
            .unwrap()
            .unwrap();
        assert_eq!(row.aoe_session_id, None);
    }

    #[tokio::test]
    async fn edit_agent_toggling_auto_event_loop_wakes_the_agents_waiter() {
        let conn = setup().await;
        seed_agent(&conn, "alice", "tok-a", "2026-06-01T00:00:00Z").await;
        let op = operator_with(&[Capability::AgentsTerminate]);
        let registry = WaiterRegistry::new();
        let (_sender, mut receiver) = registry.register("alice");
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = EditAgentTool::call(
            Some(&op),
            &serde_json::json!({"agent_id": "alice", "auto_event_loop": false}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        assert!(receiver.try_recv().is_ok());
    }

    #[tokio::test]
    async fn edit_agent_missing_agent_is_not_found() {
        let conn = setup().await;
        let op = operator_with(&[Capability::AgentsTerminate]);
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = EditAgentTool::call(
            Some(&op),
            &serde_json::json!({"agent_id": "ghost", "color": "#000000"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::NotFound { .. }));
    }

    // ── TerminateAgentTool ───────────────────────────────────────────

    async fn seed_task(
        conn: &AsyncMutex<Connection>,
        task_id: &str,
        assigned_to: Option<&str>,
        status: &str,
        created_at: &str,
    ) {
        seed_task_with_parent(conn, task_id, None, assigned_to, status, created_at).await;
    }

    async fn seed_task_with_parent(
        conn: &AsyncMutex<Connection>,
        task_id: &str,
        parent_task: Option<&str>,
        assigned_to: Option<&str>,
        status: &str,
        created_at: &str,
    ) {
        let guard = conn.lock().await;
        conexus_db::task_repository::create(
            &guard,
            conexus_db::task_repository::NewTask {
                task_id: Some(task_id),
                title: "a task",
                description: None,
                assigned_to,
                created_by: "alice",
                status,
                priority: "medium",
                parent_task,
                child_tasks: None,
                depends_on_tasks: None,
                notes: None,
                now: created_at,
            },
        )
        .unwrap();
    }

    #[tokio::test]
    async fn terminate_agent_denies_a_plain_worker() {
        let alice = worker();
        let denied =
            TerminateAgentTool::REQUIRED.check(Some(&alice), &conexus_auth::NoPolicyOverrides);
        assert!(denied.is_err());
    }

    #[tokio::test]
    async fn terminate_agent_flips_status_and_revokes_the_token() {
        let conn = setup().await;
        seed_agent(&conn, "alice", "tok-a", "2026-06-01T00:00:00Z").await;
        let op = operator_with(&[Capability::AgentsTerminate]);
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = TerminateAgentTool::call(
            Some(&op),
            &serde_json::json!({"agent_id": "alice"}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        let row = conexus_db::agent_repository::AgentRepository::get_by_id(&guard, "alice")
            .unwrap()
            .unwrap();
        assert_eq!(row.status, "terminated");
        assert!(row.terminated_at.is_some());
    }

    #[tokio::test]
    async fn terminate_agent_missing_agent_is_not_found() {
        let conn = setup().await;
        let op = operator_with(&[Capability::AgentsTerminate]);
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = TerminateAgentTool::call(
            Some(&op),
            &serde_json::json!({"agent_id": "ghost"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::NotFound { .. }));
    }

    #[tokio::test]
    async fn terminate_agent_on_an_already_terminated_agent_is_not_found_not_conflict() {
        // Matches Python's real combined outcome: no separate Conflict
        // branch exists for terminate (unlike rotate_agent_token).
        let conn = setup().await;
        seed_agent(&conn, "alice", "tok-a", "2026-06-01T00:00:00Z").await;
        {
            let guard = conn.lock().await;
            conexus_db::agent_repository::AgentRepository::terminate(
                &guard,
                "alice",
                "2026-06-01T00:00:01Z",
            )
            .unwrap();
        }
        let op = operator_with(&[Capability::AgentsTerminate]);
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = TerminateAgentTool::call(
            Some(&op),
            &serde_json::json!({"agent_id": "alice"}),
            &conn,
            "2026-06-01T00:02:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::NotFound { .. }));
    }

    #[tokio::test]
    async fn terminate_agent_reassigns_active_tasks_but_preserves_terminal_ones() {
        let conn = setup().await;
        seed_agent(&conn, "alice", "tok-a", "2026-06-01T00:00:00Z").await;
        seed_task(
            &conn,
            "task-active",
            Some("alice"),
            "in_progress",
            "2026-06-01T00:00:00Z",
        )
        .await;
        seed_task_with_parent(
            &conn,
            "task-done",
            Some("task-active"),
            Some("alice"),
            "completed",
            "2026-06-01T00:00:00Z",
        )
        .await;
        let op = operator_with(&[Capability::AgentsTerminate]);
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = TerminateAgentTool::call(
            Some(&op),
            &serde_json::json!({"agent_id": "alice"}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        let active = conexus_db::task_repository::get_by_id(&guard, "task-active")
            .unwrap()
            .unwrap();
        assert_eq!(active.status, "unassigned");
        assert_eq!(active.assigned_to, None);
        let done = conexus_db::task_repository::get_by_id(&guard, "task-done")
            .unwrap()
            .unwrap();
        assert_eq!(done.status, "completed");
        assert_eq!(done.assigned_to.as_deref(), Some("alice"));
    }

    #[tokio::test]
    async fn terminate_agent_wakes_every_active_agent_when_a_task_is_reassigned() {
        let conn = setup().await;
        seed_agent(&conn, "alice", "tok-a", "2026-06-01T00:00:00Z").await;
        seed_agent(&conn, "bob", "tok-b", "2026-06-01T00:00:00Z").await;
        seed_task(
            &conn,
            "task-active",
            Some("alice"),
            "in_progress",
            "2026-06-01T00:00:00Z",
        )
        .await;
        let op = operator_with(&[Capability::AgentsTerminate]);
        let registry = WaiterRegistry::new();
        let (_sender, mut receiver) = registry.register("bob");
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = TerminateAgentTool::call(
            Some(&op),
            &serde_json::json!({"agent_id": "alice"}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        assert!(receiver.try_recv().is_ok());
    }

    #[tokio::test]
    async fn terminate_agent_writes_a_durable_audit_row_attributed_to_admin() {
        let conn = setup().await;
        seed_agent(&conn, "alice", "tok-a", "2026-06-01T00:00:00Z").await;
        let op = operator_with(&[Capability::AgentsTerminate]);
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = TerminateAgentTool::call(
            Some(&op),
            &serde_json::json!({"agent_id": "alice"}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        let row: (String, String) = guard
            .query_row(
                "SELECT agent_id, action_type FROM agent_actions WHERE action_type = 'terminated_agent'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(row.0, "admin");
        assert_eq!(row.1, "terminated_agent");
    }
}
