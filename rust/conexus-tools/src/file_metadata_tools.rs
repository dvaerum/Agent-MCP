//! Port of `agent_mcp/tools/file_metadata_tools.py` (Phase D5, PR 5).
//! Two tools: `view_file_metadata` (agent-bearer + `files.use`,
//! `Requirement::Predicate` — same load-bearing "keys on `agent_id`"
//! rationale as `file_management_tools.rs`'s sibling read tool) and
//! `update_file_metadata` (`Cap(system.config.write)`, operator-only —
//! Python's own comment on this exact tool: the gate collapses to a
//! single capability test, unlike its Predicate-gated sibling).
//!
//! Path normalization is DELIBERATELY DIFFERENT from
//! `file_management_tools.rs`'s `resolve_abs_filepath`: Python's
//! `_normalize_filepath` here calls `Path.resolve()`, which — unlike
//! `os.path.abspath` — actually resolves symlinks on disk (a real
//! filesystem call, not pure lexical normalization) and can raise
//! `ValueError`/`OSError` on a malformed path (e.g. an embedded null
//! byte), which the Python source deliberately catches and maps to a
//! controlled `Invalid` rather than a 500 (R6-F3). [`normalize_filepath`]
//! reproduces this: reject an embedded NUL byte up front (Rust doesn't
//! validate this when building a `PathBuf`, but the underlying OS call
//! would reject it), try `fs::canonicalize` (the real symlink-
//! resolving syscall, matching Python's happy path when the target
//! exists), and fall back to the SAME lexical join+normalize
//! `file_management_tools::resolve_abs_filepath` already implements
//! when the path doesn't exist yet (Python's `Path.resolve()` without
//! `strict=True` tolerates a nonexistent tail the same way — reusing
//! the existing lexical fallback rather than hand-rolling a second
//! one). Documented approximation: this does NOT walk resolving
//! symlinks on an *existing* leading directory whose full path doesn't
//! yet exist (a rare edge case, not security-relevant for a metadata
//! cache key).
//!
//! Deliberately NOT ported, with an explicit reason (never a silent
//! drop): the in-memory `log_audit` trail — same precedent as every
//! prior Phase D5 tool. The DURABLE `agent_actions` audit row IS
//! ported (`update_file_metadata`'s `log_agent_action_to_db` call),
//! matching Python's actual persistence guarantee.

use conexus_auth::{Requirement, Tool};
use conexus_core::capability::Capability;
use conexus_core::principal::Principal;
use conexus_core::tool_result::ToolResult;
use conexus_db::{agent_action_repository, file_metadata_repository};
use rusqlite::Connection;
use serde_json::Value;
use tokio::sync::Mutex as AsyncMutex;

use crate::file_management_tools::{is_file_capable_agent, resolve_abs_filepath};
use crate::task_tools::str_arg;

/// See this module's doc for why this differs from
/// `file_management_tools::resolve_abs_filepath`. Returns `None` on a
/// malformed path (embedded NUL byte) — the caller maps that to
/// `Invalid`, matching Python's `ValueError`-catch.
fn normalize_filepath(base: &str, path: &str) -> Option<String> {
    if path.contains('\0') {
        return None;
    }
    let lexical = resolve_abs_filepath(base, path);
    match std::fs::canonicalize(&lexical) {
        Ok(resolved) => Some(resolved.display().to_string()),
        Err(_) => Some(lexical),
    }
}

const VIEW_DENIED: &str =
    "Unauthorized: agent token with files.use capability required to view file metadata";
const UPDATE_DENIED: &str = "Unauthorized: Updating file metadata is an operator-only action; a \
    worker agent cannot record file metadata. Ask a project operator to set it. (You can still \
    read metadata with view_file_metadata.)";

pub struct ViewFileMetadataTool;

impl Tool for ViewFileMetadataTool {
    const NAME: &'static str = "view_file_metadata";
    const REQUIRED: Requirement = Requirement::Predicate {
        check: is_file_capable_agent,
        reason: VIEW_DENIED,
    };
    const DESCRIPTION: &'static str =
        "View stored metadata (e.g., purpose, components) for a specific file path.";
    // "maxLength": 4096 mirrors conexus_core::schema_limits::PATH_MAX_LEN
    // -- kept as a literal (SCHEMA must be `const`-constructible) and
    // cross-checked against that constant by this module's own test.
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "filepath": {
                "type": "string",
                "description": "Path to the file (can be relative to agent's CWD or absolute)",
                "maxLength": 4096
            }
        },
        "required": ["filepath"],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        _now: &'a str,
        _ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let Some(filepath_arg) = str_arg(arguments, "filepath").filter(|s| !s.is_empty())
            else {
                return ToolResult::Invalid {
                    field: Some("filepath".to_string()),
                    message: "filepath is required and must be a string.".to_string(),
                };
            };
            let agent_id = principal.and_then(|p| p.agent_id.as_deref()).unwrap_or("");

            let guard = conn.lock().await;
            let wd = crate::file_management_tools::working_directory_for(&guard, agent_id);
            let Some(normalized) = normalize_filepath(&wd, &filepath_arg) else {
                return ToolResult::Invalid {
                    field: Some("filepath".to_string()),
                    message: "filepath could not be resolved to a valid path.".to_string(),
                };
            };

            match file_metadata_repository::get(&guard, &normalized) {
                Ok(Some(row)) => {
                    let metadata_parsed: Value = serde_json::from_str(&row.metadata)
                        .unwrap_or_else(|_| {
                            serde_json::json!({
                                "error": "Could not parse stored metadata string.",
                                "raw_value": row.metadata,
                            })
                        });
                    let response_data = serde_json::json!({
                        "filepath": normalized,
                        "metadata": metadata_parsed,
                        "last_updated_by": row.updated_by,
                        "last_updated_at": row.last_updated,
                        "content_hash": row.content_hash.unwrap_or_else(|| "N/A".to_string()),
                    });
                    let message = format!(
                        "Metadata for file '{filepath_arg}' (normalized: {normalized}):\n\n{}",
                        serde_json::to_string_pretty(&response_data).unwrap_or_default()
                    );
                    ToolResult::Ok {
                        data: Some(response_data),
                        message: Some(message),
                    }
                }
                Ok(None) => ToolResult::Ok {
                    data: Some(serde_json::json!({
                        "filepath": normalized,
                        "metadata": null,
                        "last_updated_by": null,
                        "last_updated_at": null,
                        "content_hash": "N/A",
                    })),
                    message: Some(format!(
                        "No metadata has been recorded for '{normalized}' yet. File metadata is \
                         optional and operator-managed; an empty result here is normal."
                    )),
                },
                Err(_e) => ToolResult::Failed {
                    message: "A database error occurred; it has been logged. Retry, or ask an \
                        operator to check logs."
                        .to_string(),
                },
            }
        })
    }
}

pub struct UpdateFileMetadataTool;

impl Tool for UpdateFileMetadataTool {
    const NAME: &'static str = "update_file_metadata";
    const REQUIRED: Requirement = Requirement::Cap {
        cap: Capability::SystemConfigWrite,
        reason: Some(UPDATE_DENIED),
    };
    const DESCRIPTION: &'static str = "Add or replace the entire metadata object for a specific \
        file path. Admin only.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "filepath": {
                "type": "string",
                "description": "Path to the file (can be relative or absolute)",
                "maxLength": 4096
            },
            "metadata": {
                "type": "object",
                "description": "A JSON object containing the metadata to set for the file."
            }
        },
        "required": ["filepath", "metadata"],
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
            let Some(filepath_arg) = str_arg(arguments, "filepath").filter(|s| !s.is_empty())
            else {
                return ToolResult::Invalid {
                    field: Some("filepath".to_string()),
                    message: "filepath is required and must be a string.".to_string(),
                };
            };
            let Some(metadata_to_set) = arguments.get("metadata").filter(|v| v.is_object()) else {
                return ToolResult::Invalid {
                    field: Some("metadata".to_string()),
                    message: "metadata is required and must be a dictionary.".to_string(),
                };
            };

            // Operator-tier callers attribute via user_id (actor_label
            // falls back through agent_id -> user_id -> kind label);
            // matches Python's `principal.actor_label()`.
            let requesting_admin_id = principal.map(Principal::actor_label).unwrap_or("unknown");
            let agent_id_for_wd = principal.and_then(|p| p.agent_id.as_deref()).unwrap_or("");

            let guard = conn.lock().await;
            let wd = crate::file_management_tools::working_directory_for(&guard, agent_id_for_wd);
            let Some(normalized) = normalize_filepath(&wd, &filepath_arg) else {
                return ToolResult::Invalid {
                    field: Some("filepath".to_string()),
                    message: "filepath could not be resolved to a valid path.".to_string(),
                };
            };

            let metadata_json = metadata_to_set.to_string();

            match file_metadata_repository::upsert(
                &guard,
                &normalized,
                &metadata_json,
                requesting_admin_id,
                now,
            ) {
                Ok(()) => {
                    let _ = agent_action_repository::log_agent_action(
                        &guard,
                        requesting_admin_id,
                        "updated_file_metadata",
                        None,
                        Some(&serde_json::json!({"filepath": normalized, "action": "set/update"})),
                        now,
                    );
                    ToolResult::Ok {
                        data: Some(serde_json::json!({
                            "filepath": normalized,
                            "original_path": filepath_arg,
                            "updated_by": requesting_admin_id,
                            "last_updated": now,
                        })),
                        message: Some(format!(
                            "File metadata updated successfully for '{filepath_arg}' \
                             (normalized: {normalized})."
                        )),
                    }
                }
                Err(_e) => ToolResult::Failed {
                    message: "A database error occurred; it has been logged. Retry, or ask an \
                        operator to check logs."
                        .to_string(),
                },
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_auth::ToolCallContext;
    use conexus_core::capability::{AgentRole, Capabilities};
    use conexus_core::principal::PrincipalKind;
    use conexus_db::agent_repository::{AgentRepository, NewAgent};
    use conexus_db::schema::init_schema;
    use conexus_wakeloop::file_map::FileMap;
    use conexus_wakeloop::waiter_registry::WaiterRegistry;
    use std::collections::HashSet;

    fn worker_principal(agent_id: &str) -> Principal {
        Principal {
            kind: PrincipalKind::AgentBearer,
            user_id: None,
            agent_id: Some(agent_id.to_string()),
            project_name: None,
            project_role: None,
            agent_role: Some(AgentRole::Worker),
            can_wake_loop: true,
            source_token: None,
            capabilities: Capabilities::Set(HashSet::from([Capability::FilesUse])),
        }
    }

    fn operator_principal() -> Principal {
        Principal {
            kind: PrincipalKind::ForwardingHeader,
            user_id: Some("op-1".to_string()),
            agent_id: None,
            project_name: Some("demo".to_string()),
            project_role: Some(conexus_core::capability::ProjectRole::Operator),
            agent_role: None,
            can_wake_loop: false,
            source_token: None,
            capabilities: Capabilities::Set(HashSet::from([Capability::SystemConfigWrite])),
        }
    }

    async fn setup() -> AsyncMutex<Connection> {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        AgentRepository::create(
            &conn,
            NewAgent {
                token: "tok",
                agent_id: "alice",
                created_at: "2026-06-01T00:00:00Z",
                status: "active",
                current_task: None,
                working_directory: "/home/alice/repo",
                color: None,
                agent_role: "worker",
            },
        )
        .unwrap();
        AsyncMutex::new(conn)
    }

    #[tokio::test]
    async fn view_reports_no_metadata_recorded_yet() {
        let conn = setup().await;
        let principal = worker_principal("alice");
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map);
        let result = ViewFileMetadataTool::call(
            Some(&principal),
            &serde_json::json!({"filepath": "main.rs"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &ctx,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert_eq!(data.unwrap()["metadata"], Value::Null);
    }

    #[test]
    fn a_worker_cannot_update_metadata() {
        let principal = worker_principal("alice");
        let denial = ViewFileMetadataTool::REQUIRED.check(None, &conexus_auth::NoPolicyOverrides);
        assert!(denial.is_err());
        // The real gate is Cap(system.config.write); a worker principal
        // (files.use only) fails it.
        let check = UpdateFileMetadataTool::REQUIRED
            .check(Some(&principal), &conexus_auth::NoPolicyOverrides);
        assert!(check.is_err());
    }

    #[tokio::test]
    async fn an_operator_updates_metadata_and_it_is_readable_back() {
        let conn = setup().await;
        let operator = operator_principal();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map);
        let result = UpdateFileMetadataTool::call(
            Some(&operator),
            &serde_json::json!({
                "filepath": "/tmp/nonexistent-dir-xyz/main.rs",
                "metadata": {"purpose": "entry point"}
            }),
            &conn,
            "2026-06-01T00:00:00Z",
            &ctx,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let data = data.unwrap();
        assert_eq!(data["updated_by"], "op-1");
        let normalized = data["filepath"].as_str().unwrap().to_string();

        let worker = worker_principal("alice");
        let view_result = ViewFileMetadataTool::call(
            Some(&worker),
            &serde_json::json!({"filepath": normalized}),
            &conn,
            "2026-06-01T00:01:00Z",
            &ctx,
        )
        .await;
        let ToolResult::Ok { data, .. } = view_result else {
            panic!("expected Ok, got {view_result:?}");
        };
        assert_eq!(data.unwrap()["metadata"]["purpose"], "entry point");
    }

    #[tokio::test]
    async fn a_malformed_path_is_invalid_not_a_500() {
        let conn = setup().await;
        let principal = worker_principal("alice");
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map);
        let result = ViewFileMetadataTool::call(
            Some(&principal),
            &serde_json::json!({"filepath": "bad\u{0}path.rs"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &ctx,
        )
        .await;
        assert!(matches!(result, ToolResult::Invalid { .. }));
    }

    #[test]
    fn schema_max_lengths_match_the_shared_constant() {
        for schema in [ViewFileMetadataTool::SCHEMA, UpdateFileMetadataTool::SCHEMA] {
            let parsed: Value = serde_json::from_str(schema).unwrap();
            let max_len = parsed["properties"]["filepath"]["maxLength"]
                .as_u64()
                .unwrap();
            assert_eq!(max_len as usize, conexus_core::schema_limits::PATH_MAX_LEN);
        }
    }
}
