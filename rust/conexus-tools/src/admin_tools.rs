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
//! ## PR1 (this file's initial content): pure helpers + new primitives
//!
//! No tools registered yet -- matches this migration's own "PR1 pure
//! helpers, no tools" precedent (`project_context_tools.rs`,
//! `task_tools.rs`). Every item `pub` (not `pub(crate)`), matching
//! `wake_notify.rs`'s precedent for a helpers-ahead-of-their-first-
//! consumer module (avoids a dead-code lint failure before PR2+ wire
//! a real caller).

use conexus_core::principal::Principal;
use serde_json::Value;

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
}
