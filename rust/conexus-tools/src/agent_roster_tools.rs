//! Port of `agent_mcp/tools/agent_roster_tools.py` (Phase D5, PR 3) —
//! the single `view_agents` peer-roster tool. Any authenticated agent
//! (worker/manager) or operator tier can list every live agent's
//! public identity + self-authored profile, answering "who do I
//! ask?" (feeds `request_assistance`). Deliberately narrow projection
//! — `{agent_id, agent_role, profile, profile_updated_at}`, no token,
//! no working directory, no secrets.
//!
//! `Requirement::Predicate` rather than `Cap`: the admission rule is
//! an OR of two capabilities from DIFFERENT bundle families
//! (`agents.use`, granted only to agent-bearers; `agents.view`,
//! granted only to operator tiers) — no single `Cap` gate spans both,
//! and collapsing to either alone would narrow who's admitted, per
//! Python's own Finding-A rationale (see `_principal_can_view_roster`'s
//! docstring).
//!
//! `list_active` (Phase B) already excludes terminated/tombstone rows;
//! this tool additionally excludes `"system"` status (the synthetic
//! system pseudo-agent, not a real peer) — matching Python's
//! `_ROSTER_EXCLUDED_STATUSES`'s extra third entry over and above
//! `list_active`'s own two-status filter.
//!
//! Deliberately NOT ported, with an explicit reason (never a silent
//! drop): `utils/audit_utils.log_audit`'s in-memory `g.audit_log` /
//! file trail — same precedent as every prior Phase D5 tool (no Rust
//! reader for it yet, and no durable `agent_actions` row exists here
//! either since Python's own call writes only to the transient trail).

use conexus_auth::{Requirement, Tool};
use conexus_core::capability::Capability;
use conexus_core::principal::Principal;
use conexus_core::tool_result::ToolResult;
use conexus_db::agent_repository::AgentRepository;
use rusqlite::Connection;
use serde_json::Value;
use tokio::sync::Mutex as AsyncMutex;

const ROSTER_EXCLUDED_STATUSES: &[&str] = &["system"];

const ROSTER_DENIED: &str =
    "Unauthorized: An authenticated agent or operator is required to view the agent roster.";

fn principal_can_view_roster(principal: Option<&Principal>) -> bool {
    principal.is_some_and(|p| {
        p.has_capability(Capability::AgentsUse) || p.has_capability(Capability::AgentsView)
    })
}

pub struct ViewAgentsTool;

impl Tool for ViewAgentsTool {
    const NAME: &'static str = "view_agents";
    const REQUIRED: Requirement = Requirement::Predicate {
        check: principal_can_view_roster,
        reason: ROSTER_DENIED,
    };
    const DESCRIPTION: &'static str = "List every active agent on the team with their role and \
        self-authored profile (what they do, what they work on, what to ask them about). Use \
        this to find who to talk to or hand work to. Returns {\"agents\": [{agent_id, \
        agent_role, profile, profile_updated_at}, ...]}.";
    const SCHEMA: &'static str =
        r#"{"type":"object","properties":{},"required":[],"additionalProperties":false}"#;

    fn call<'a>(
        _principal: Option<&'a Principal>,
        _arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        _now: &'a str,
        _ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let conn = conn.lock().await;
            let rows = match AgentRepository::list_active(&conn) {
                Ok(rows) => rows,
                Err(_e) => {
                    return ToolResult::Failed {
                        message: "Database error reading the agent roster".to_string(),
                    }
                }
            };

            let mut roster: Vec<Value> = rows
                .into_iter()
                .filter(|r| !ROSTER_EXCLUDED_STATUSES.contains(&r.status.as_str()))
                .map(|r| {
                    serde_json::json!({
                        "agent_id": r.agent_id,
                        "agent_role": if r.agent_role.is_empty() { "worker".to_string() } else { r.agent_role },
                        "profile": r.profile,
                        "profile_updated_at": r.profile_updated_at,
                    })
                })
                .collect();
            roster.sort_by(|a, b| {
                a["agent_id"]
                    .as_str()
                    .unwrap_or("")
                    .cmp(b["agent_id"].as_str().unwrap_or(""))
            });

            ToolResult::Ok {
                data: Some(serde_json::json!({ "agents": roster })),
                message: None,
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
    use conexus_db::agent_repository::NewAgent;
    use conexus_db::schema::init_schema;
    use conexus_wakeloop::waiter_registry::WaiterRegistry;
    use std::collections::HashSet;

    fn agent_bearer_with(cap: Capability) -> Principal {
        Principal {
            kind: PrincipalKind::AgentBearer,
            user_id: None,
            agent_id: Some("caller-1".to_string()),
            project_name: None,
            project_role: None,
            agent_role: Some(conexus_core::capability::AgentRole::Worker),
            can_wake_loop: true,
            source_token: None,
            capabilities: Capabilities::Set(HashSet::from([cap])),
        }
    }

    async fn setup() -> AsyncMutex<Connection> {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        AsyncMutex::new(conn)
    }

    fn seed(conn: &Connection, agent_id: &str, role: &str) {
        AgentRepository::create(
            conn,
            NewAgent {
                token: &format!("{agent_id}-tok"),
                agent_id,
                created_at: "2026-06-01T00:00:00Z",
                status: "active",
                current_task: None,
                working_directory: "/tmp",
                color: None,
                agent_role: role,
            },
        )
        .unwrap();
    }

    #[test]
    fn a_caller_with_neither_read_capability_is_denied() {
        let principal = agent_bearer_with(Capability::McpConnect);
        let result =
            ViewAgentsTool::REQUIRED.check(Some(&principal), &conexus_auth::NoPolicyOverrides);
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn lists_live_agents_sorted_by_id_excluding_terminated_tombstone_and_system() {
        let conn = setup().await;
        {
            let c = conn.lock().await;
            seed(&c, "zebra-agent", "worker");
            seed(&c, "alpha-agent", "manager");
            seed(&c, "gone-agent", "worker");
            c.execute(
                "UPDATE agents SET status = 'terminated' WHERE agent_id = 'gone-agent'",
                [],
            )
            .unwrap();
            seed(&c, "system", "worker");
            c.execute(
                "UPDATE agents SET status = 'system' WHERE agent_id = 'system'",
                [],
            )
            .unwrap();
        }
        let principal = agent_bearer_with(Capability::AgentsUse);
        let registry = WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
        let result = ViewAgentsTool::call(
            Some(&principal),
            &Value::Null,
            &conn,
            "2026-06-01T00:00:00Z",
            &ctx,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let agents = data.unwrap()["agents"].as_array().unwrap().clone();
        let ids: Vec<&str> = agents
            .iter()
            .map(|a| a["agent_id"].as_str().unwrap())
            .collect();
        assert_eq!(ids, vec!["alpha-agent", "zebra-agent"]);
        assert_eq!(agents[0]["agent_role"], "manager");
    }

    #[tokio::test]
    async fn an_operator_tier_view_capability_also_admits() {
        let conn = setup().await;
        {
            let c = conn.lock().await;
            seed(&c, "only-agent", "worker");
        }
        let mut principal = agent_bearer_with(Capability::AgentsView);
        principal.kind = PrincipalKind::ForwardingHeader;
        principal.agent_id = None;
        let registry = WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
        let result = ViewAgentsTool::call(
            Some(&principal),
            &Value::Null,
            &conn,
            "2026-06-01T00:00:00Z",
            &ctx,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert_eq!(data.unwrap()["agents"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn a_missing_principal_is_denied() {
        assert!(!principal_can_view_roster(None));
    }
}
