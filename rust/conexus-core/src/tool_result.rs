//! `ToolResult` — the typed return shape of a tool implementation.
//!
//! Faithful port of `agent_mcp/core/tool_result.py` (the migration plan's
//! own pick for "the single most Rust-native pattern already in the
//! codebase" — a Python `Union` of frozen dataclasses, matched via
//! `isinstance()` in three separate renderers, becomes a native `enum`
//! with exhaustive `match`). Kept dependency-free (only `serde_json` for
//! the `data`/body payload shape) per `conexus-core`'s "pure domain
//! types, zero I/O" role — `render_as_text_content` returns plain
//! `Vec<String>` text blocks rather than an MCP `ContentBlock`, so this
//! crate never needs an `rmcp` dependency; `conexus-mcp` does the
//! trivial `ContentBlock::text(s)` wrap at the wire boundary.
//!
//! Variant semantics mirror the Python docstring exactly:
//! - `Ok` — success. `data` is the payload (JSON-serialized for the
//!   wire); `message` is an optional human-readable summary. When BOTH
//!   are set, the MCP-wire renderer emits two blocks (message first,
//!   data second) — the "message wins, data dropped" bug this fixes is
//!   documented in the Python source's own history.
//! - `NotFound` — REST 404. `hint` is a static (never resource-owner-
//!   interpolated) clause a caller can fuse onto the phantom-404 to add
//!   an actionable note without a second variant (see PF-1 / the
//!   existence-oracle callers in the Python source).
//! - `PermissionDenied` — REST 403, never 401 (401 is reserved for
//!   missing/invalid credentials, resolved upstream of dispatch).
//! - `Invalid` — REST 400. `field` names the offending input when one
//!   can be named.
//! - `Conflict` — REST 409 (uniqueness/state-invariant violation).
//! - `Failed` — REST 500. `message` is treated as INTERNAL (SEC-R8-1):
//!   several call sites build it from a caught DB error, so it can
//!   embed table/column names, paths — never render it verbatim to a
//!   caller; log it server-side, render a static generic string.

use serde_json::Value;

#[derive(Debug, Clone, PartialEq)]
pub enum ToolResult {
    Ok {
        data: Option<Value>,
        message: Option<String>,
    },
    NotFound {
        resource: String,
        identifier: String,
        hint: Option<String>,
    },
    PermissionDenied {
        reason: String,
    },
    Invalid {
        message: String,
        field: Option<String>,
    },
    Conflict {
        reason: String,
    },
    Failed {
        message: String,
    },
}

impl ToolResult {
    /// Whether this result represents a failure. `Ok` is the sole
    /// success variant; every other variant is an error — mirrors
    /// `is_error_result` in the Python source, which the MCP wire
    /// handler uses to set `CallToolResult.isError` so a RETURNED
    /// denial reaches the client with the same `isError=true` a RAISED
    /// exception gets (finding AS-1).
    pub fn is_error(&self) -> bool {
        !matches!(self, ToolResult::Ok { .. })
    }

    /// Render as the `Vec<String>` text-block shape the MCP wire
    /// consumes (`conexus-mcp` wraps each string in a `ContentBlock`).
    pub fn render_as_text_content(&self) -> Vec<String> {
        match self {
            ToolResult::Ok { data, message } => match (message, data) {
                (Some(m), Some(d)) => vec![m.clone(), data_to_text(d)],
                (Some(m), None) => vec![m.clone()],
                (None, Some(d)) => vec![data_to_text(d)],
                (None, None) => vec![String::new()],
            },
            ToolResult::NotFound {
                resource,
                identifier,
                hint,
            } => {
                let tail = hint.as_deref().unwrap_or(".");
                vec![format!("Error: {resource} {identifier:?} not found{tail}")]
            }
            ToolResult::PermissionDenied { reason } => {
                vec![format!("Unauthorized: {reason}")]
            }
            ToolResult::Invalid { message, field } => {
                vec![match field {
                    Some(f) => format!("Error: invalid {f}: {message}"),
                    None => format!("Error: invalid input: {message}"),
                }]
            }
            ToolResult::Conflict { reason } => {
                vec![format!("Error: conflict: {reason}")]
            }
            ToolResult::Failed { message } => {
                // SEC-R8-1: never render the internal message verbatim —
                // the caller is responsible for logging `message` (this
                // crate has no I/O, so it can't log itself).
                let _ = message;
                vec!["Error: Operation failed".to_string()]
            }
        }
    }

    /// The single `(http_status, json_body)` mapping every REST
    /// consumer shares — the `_STATUS_BY_VARIANT`/`tool_result_to_http`
    /// pair from the Python source, unified into one method since Rust
    /// has no separate "just the status" lookup need callers reach for
    /// independently the way the Python dict was.
    pub fn to_http(&self) -> (u16, Value) {
        match self {
            ToolResult::Ok { data, message } => {
                let mut body = serde_json::json!({
                    "success": true,
                    "message": message.clone().unwrap_or_default(),
                });
                if let Some(d) = data {
                    body["data"] = d.clone();
                }
                (200, body)
            }
            ToolResult::NotFound {
                resource,
                identifier,
                hint,
            } => {
                let tail = hint.as_deref().unwrap_or(".");
                let text = format!("{resource} {identifier:?} not found{tail}");
                (
                    404,
                    serde_json::json!({
                        "success": false,
                        "error": "not_found",
                        "resource": resource,
                        "identifier": identifier,
                        "message": text,
                    }),
                )
            }
            ToolResult::PermissionDenied { reason } => (
                403,
                serde_json::json!({
                    "success": false,
                    "error": "permission_denied",
                    "reason": reason,
                    "message": reason,
                }),
            ),
            ToolResult::Invalid { message, field } => (
                400,
                serde_json::json!({
                    "success": false,
                    "error": "invalid",
                    "field": field,
                    "message": message,
                }),
            ),
            ToolResult::Conflict { reason } => (
                409,
                serde_json::json!({
                    "success": false,
                    "error": "conflict",
                    "reason": reason,
                    "message": reason,
                }),
            ),
            ToolResult::Failed { message } => {
                // SEC-R8-1: static generic message only; caller logs the
                // real `message` server-side.
                let _ = message;
                (
                    500,
                    serde_json::json!({
                        "success": false,
                        "error": "failed",
                        "message": "Operation failed",
                    }),
                )
            }
        }
    }

    /// The legacy `{"error": ...}` envelope's error-detail string —
    /// port of `tool_result_error_message`. Pairs with [`Self::to_http`],
    /// which supplies the status; this supplies the body wording several
    /// REST routes pin exact strings against.
    pub fn error_message(&self, fallback: &str, not_found_label: Option<&str>) -> String {
        match self {
            ToolResult::NotFound {
                resource,
                identifier,
                hint,
            } => {
                let label = not_found_label.unwrap_or(resource);
                format!(
                    "{label} '{identifier}' not found{}",
                    hint.as_deref().unwrap_or("")
                )
            }
            ToolResult::Conflict { reason } | ToolResult::PermissionDenied { reason } => {
                reason.clone()
            }
            ToolResult::Invalid { message, .. } => message.clone(),
            _ => fallback.to_string(),
        }
    }
}

/// Render `data` as text for the MCP-wire success path. A string value
/// passes through verbatim (JSON-quoting it would wrap it in extra
/// quotes and waste a parse on the client); anything else is
/// JSON-serialized.
fn data_to_text(d: &Value) -> String {
    if let Value::String(s) = d {
        s.clone()
    } else {
        serde_json::to_string(d).unwrap_or_else(|_| d.to_string())
    }
}

#[cfg(test)]
mod tests {
    use crate::tool_result::ToolResult;
    use serde_json::json;

    // ── Ok rendering (render_as_text_content) ──────────────────────

    #[test]
    fn ok_with_message_and_data_renders_two_blocks() {
        let r = ToolResult::Ok {
            data: Some(json!({"token": "abc123"})),
            message: Some("Agent registered.".to_string()),
        };
        let blocks = r.render_as_text_content();
        assert_eq!(blocks.len(), 2);
        assert_eq!(blocks[0], "Agent registered.");
        assert_eq!(blocks[1], r#"{"token":"abc123"}"#);
    }

    #[test]
    fn ok_with_message_only_renders_one_block() {
        let r = ToolResult::Ok {
            data: None,
            message: Some("done".to_string()),
        };
        assert_eq!(r.render_as_text_content(), vec!["done".to_string()]);
    }

    #[test]
    fn ok_with_string_data_passes_through_verbatim_not_double_encoded() {
        let r = ToolResult::Ok {
            data: Some(json!("raw string payload")),
            message: None,
        };
        // A string data value must NOT be JSON-quoted — passed through
        // verbatim, matching the Python renderer's `_data_to_text`.
        assert_eq!(
            r.render_as_text_content(),
            vec!["raw string payload".to_string()]
        );
    }

    #[test]
    fn ok_with_non_string_data_only_renders_json() {
        let r = ToolResult::Ok {
            data: Some(json!({"a": 1})),
            message: None,
        };
        assert_eq!(r.render_as_text_content(), vec![r#"{"a":1}"#.to_string()]);
    }

    #[test]
    fn ok_with_neither_renders_one_empty_block() {
        let r = ToolResult::Ok {
            data: None,
            message: None,
        };
        assert_eq!(r.render_as_text_content(), vec!["".to_string()]);
    }

    // ── Error rendering (render_as_text_content) ───────────────────

    #[test]
    fn not_found_renders_without_hint() {
        let r = ToolResult::NotFound {
            resource: "task".to_string(),
            identifier: "task_123".to_string(),
            hint: None,
        };
        assert_eq!(
            r.render_as_text_content(),
            vec!["Error: task \"task_123\" not found.".to_string()]
        );
    }

    #[test]
    fn not_found_renders_with_hint_replacing_trailing_period() {
        let r = ToolResult::NotFound {
            resource: "task".to_string(),
            identifier: "task_123".to_string(),
            hint: Some(", or you are not its author.".to_string()),
        };
        assert_eq!(
            r.render_as_text_content(),
            vec!["Error: task \"task_123\" not found, or you are not its author.".to_string()]
        );
    }

    #[test]
    fn permission_denied_renders_unauthorized_prefix() {
        let r = ToolResult::PermissionDenied {
            reason: "capability task.assign required".to_string(),
        };
        assert_eq!(
            r.render_as_text_content(),
            vec!["Unauthorized: capability task.assign required".to_string()]
        );
    }

    #[test]
    fn invalid_with_field_names_the_field() {
        let r = ToolResult::Invalid {
            message: "must be a string".to_string(),
            field: Some("task_id".to_string()),
        };
        assert_eq!(
            r.render_as_text_content(),
            vec!["Error: invalid task_id: must be a string".to_string()]
        );
    }

    #[test]
    fn invalid_without_field_uses_generic_wording() {
        let r = ToolResult::Invalid {
            message: "cross-field mismatch".to_string(),
            field: None,
        };
        assert_eq!(
            r.render_as_text_content(),
            vec!["Error: invalid input: cross-field mismatch".to_string()]
        );
    }

    #[test]
    fn conflict_renders_conflict_prefix() {
        let r = ToolResult::Conflict {
            reason: "status transition not allowed".to_string(),
        };
        assert_eq!(
            r.render_as_text_content(),
            vec!["Error: conflict: status transition not allowed".to_string()]
        );
    }

    #[test]
    fn failed_never_leaks_the_internal_message_on_the_wire() {
        // SEC-R8-1: Failed.message can embed table/column names, paths,
        // internals (built from a caught DB error) — the wire text must
        // be a static generic string, never the raw message.
        let r = ToolResult::Failed {
            message: "sqlite3.OperationalError: no such table: rag_chunks".to_string(),
        };
        let blocks = r.render_as_text_content();
        assert_eq!(blocks, vec!["Error: Operation failed".to_string()]);
        assert!(!blocks[0].contains("sqlite3"));
        assert!(!blocks[0].contains("rag_chunks"));
    }

    // ── is_error ─────────────────────────────────────────────────────

    #[test]
    fn only_ok_is_not_an_error() {
        assert!(!ToolResult::Ok {
            data: None,
            message: None
        }
        .is_error());
        assert!(ToolResult::NotFound {
            resource: "x".into(),
            identifier: "y".into(),
            hint: None
        }
        .is_error());
        assert!(ToolResult::PermissionDenied { reason: "x".into() }.is_error());
        assert!(ToolResult::Invalid {
            message: "x".into(),
            field: None
        }
        .is_error());
        assert!(ToolResult::Conflict { reason: "x".into() }.is_error());
        assert!(ToolResult::Failed {
            message: "x".into()
        }
        .is_error());
    }

    // ── HTTP status + body mapping (to_http) ────────────────────────

    #[test]
    fn to_http_status_codes_match_the_locked_tiebreak_table() {
        // PermissionDenied -> 403, NOT 401 (401 is reserved for missing/
        // invalid credentials, resolved upstream of dispatch).
        assert_eq!(
            ToolResult::Ok {
                data: None,
                message: None
            }
            .to_http()
            .0,
            200
        );
        assert_eq!(
            ToolResult::NotFound {
                resource: "x".into(),
                identifier: "y".into(),
                hint: None
            }
            .to_http()
            .0,
            404
        );
        assert_eq!(
            ToolResult::PermissionDenied { reason: "x".into() }
                .to_http()
                .0,
            403
        );
        assert_eq!(
            ToolResult::Invalid {
                message: "x".into(),
                field: None
            }
            .to_http()
            .0,
            400
        );
        assert_eq!(ToolResult::Conflict { reason: "x".into() }.to_http().0, 409);
        assert_eq!(
            ToolResult::Failed {
                message: "x".into()
            }
            .to_http()
            .0,
            500
        );
    }

    #[test]
    fn to_http_ok_body_includes_data_only_when_present() {
        let (_, body) = ToolResult::Ok {
            data: Some(json!({"k": "v"})),
            message: Some("done".into()),
        }
        .to_http();
        assert_eq!(body["success"], json!(true));
        assert_eq!(body["message"], json!("done"));
        assert_eq!(body["data"], json!({"k": "v"}));

        let (_, body_no_data) = ToolResult::Ok {
            data: None,
            message: Some("done".into()),
        }
        .to_http();
        assert!(body_no_data.get("data").is_none());
    }

    #[test]
    fn to_http_not_found_body_shape() {
        let (_, body) = ToolResult::NotFound {
            resource: "task".into(),
            identifier: "task_1".into(),
            hint: None,
        }
        .to_http();
        assert_eq!(body["success"], json!(false));
        assert_eq!(body["error"], json!("not_found"));
        assert_eq!(body["resource"], json!("task"));
        assert_eq!(body["identifier"], json!("task_1"));
    }

    #[test]
    fn to_http_failed_body_never_leaks_internal_message() {
        let (_, body) = ToolResult::Failed {
            message: "sqlite3.OperationalError: leaked internals".into(),
        }
        .to_http();
        assert_eq!(body["message"], json!("Operation failed"));
    }

    // ── Legacy error_message renderer ───────────────────────────────

    #[test]
    fn error_message_not_found_uses_default_label_or_override() {
        let r = ToolResult::NotFound {
            resource: "agent".into(),
            identifier: "alice".into(),
            hint: None,
        };
        assert_eq!(
            r.error_message("Operation failed", None),
            "agent 'alice' not found"
        );
        assert_eq!(
            r.error_message("Operation failed", Some("Agent")),
            "Agent 'alice' not found"
        );
    }

    #[test]
    fn error_message_conflict_and_permission_denied_use_reason() {
        assert_eq!(
            ToolResult::Conflict {
                reason: "dup".into()
            }
            .error_message("fallback", None),
            "dup"
        );
        assert_eq!(
            ToolResult::PermissionDenied {
                reason: "denied".into()
            }
            .error_message("fallback", None),
            "denied"
        );
    }

    #[test]
    fn error_message_invalid_uses_message() {
        assert_eq!(
            ToolResult::Invalid {
                message: "bad input".into(),
                field: None
            }
            .error_message("fallback", None),
            "bad input"
        );
    }

    #[test]
    fn error_message_failed_uses_fallback_not_internal_message() {
        assert_eq!(
            ToolResult::Failed {
                message: "internal db error".into()
            }
            .error_message("Operation failed", None),
            "Operation failed"
        );
    }
}
