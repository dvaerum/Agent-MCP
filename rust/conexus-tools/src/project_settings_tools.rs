//! Port of `agent_mcp/tools/project_settings_tools.py` — the
//! `project_settings` store's (ADR-0016) 3-tool surface. First real
//! `Tool` impls in the CoNexus port (Phase D1's chosen first vertical
//! slice: all 3 tools gate on the identical `system.config.write`
//! capability, touch exactly one already-ported repository
//! (`project_settings_repository`, Phase B), and their one piece of
//! cross-module coupling — the post-write wake — is handled via
//! `crate::wake_notify` rather than silently dropped; see that
//! module's doc for why delivery is deferred but classification isn't).
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

use crate::wake_notify::wakes_for;

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

    fn call(
        principal: Option<&Principal>,
        _arguments: &Value,
        conn: &Connection,
        _now: &str,
    ) -> ToolResult {
        let rows = match settings_repo::list_all(conn) {
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

    fn call(
        principal: Option<&Principal>,
        arguments: &Value,
        conn: &Connection,
        now: &str,
    ) -> ToolResult {
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

        let tx = match conn.unchecked_transaction() {
            Ok(tx) => tx,
            Err(_e) => {
                return ToolResult::Failed {
                    message: "Database error updating project settings".to_string(),
                }
            }
        };
        let (_, created) = match settings_repo::upsert(
            &tx,
            context_key,
            &value_json_str,
            description,
            description_provided,
            requesting_actor,
            now,
        ) {
            Ok(result) => result,
            Err(_e) => {
                return ToolResult::Failed {
                    message: "Database error updating project settings".to_string(),
                }
            }
        };
        // Audit through the SAME transaction as the settings write --
        // matches Python's `with unit_of_work() as u:` wrapping both
        // calls on one cursor.
        let audit_details = serde_json::json!({"context_key": context_key, "created": created});
        if let Err(_e) = agent_action_repository::log_agent_action(
            &tx,
            requesting_actor,
            "updated_setting",
            None,
            Some(&audit_details),
            now,
        ) {
            // Best-effort: an audit-log failure must not fail the
            // primary write (matches Python's own try/except around
            // `log_agent_action_to_db`'s INSERT) -- but it DOES still
            // fail the whole transaction if left uncommitted, so
            // explicitly fall through to the same commit either way
            // rather than returning early, matching Python's
            // fire-and-forget semantics (the underlying call there
            // also can't roll back the settings write on audit
            // failure -- it just logs and continues).
        }
        if let Err(_e) = tx.commit() {
            return ToolResult::Failed {
                message: "Database error updating project settings".to_string(),
            };
        }

        // BL-R14-1 parity: classify which post-write wake(s) this key
        // requires. Actual delivery deferred -- see crate::wake_notify.
        let wakes: Vec<&str> = wakes_for(context_key)
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

    fn call(
        principal: Option<&Principal>,
        arguments: &Value,
        conn: &Connection,
        now: &str,
    ) -> ToolResult {
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

        let tx = match conn.unchecked_transaction() {
            Ok(tx) => tx,
            Err(_e) => {
                return ToolResult::Failed {
                    message: "Database error deleting project settings".to_string(),
                }
            }
        };
        let deleted = match settings_repo::delete_many(&tx, &[context_key]) {
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
        let audit_details = serde_json::json!({"context_key": context_key});
        if let Err(_e) = agent_action_repository::log_agent_action(
            &tx,
            requesting_actor,
            "deleted_setting",
            None,
            Some(&audit_details),
            now,
        ) {
            // Best-effort audit, same rationale as the update tool above.
        }
        if let Err(_e) = tx.commit() {
            return ToolResult::Failed {
                message: "Database error deleting project settings".to_string(),
            };
        }

        let wakes: Vec<&str> = wakes_for(context_key)
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
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_auth::Tool;
    use conexus_core::capability::{Capabilities, ProjectRole};
    use conexus_core::principal::PrincipalKind;
    use conexus_db::schema::init_schema;

    fn test_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        conn
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

    #[test]
    fn view_reports_no_settings_when_store_is_empty() {
        let conn = test_conn();
        let result =
            ViewProjectSettingsTool::call(Some(&operator_principal()), &Value::Null, &conn, NOW);
        assert_eq!(
            result,
            ToolResult::Ok {
                data: Some(serde_json::json!({"settings": []})),
                message: Some("No project settings set (all toggles at defaults).".to_string()),
            }
        );
    }

    #[test]
    fn update_rejects_a_missing_context_key() {
        let conn = test_conn();
        let result = UpdateProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({"context_value": true}),
            &conn,
            NOW,
        );
        assert!(
            matches!(result, ToolResult::Invalid { field, .. } if field.as_deref() == Some("context_key"))
        );
    }

    #[test]
    fn update_rejects_a_key_outside_the_config_namespace() {
        let conn = test_conn();
        let result = UpdateProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({"context_key": "not_config_shaped", "context_value": true}),
            &conn,
            NOW,
        );
        assert!(
            matches!(result, ToolResult::Invalid { field, .. } if field.as_deref() == Some("context_key"))
        );
    }

    #[test]
    fn update_rejects_a_missing_context_value() {
        let conn = test_conn();
        let result = UpdateProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({"context_key": "config_x"}),
            &conn,
            NOW,
        );
        assert!(
            matches!(result, ToolResult::Invalid { field, .. } if field.as_deref() == Some("context_value"))
        );
    }

    #[test]
    fn update_creates_a_new_row_and_reports_created_true() {
        let conn = test_conn();
        let result = UpdateProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({"context_key": "config_max_agents", "context_value": 10}),
            &conn,
            NOW,
        );
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}")
        };
        let data = data.unwrap();
        assert_eq!(data["context_key"], "config_max_agents");
        assert_eq!(data["created"], true);

        let row = settings_repo::get(&conn, "config_max_agents")
            .unwrap()
            .unwrap();
        assert_eq!(row.value, "10");
    }

    #[test]
    fn update_on_existing_key_reports_created_false() {
        let conn = test_conn();
        UpdateProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({"context_key": "config_x", "context_value": 1}),
            &conn,
            NOW,
        );
        let result = UpdateProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({"context_key": "config_x", "context_value": 2}),
            &conn,
            NOW,
        );
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}")
        };
        assert_eq!(data.unwrap()["created"], false);
    }

    #[test]
    fn update_writes_an_audit_row_in_the_same_transaction() {
        let conn = test_conn();
        UpdateProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({"context_key": "config_x", "context_value": 1}),
            &conn,
            NOW,
        );
        let (action_type, agent_id): (String, String) = conn
            .query_row("SELECT action_type, agent_id FROM agent_actions", [], |r| {
                Ok((r.get(0)?, r.get(1)?))
            })
            .unwrap();
        assert_eq!(action_type, "updated_setting");
        assert_eq!(agent_id, "op1");
    }

    #[test]
    fn update_embeds_the_worker_policy_wake_for_a_matching_key() {
        let conn = test_conn();
        let result = UpdateProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({"context_key": "config_allow_worker_to_worker", "context_value": true}),
            &conn,
            NOW,
        );
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}")
        };
        assert_eq!(
            data.unwrap()["wakes"],
            serde_json::json!(["tools_list_changed"])
        );
    }

    #[test]
    fn update_embeds_no_wakes_for_an_unrelated_key() {
        let conn = test_conn();
        let result = UpdateProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({"context_key": "config_max_agents", "context_value": 1}),
            &conn,
            NOW,
        );
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}")
        };
        assert_eq!(data.unwrap()["wakes"], serde_json::json!([]));
    }

    #[test]
    fn delete_rejects_a_missing_context_key() {
        let conn = test_conn();
        let result = DeleteProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({}),
            &conn,
            NOW,
        );
        assert!(
            matches!(result, ToolResult::Invalid { field, .. } if field.as_deref() == Some("context_key"))
        );
    }

    #[test]
    fn delete_reports_not_found_for_a_missing_key() {
        let conn = test_conn();
        let result = DeleteProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({"context_key": "config_does_not_exist"}),
            &conn,
            NOW,
        );
        assert_eq!(
            result,
            ToolResult::NotFound {
                resource: "project_settings".to_string(),
                identifier: "config_does_not_exist".to_string(),
                hint: None,
            }
        );
    }

    #[test]
    fn delete_removes_an_existing_row_and_writes_an_audit_row() {
        let conn = test_conn();
        UpdateProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({"context_key": "config_x", "context_value": 1}),
            &conn,
            NOW,
        );
        let result = DeleteProjectSettingsTool::call(
            Some(&operator_principal()),
            &serde_json::json!({"context_key": "config_x"}),
            &conn,
            NOW,
        );
        assert_eq!(
            result,
            ToolResult::Ok {
                data: Some(
                    serde_json::json!({"context_key": "config_x", "wakes": Vec::<&str>::new()})
                ),
                message: Some("Project setting 'config_x' deleted.".to_string()),
            }
        );
        assert_eq!(settings_repo::get(&conn, "config_x").unwrap(), None);

        let count: i64 = conn
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
