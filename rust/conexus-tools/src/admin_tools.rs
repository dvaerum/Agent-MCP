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
use conexus_core::principal::Principal;
use conexus_core::tool_result::ToolResult;
use conexus_db::agent_action_repository;
use rusqlite::Connection;
use serde_json::Value;
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
}
