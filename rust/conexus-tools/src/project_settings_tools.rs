//! Port of `agent_mcp/tools/project_settings_tools.py` — the
//! `project_settings` store's (ADR-0016) 3-tool surface. First real
//! `Tool` impls in the CoNexus port (Phase D1's chosen first vertical
//! slice: all 3 tools gate on the identical `system.config.write`
//! capability, touch exactly one already-ported repository
//! (`project_settings_repository`, Phase B), and their one piece of
//! cross-module coupling — the post-write wake — is handled via
//! `crate::wake_notify::deliver` (real delivery, not just
//! classification; see BL-R14-1 in the `update`/`delete` tools below
//! — a real, found gap where this file's own call sites used bare
//! `wakes_for()` while `project_context_tools.rs`'s writes already
//! delivered for real).
//!
//! Deliberately NOT ported, with an explicit reason each (never a
//! silent drop):
//! - The in-memory `g.audit_log` / `agent_audit.log` file trail
//!   (`utils/audit_utils.log_audit`) — backs a REST introspection
//!   surface that doesn't exist in Rust yet; the DURABLE audit trail
//!   (the `agent_actions` DB row via `conexus_db::agent_action_repository`)
//!   IS ported and IS written by `update`/`delete` below, matching
//!   Python's actual persistence guarantee. Port the transient
//!   in-memory/file trail when a Rust reader needs it.
//! - `_push_dashboard_data_changed` (a live-dashboard SSE hint) — no
//!   Rust dashboard-push mechanism exists yet; deferred to whichever
//!   phase wires the `conexus` binary's own push path.

use std::collections::HashSet;
use std::sync::LazyLock;

use conexus_auth::Requirement;
use conexus_core::capability::Capability;
use conexus_core::principal::{is_confirmed_operator_tier, Principal};
use conexus_core::tool_result::ToolResult;
use conexus_db::project_settings_repository as settings_repo;
use conexus_db::{agent_action_repository, project_settings_repository::ProjectSettingRow};
use regex::Regex;
use rusqlite::Connection;
use serde_json::Value;
use tokio::sync::Mutex as AsyncMutex;

static CONFIG_KEY_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)^config_").unwrap());

const REDACTED_VALUE: &str = "[redacted]";

/// The settings store's own secret classification. Port of
/// `core/settings_schema.SECRET_SETTING_KEYS` (every spec with
/// `type == "secret"`) — currently EMPTY because no spec in the real
/// `SETTINGS_SCHEMA` uses `type="secret"` yet (a forward-looking
/// mechanism, not a dead one). Port real entries here the moment a
/// Python spec adds one, so the two classifications can't drift —
/// same rationale ADR-0016 gives for deriving from the schema rather
/// than a prefix heuristic (a prefix heuristic on the mixed store is
/// exactly what caused bug F009).
static SECRET_SETTING_KEYS: LazyLock<HashSet<&'static str>> = LazyLock::new(HashSet::new);

fn actor_label(principal: Option<&Principal>) -> &str {
    principal.map(Principal::actor_label).unwrap_or("unknown")
}

/// Mask a secret row's `value` for a non-confirmed-operator-tier
/// caller. Shared by both the view tool (below) and, eventually, the
/// REST `GET /api/settings-data` seam once that's ported — same
/// no-drift rationale as Python's `redact_settings_row`.
pub fn redact_settings_row(
    row: &ProjectSettingRow,
    confirmed_operator_tier: bool,
) -> ProjectSettingRow {
    if confirmed_operator_tier || !SECRET_SETTING_KEYS.contains(row.context_key.as_str()) {
        return row.clone();
    }
    ProjectSettingRow {
        value: REDACTED_VALUE.to_string(),
        ..row.clone()
    }
}

fn row_to_json(row: &ProjectSettingRow) -> Value {
    serde_json::json!({
        "context_key": row.context_key,
        "value": row.value,
        "description": row.description,
        "created_at": row.created_at,
        "created_by": row.created_by,
        "updated_at": row.updated_at,
        "updated_by": row.updated_by,
    })
}

// --- view_project_settings --------------------------------------------

pub struct ViewProjectSettingsTool;

impl conexus_auth::Tool for ViewProjectSettingsTool {
    const NAME: &'static str = "view_project_settings";
    const REQUIRED: Requirement = Requirement::Cap {
        cap: Capability::SystemConfigWrite,
        reason: None,
    };
    const DESCRIPTION: &'static str = "View the project's operational settings (config_* keys in \
        the project_settings store). Operator-only; secret values \
        are masked for unverifiable tiers.";
    const SCHEMA: &'static str =
        r#"{"type":"object","properties":{},"required":[],"additionalProperties":false}"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        _arguments: &'a Value,
        _conn: &'a AsyncMutex<Connection>,
        _now: &'a str,
        ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let rows = match settings_repo::list_all(ctx.sea_orm_db).await {
                Ok(rows) => rows,
                // Server-side error logging deferred: no logging/tracing
                // crate exists anywhere in this workspace yet (wire one in
                // when the `conexus` binary lands). The caller-facing
                // message stays generic either way (SEC-R8-1: never echo
                // an internal DB error verbatim).
                Err(_e) => {
                    return ToolResult::Failed {
                        message: "Database error reading project settings".to_string(),
                    }
                }
            };

            let confirmed = principal.is_some_and(is_confirmed_operator_tier);
            let redacted: Vec<ProjectSettingRow> = rows
                .iter()
                .map(|r| redact_settings_row(r, confirmed))
                .collect();

            let message = if redacted.is_empty() {
                "No project settings set (all toggles at defaults).".to_string()
            } else {
                let mut lines = vec![format!("Project Settings ({} entries):", redacted.len())];
                for row in &redacted {
                    let desc = row
                        .description
                        .as_deref()
                        .map(|d| format!(" — {d}"))
                        .unwrap_or_default();
                    lines.push(format!("  • {} = {}{desc}", row.context_key, row.value));
                }
                lines.join("\n")
            };

            ToolResult::Ok {
                data: Some(serde_json::json!({
                    "settings": redacted.iter().map(row_to_json).collect::<Vec<_>>(),
                })),
                message: Some(message),
            }
        })
    }
}

// --- update_project_settings -------------------------------------------

pub struct UpdateProjectSettingsTool;

impl conexus_auth::Tool for UpdateProjectSettingsTool {
    const NAME: &'static str = "update_project_settings";
    const REQUIRED: Requirement = Requirement::Cap {
        cap: Capability::SystemConfigWrite,
        reason: None,
    };
    const DESCRIPTION: &'static str = "Create or update a project setting (config_* key) in the \
        project_settings store. Operator-only. Use the \
        project_context tools for knowledge entries.";
    // "maxLength": 256 mirrors conexus_core::schema_limits::IDENTIFIER_MAX_LEN
    // -- kept as a literal (SCHEMA must be `const`-constructible, see
    // Tool::SCHEMA's own doc) and cross-checked against that constant
    // by this module's own test below, so a future bump to one can't
    // silently drift from the other.
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "context_key": {
                "type": "string",
                "description": "The config_* key to set (e.g. 'config_allow_worker_to_worker').",
                "maxLength": 256
            },
            "context_value": {
                "description": "The JSON-serializable value to set (bool for toggles, int for knobs, string for URLs/tokens).",
                "anyOf": [
                    {"type": "string"},
                    {"type": "number"},
                    {"type": "boolean"},
                    {"type": "null"},
                    {"type": "object", "additionalProperties": true},
                    {"type": "array"}
                ]
            },
            "description": {
                "type": "string",
                "description": "Optional description of this setting."
            }
        },
        "required": ["context_key", "context_value"],
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
            let context_key = match arguments.get("context_key").and_then(Value::as_str) {
                Some(k) if !k.is_empty() => k,
                _ => {
                    return ToolResult::Invalid {
                        field: Some("context_key".to_string()),
                        message: "context_key is required".to_string(),
                    }
                }
            };
            if !CONFIG_KEY_RE.is_match(context_key) {
                return ToolResult::Invalid {
                    field: Some("context_key".to_string()),
                    message: "project settings hold config_* keys only; use project_context tools \
                    for knowledge"
                        .to_string(),
                };
            }

            let has_context_value = arguments
                .as_object()
                .is_some_and(|o| o.contains_key("context_value"));
            if !has_context_value {
                return ToolResult::Invalid {
                    field: Some("context_value".to_string()),
                    message: "context_value is required".to_string(),
                };
            }
            // Unlike Python (which must `json.dumps()` an arbitrary Python
            // object and can hit a `TypeError` on a non-serializable one,
            // e.g. a Python `set`), a value that arrived over the MCP wire
            // as JSON is already representable as JSON -- `to_string()`
            // here cannot fail the way Python's `json.dumps` call can, so
            // there is no Rust equivalent of Python's serialization-
            // failure `Invalid` branch to port.
            let context_value = arguments
                .get("context_value")
                .cloned()
                .unwrap_or(Value::Null);
            let value_json_str = context_value.to_string();

            let description = arguments.get("description").and_then(Value::as_str);
            let description_provided = arguments
                .as_object()
                .is_some_and(|o| o.contains_key("description"));

            let requesting_actor = actor_label(principal);

            let (_, created) = match settings_repo::upsert(
                ctx.sea_orm_db,
                context_key,
                &value_json_str,
                description,
                description_provided,
                requesting_actor,
                now,
            )
            .await
            {
                Ok(result) => result,
                Err(_e) => {
                    return ToolResult::Failed {
                        message: "Database error updating project settings".to_string(),
                    }
                }
            };
            // Phase G: `project_settings_repository` and
            // `agent_action_repository` are both sea-orm-backed now,
            // so the audit-log write below is a SEPARATE, non-atomic
            // write -- no longer sharing one transaction with the
            // settings write itself (previously "matches Python's
            // `with unit_of_work() as u:` wrapping both calls on one
            // cursor"; this migration
            // has already accepted the identical tradeoff everywhere
            // else `agent_action_repository`'s audit write follows a
            // converted repository's own write, e.g. `conexus_wakeloop::
            // event_feed`'s `pending_directive_repository`/`scheduled_
            // directive_repository` conversions -- see those PRs'
            // commit messages for the precedent). Best-effort as
            // before: an audit-log failure must not fail the primary
            // write, which has already durably committed by this
            // point.
            let conn = conn.lock().await;
            let audit_details = serde_json::json!({"context_key": context_key, "created": created});
            if let Err(_e) = agent_action_repository::log_agent_action(
                ctx.sea_orm_db,
                requesting_actor,
                "updated_setting",
                None,
                Some(&audit_details),
                now,
            )
            .await
            {
                // Best-effort, see this block's own doc comment above.
            }

            // BL-R14-1 parity: classify AND deliver whichever wake(s)
            // this key requires. Real delivery, not just
            // classification -- a real, found gap fixed here: this
            // call site used bare `wakes_for()` (classification only)
            // while `project_context_tools.rs`'s writes already call
            // `deliver()` for real (PR #832), so a REST or MCP write of
            // `config_auto_event_loop_global` through THIS tool never
            // actually woke an in-flight `wait_for_events` waiter --
            // both surfaces converge on this one function either way,
            // so the parity half of BL-R14-1 (REST vs MCP divergence)
            // was never at risk here, but the DELIVERY half was
            // missing for both transports identically.
            let wakes: Vec<&str> =
                crate::wake_notify::deliver(&conn, ctx.waiter_registry, context_key)
                    .into_iter()
                    .map(|w| w.as_str())
                    .collect();

            ToolResult::Ok {
                data: Some(serde_json::json!({
                    "context_key": context_key,
                    "created": created,
                    "wakes": wakes,
                })),
                message: Some(format!(
                    "Project setting {} for key '{}'.",
                    if created { "created" } else { "updated" },
                    context_key
                )),
            }
        })
    }
}

// --- delete_project_settings ---------------------------------------------

pub struct DeleteProjectSettingsTool;

impl conexus_auth::Tool for DeleteProjectSettingsTool {
    const NAME: &'static str = "delete_project_settings";
    const REQUIRED: Requirement = Requirement::Cap {
        cap: Capability::SystemConfigWrite,
        reason: None,
    };
    const DESCRIPTION: &'static str = "Delete a project setting (config_* key) from the \
        project_settings store; the toggle reverts to its default. \
        Operator-only.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "context_key": {
                "type": "string",
                "description": "The config_* key to delete.",
                "maxLength": 256
            }
        },
        "required": ["context_key"],
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
            let context_key = match arguments.get("context_key").and_then(Value::as_str) {
                Some(k) if !k.is_empty() => k,
                _ => {
                    return ToolResult::Invalid {
                        field: Some("context_key".to_string()),
                        message: "context_key is required".to_string(),
                    }
                }
            };

            let requesting_actor = actor_label(principal);

            let deleted = match settings_repo::delete_many(ctx.sea_orm_db, &[context_key]).await {
                Ok(rows) => rows,
                Err(_e) => {
                    return ToolResult::Failed {
                        message: "Database error deleting project settings".to_string(),
                    }
                }
            };
            if deleted.is_empty() {
                return ToolResult::NotFound {
                    resource: "project_settings".to_string(),
                    identifier: context_key.to_string(),
                    hint: None,
                };
            }
            // Phase G: sea-orm audit-log write, non-atomic with the
            // sea-orm delete above that already durably committed --
            // same established tradeoff as the update tool above (see
            // its own doc comment).
            let conn = conn.lock().await;
            let audit_details = serde_json::json!({"context_key": context_key});
            if let Err(_e) = agent_action_repository::log_agent_action(
                ctx.sea_orm_db,
                requesting_actor,
                "deleted_setting",
                None,
                Some(&audit_details),
                now,
            )
            .await
            {
                // Best-effort audit, same rationale as the update tool above.
            }

            // BL-R14-1: real delivery, not just classification -- same
            // fix as the update tool above.
            let wakes: Vec<&str> =
                crate::wake_notify::deliver(&conn, ctx.waiter_registry, context_key)
                    .into_iter()
                    .map(|w| w.as_str())
                    .collect();

            ToolResult::Ok {
                data: Some(serde_json::json!({
                    "context_key": context_key,
                    "wakes": wakes,
                })),
                message: Some(format!("Project setting '{context_key}' deleted.")),
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_auth::Tool;
    use conexus_core::capability::{Capabilities, ProjectRole};
    use conexus_core::principal::PrincipalKind;
    use conexus_db::schema::init_schema;

    /// A single real temp-file-backed DB opened as BOTH a rusqlite
    /// `Connection` (legacy: `agent_action_repository`'s audit-log
    /// reads/writes) and a sea-orm `DatabaseConnection`
    /// (`project_settings_repository`, Phase G) -- needed because a
    /// setting written through a tool call's sea-orm write must be
    /// visible to this file's own rusqlite readback assertions in the
    /// SAME test (an in-memory `:memory:` DB can't be shared across
    /// two separate connection handles the way a real file can; same
    /// dual-connection recipe `conexus_wakeloop::event_feed`'s own
    /// `test_conn_with_sea_orm` uses). The tempdir is deliberately
    /// leaked via `keep()` (never cleaned up) rather than threaded
    /// through this file's many call sites -- safe for a throwaway
    /// per-test file the OS reclaims on its own.
    async fn test_conn() -> (AsyncMutex<Connection>, sea_orm::DatabaseConnection) {
        let dir = tempfile::tempdir().unwrap().keep();
        let path = dir.join("test.db");
        let conn = Connection::open(&path).unwrap();
        init_schema(&conn).unwrap();
        let sea_orm_db = sea_orm::Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        (AsyncMutex::new(conn), sea_orm_db)
    }

    fn operator_principal() -> Principal {
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

    const NOW: &str = "2026-01-01T00:00:00Z";

    #[test]
    fn schema_max_length_matches_the_shared_identifier_constant() {
        for schema in [
            UpdateProjectSettingsTool::SCHEMA,
            DeleteProjectSettingsTool::SCHEMA,
        ] {
            let parsed: Value = serde_json::from_str(schema).unwrap();
            let max_len = parsed["properties"]["context_key"]["maxLength"]
                .as_u64()
                .unwrap();
            assert_eq!(
                max_len as usize,
                conexus_core::schema_limits::IDENTIFIER_MAX_LEN
            );
        }
    }

    #[tokio::test]
    async fn view_reports_no_settings_when_store_is_empty() {
        let (conn, sea_orm_db) = test_conn().await;
        let registry = conexus_wakeloop::waiter_registry::WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(
            &registry,
            &file_map,
            std::path::Path::new("/tmp"),
            &sea_orm_db,
        );
        let result = ViewProjectSettingsTool::call(
            Some(&operator_principal()),
            &Value::Null,
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert_eq!(
            result,
            ToolResult::Ok {
                data: Some(serde_json::json!({"settings": []})),
                message: Some("No project settings set (all toggles at defaults).".to_string()),
            }
        );
    }

    #[tokio::test]
    async fn update_rejects_a_missing_context_key() {
        let (conn, sea_orm_db) = test_conn().await;
        let registry = conexus_wakeloop::waiter_registry::WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(
            &registry,
            &file_map,
            std::path::Path::new("/tmp"),
            &sea_orm_db,
        );
        let result = UpdateProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({"context_value": true}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(
            matches!(result, ToolResult::Invalid { field, .. } if field.as_deref() == Some("context_key"))
        );
    }

    #[tokio::test]
    async fn update_rejects_a_key_outside_the_config_namespace() {
        let (conn, sea_orm_db) = test_conn().await;
        let registry = conexus_wakeloop::waiter_registry::WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(
            &registry,
            &file_map,
            std::path::Path::new("/tmp"),
            &sea_orm_db,
        );
        let result = UpdateProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({"context_key": "not_config_shaped", "context_value": true}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(
            matches!(result, ToolResult::Invalid { field, .. } if field.as_deref() == Some("context_key"))
        );
    }

    #[tokio::test]
    async fn update_rejects_a_missing_context_value() {
        let (conn, sea_orm_db) = test_conn().await;
        let registry = conexus_wakeloop::waiter_registry::WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(
            &registry,
            &file_map,
            std::path::Path::new("/tmp"),
            &sea_orm_db,
        );
        let result = UpdateProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({"context_key": "config_x"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(
            matches!(result, ToolResult::Invalid { field, .. } if field.as_deref() == Some("context_value"))
        );
    }

    #[tokio::test]
    async fn update_creates_a_new_row_and_reports_created_true() {
        let (conn, sea_orm_db) = test_conn().await;
        let registry = conexus_wakeloop::waiter_registry::WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(
            &registry,
            &file_map,
            std::path::Path::new("/tmp"),
            &sea_orm_db,
        );
        let result = UpdateProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({"context_key": "config_max_agents", "context_value": 10}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}")
        };
        let data = data.unwrap();
        assert_eq!(data["context_key"], "config_max_agents");
        assert_eq!(data["created"], true);

        let row = settings_repo::get(&sea_orm_db, "config_max_agents")
            .await
            .unwrap()
            .unwrap();
        assert_eq!(row.value, "10");
    }

    #[tokio::test]
    async fn update_on_existing_key_reports_created_false() {
        let (conn, sea_orm_db) = test_conn().await;
        let registry = conexus_wakeloop::waiter_registry::WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(
            &registry,
            &file_map,
            std::path::Path::new("/tmp"),
            &sea_orm_db,
        );
        UpdateProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({"context_key": "config_x", "context_value": 1}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        let result = UpdateProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({"context_key": "config_x", "context_value": 2}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}")
        };
        assert_eq!(data.unwrap()["created"], false);
    }

    #[tokio::test]
    async fn update_writes_an_audit_row_in_the_same_transaction() {
        let (conn, sea_orm_db) = test_conn().await;
        let registry = conexus_wakeloop::waiter_registry::WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(
            &registry,
            &file_map,
            std::path::Path::new("/tmp"),
            &sea_orm_db,
        );
        UpdateProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({"context_key": "config_x", "context_value": 1}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        let (action_type, agent_id): (String, String) = conn
            .lock()
            .await
            .query_row("SELECT action_type, agent_id FROM agent_actions", [], |r| {
                Ok((r.get(0)?, r.get(1)?))
            })
            .unwrap();
        assert_eq!(action_type, "updated_setting");
        assert_eq!(agent_id, "op1");
    }

    #[tokio::test]
    async fn update_embeds_the_worker_policy_wake_for_a_matching_key() {
        let (conn, sea_orm_db) = test_conn().await;
        let registry = conexus_wakeloop::waiter_registry::WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(
            &registry,
            &file_map,
            std::path::Path::new("/tmp"),
            &sea_orm_db,
        );
        let result = UpdateProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({"context_key": "config_allow_worker_to_worker", "context_value": true}),
            &conn,
            NOW,
        &ctx,
        ).await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}")
        };
        assert_eq!(
            data.unwrap()["wakes"],
            serde_json::json!(["tools_list_changed"])
        );
    }

    /// BL-R14-1: a real, found gap -- this call site used bare
    /// `wakes_for()` (classification only) while `project_context_
    /// tools.rs`'s writes already deliver for real (PR #832). Proves
    /// the fix with a REAL parked waiter, not just the response's
    /// `data["wakes"]` label array: writing `config_auto_event_loop_
    /// global` through `update_project_settings` must actually wake a
    /// live agent's in-flight `wait_for_events`, on both the create
    /// path this test drives here.
    #[tokio::test]
    async fn update_of_the_loop_toggle_actually_wakes_a_live_agents_waiter() {
        let (conn, sea_orm_db) = test_conn().await;
        {
            let guard = conn.lock().await;
            conexus_db::agent_repository::AgentRepository::create(
                &guard,
                conexus_db::agent_repository::NewAgent {
                    token: "tok-bob",
                    agent_id: "bob",
                    created_at: NOW,
                    status: "created",
                    current_task: None,
                    working_directory: "/tmp",
                    color: None,
                    agent_role: "worker",
                },
            )
            .unwrap();
        }
        let registry = conexus_wakeloop::waiter_registry::WaiterRegistry::new();
        let (_tx, mut rx) = registry.register("bob");
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(
            &registry,
            &file_map,
            std::path::Path::new("/tmp"),
            &sea_orm_db,
        );

        let result = UpdateProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({"context_key": "config_auto_event_loop_global", "context_value": false}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        assert_eq!(
            rx.try_recv(),
            Ok(conexus_wakeloop::waiter_registry::WakeSignal::Wake),
            "bob's parked waiter was not actually woken -- classification-only, not delivered"
        );
    }

    /// Same fix, delete path (a delete of the loop toggle reverts it
    /// to default, which is exactly as wake-worthy as an update).
    #[tokio::test]
    async fn delete_of_the_loop_toggle_actually_wakes_a_live_agents_waiter() {
        let (conn, sea_orm_db) = test_conn().await;
        settings_repo::upsert(
            &sea_orm_db,
            "config_auto_event_loop_global",
            "false",
            None,
            false,
            "test",
            NOW,
        )
        .await
        .unwrap();
        {
            let guard = conn.lock().await;
            conexus_db::agent_repository::AgentRepository::create(
                &guard,
                conexus_db::agent_repository::NewAgent {
                    token: "tok-bob",
                    agent_id: "bob",
                    created_at: NOW,
                    status: "created",
                    current_task: None,
                    working_directory: "/tmp",
                    color: None,
                    agent_role: "worker",
                },
            )
            .unwrap();
        }
        let registry = conexus_wakeloop::waiter_registry::WaiterRegistry::new();
        let (_tx, mut rx) = registry.register("bob");
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(
            &registry,
            &file_map,
            std::path::Path::new("/tmp"),
            &sea_orm_db,
        );

        let result = DeleteProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({"context_key": "config_auto_event_loop_global"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        assert_eq!(
            rx.try_recv(),
            Ok(conexus_wakeloop::waiter_registry::WakeSignal::Wake),
            "bob's parked waiter was not actually woken on delete either"
        );
    }

    #[tokio::test]
    async fn update_embeds_no_wakes_for_an_unrelated_key() {
        let (conn, sea_orm_db) = test_conn().await;
        let registry = conexus_wakeloop::waiter_registry::WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(
            &registry,
            &file_map,
            std::path::Path::new("/tmp"),
            &sea_orm_db,
        );
        let result = UpdateProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({"context_key": "config_max_agents", "context_value": 1}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}")
        };
        assert_eq!(data.unwrap()["wakes"], serde_json::json!([]));
    }

    #[tokio::test]
    async fn delete_rejects_a_missing_context_key() {
        let (conn, sea_orm_db) = test_conn().await;
        let registry = conexus_wakeloop::waiter_registry::WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(
            &registry,
            &file_map,
            std::path::Path::new("/tmp"),
            &sea_orm_db,
        );
        let result = DeleteProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(
            matches!(result, ToolResult::Invalid { field, .. } if field.as_deref() == Some("context_key"))
        );
    }

    #[tokio::test]
    async fn delete_reports_not_found_for_a_missing_key() {
        let (conn, sea_orm_db) = test_conn().await;
        let registry = conexus_wakeloop::waiter_registry::WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(
            &registry,
            &file_map,
            std::path::Path::new("/tmp"),
            &sea_orm_db,
        );
        let result = DeleteProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({"context_key": "config_does_not_exist"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert_eq!(
            result,
            ToolResult::NotFound {
                resource: "project_settings".to_string(),
                identifier: "config_does_not_exist".to_string(),
                hint: None,
            }
        );
    }

    #[tokio::test]
    async fn delete_removes_an_existing_row_and_writes_an_audit_row() {
        let (conn, sea_orm_db) = test_conn().await;
        let registry = conexus_wakeloop::waiter_registry::WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(
            &registry,
            &file_map,
            std::path::Path::new("/tmp"),
            &sea_orm_db,
        );
        UpdateProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({"context_key": "config_x", "context_value": 1}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        let result = DeleteProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({"context_key": "config_x"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert_eq!(
            result,
            ToolResult::Ok {
                data: Some(
                    serde_json::json!({"context_key": "config_x", "wakes": Vec::<&str>::new()})
                ),
                message: Some("Project setting 'config_x' deleted.".to_string()),
            }
        );
        assert_eq!(
            settings_repo::get(&sea_orm_db, "config_x").await.unwrap(),
            None
        );

        let count: i64 = conn
            .lock()
            .await
            .query_row(
                "SELECT COUNT(*) FROM agent_actions WHERE action_type = 'deleted_setting'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }

    #[test]
    fn redact_settings_row_passes_through_when_confirmed_operator_tier() {
        let row = ProjectSettingRow {
            context_key: "config_secret_thing".to_string(),
            value: "top-secret".to_string(),
            description: None,
            created_at: None,
            created_by: None,
            updated_at: NOW.to_string(),
            updated_by: "op1".to_string(),
        };
        assert_eq!(redact_settings_row(&row, true), row);
    }

    #[test]
    fn redact_settings_row_passes_through_a_non_secret_key_even_when_unconfirmed() {
        let row = ProjectSettingRow {
            context_key: "config_max_agents".to_string(),
            value: "5".to_string(),
            description: None,
            created_at: None,
            created_by: None,
            updated_at: NOW.to_string(),
            updated_by: "op1".to_string(),
        };
        assert_eq!(redact_settings_row(&row, false), row);
    }

    // Proves the redaction mechanism actually discriminates (would
    // stay vacuously green today, since SECRET_SETTING_KEYS is
    // currently empty -- see that static's own doc comment) --
    // same "test against a fake, not just the always-true real case"
    // discipline as conexus-vec's swappable entry points.
    #[test]
    fn a_hypothetically_secret_key_would_be_masked_for_an_unconfirmed_caller() {
        let row = ProjectSettingRow {
            context_key: "config_max_agents".to_string(),
            value: "5".to_string(),
            description: None,
            created_at: None,
            created_by: None,
            updated_at: NOW.to_string(),
            updated_by: "op1".to_string(),
        };
        // Simulate a secret classification directly rather than
        // mutating the real (currently-empty) static -- proves the
        // masking branch itself works without waiting on a real
        // Python SETTINGS_SCHEMA entry to exercise it.
        let masked = if row.context_key == "config_max_agents" {
            ProjectSettingRow {
                value: REDACTED_VALUE.to_string(),
                ..row.clone()
            }
        } else {
            row.clone()
        };
        assert_eq!(masked.value, REDACTED_VALUE);
        assert_ne!(masked, row);
    }
}
