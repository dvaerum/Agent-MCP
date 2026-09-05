//! `/api` REST handlers (Phase E1, prancy-napping-pie). PR 2/14:
//! `/api/prompts/catalog` and `/api/settings-schema` -- the first two
//! real endpoints, chosen because neither touches the DB (zero
//! mutation risk) and together they exercise both halves of the
//! `/api` mount: `prompts/catalog` is genuinely UNAUTHENTICATED
//! (Python's own docstring: "the router-level gate is deferred to a
//! follow-up PR" -- one of the 3 confirmed no-auth-by-design REST
//! endpoints), `settings-schema` requires the `rest_gate` door.

use std::sync::{Arc, LazyLock};

use axum::body::Bytes;
use axum::extract::{Extension, Path, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use rusqlite::OptionalExtension;
use serde_json::{json, Value};

use conexus_core::settings_schema::SETTINGS_SCHEMA;
use conexus_core::tool_result::ToolResult;
use conexus_tools::project_context_tools::{
    has_unsafe_unicode_for_identifier, is_valid_memory_key,
};

use crate::json_sanitize::decode_untrusted_body;
use crate::rest_gate::ResolvedRestPrincipal;
use crate::rest_principal::RestPrincipal;
use crate::server::{dispatch_rest_tool, SharedState};

/// Port of `agent_mcp/utils/string_utils.py::UNSAFE_KEY_ERROR`.
fn unsafe_key_error() -> serde_json::Value {
    json!({
        "error": "invalid_key_character",
        "message": "Memory key contains a disallowed character \
            (Unicode control / bidi-override / invisible). \
            Allowed: printable Unicode except \
            U+0000-U+001F, U+007F, \
            U+200B-U+200F, U+2028-U+2029, U+202A-U+202E, \
            U+2060-U+2064, U+2066-U+2069, U+206A-U+206F, U+FEFF.",
    })
}

/// Port of `agent_mcp/utils/string_utils.py::MEMORY_KEY_ERROR`.
fn memory_key_error() -> serde_json::Value {
    json!({
        "error": "invalid_key_character",
        "message": "Memory key may contain only letters, digits, and . _ / - \
            (A-Z a-z 0-9 . _ / -).",
    })
}

/// Port of `agent_mcp/app/routers/_wire_validation.py::require_str`:
/// 400 iff `value` is present (`Some`) but not a JSON string. Absent
/// (`None`) is allowed -- callers check presence/truthiness
/// separately.
fn require_str(value: Option<&serde_json::Value>, field: &str) -> Option<Response> {
    match value {
        // A JSON `null` and an absent key are the SAME thing here,
        // matching Python's `data.get(field)` -- both a missing key and
        // an explicit `"field": null` decode to Python `None`, and
        // `_require_str` only rejects `value is not None and not
        // isinstance(value, str)`. Treating `Value::Null` as present-
        // and-wrong-typed (an earlier draft of this function did) would
        // 400 a caller's explicit `null` for an optional field, and
        // ALSO 400 any handler that defaults an absent field to
        // `Value::Null` before calling this check.
        Some(v) if !v.is_string() && !v.is_null() => Some(
            (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": format!("{field} must be a string")})),
            )
                .into_response(),
        ),
        _ => None,
    }
}

/// `GET /api/prompts/catalog` -- the raw Prompt Book catalogue, served
/// verbatim with no visibility filtering (unlike `conexus_tools::
/// prompts::list_visible`, which gates on `CatalogRole` for the MCP
/// `prompts/list` surface). Genuinely unauthenticated, matching
/// Python's `prompts_catalog_api_route` exactly -- mounted on the
/// no-auth half of the `/api` router, not behind `rest_gate`.
pub async fn prompts_catalog() -> Response {
    // Serve the embedded text directly rather than round-tripping
    // through `serde_json::Value` -- it's already valid JSON (the
    // same file `conexus_tools::prompts` parses at startup), and this
    // matches Python's `JSONResponse(load_catalog())` byte-for-byte
    // (the raw file content, not a re-serialized copy that could
    // reorder keys or reformat whitespace differently).
    (
        [(axum::http::header::CONTENT_TYPE, "application/json")],
        conexus_tools::prompts::raw_catalog_json(),
    )
        .into_response()
}

/// `GET /api/settings-schema` -- the settings-schema registry plus the
/// caller's own tier flags, for the dashboard's Settings page to
/// render policy toggles and know whether it may show secret values.
/// Behind `rest_gate` (operator-tier only, matching Python's
/// `Depends(require_operator_session)` + inline confirmed-tier gate).
pub async fn settings_schema(Extension(resolved): Extension<ResolvedRestPrincipal>) -> Response {
    // BUG FIX (found while researching PR 7): Python's real handler
    // 403s the WHOLE response for a non-confirmed caller -- this gate
    // was missing here since this endpoint's first PR (#852), which
    // only surfaced `confirmed_operator` as an informational response
    // field without ever checking it. A forwarding-header caller
    // (never confirmed on REST, by PR 1's own design) could read the
    // schema when Python would reject it. Reproduced live before this
    // fix (200 for a forwarding-operator caller), confirmed 403 after.
    if !resolved.confirmed_operator_tier {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({
                "error": "forbidden",
                "message": "The settings schema is operator-tier only. Use an \
                    operator-tier session or bearer to read it.",
            })),
        )
            .into_response();
    }
    let schema: Vec<_> = SETTINGS_SCHEMA
        .iter()
        .map(|s| {
            serde_json::json!({
                "key": s.key,
                "type": s.r#type,
                "default": s.default,
                "tier": s.tier,
                "group": s.group,
                "title": s.title,
                "description": s.description,
                "widget": s.widget,
            })
        })
        .collect();
    Json(serde_json::json!({
        "schema": schema,
        "caller": {
            // Always `false`: neither remaining REST door (forwarding
            // header, operator-tier bearer) ever resolves a sysadmin
            // identity -- the dropped session-cookie door was the ONLY
            // one that could (via `group_resolver.resolve_user_is_sysadmin`).
            // Represented as a literal, not a stubbed field, since it's
            // the actually-correct value for every caller this backend
            // can now admit.
            "sysadmin": false,
            "confirmed_operator": resolved.confirmed_operator_tier,
        },
    }))
    .into_response()
}

// -- /api/memories (Phase E1 PR 4/14, conexus-rest-memories) --------

/// `POST /api/memories` -- thin adapter over `create_project_context`,
/// matching `agent_mcp/app/routers/memories.py::create_memory_api_route`.
/// R9-F2 (pentest): dispatches through the gated MCP tool rather than
/// writing the table directly, so the tool-layer authorization gates
/// (viewer-tier write guard, per-key creator-ownership matrix) apply
/// to this REST surface too -- ONE enforcement path, not a second
/// bypassable one.
pub async fn create_memory(
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
    body: Bytes,
) -> Response {
    let data = match decode_untrusted_body(&body) {
        Ok(d) => d,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": e.to_string()})),
            )
                .into_response()
        }
    };

    let context_key_value = data.get("context_key");
    let context_value = data
        .get("context_value")
        .cloned()
        .unwrap_or(serde_json::Value::Null);
    let description = data.get("description");

    // Matches Python's `if not context_key: ...` -- a JSON-truthiness
    // check (missing / null / false / 0 / "" / [] / {}), checked
    // BEFORE the type guard below, so e.g. a bare `0` hits "required"
    // the same way Python's `not 0` does, while a non-empty non-string
    // value (a number, a list) falls through to the type guard instead
    // -- collapsing these two checks into one (as an earlier draft of
    // this handler did) would misreport a wrong-typed-but-truthy value
    // as "required" instead of "must be a string".
    if is_json_falsy(context_key_value) {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": "context_key is required"})),
        )
            .into_response();
    }
    if let Some(resp) = require_str(context_key_value, "context_key") {
        return resp;
    }
    if let Some(resp) = require_str(description, "description") {
        return resp;
    }
    // Safe: `require_str` above already confirmed this is a string.
    let context_key = context_key_value.and_then(|v| v.as_str()).unwrap();

    // NOTE, verified live + against `tests/test_memories_unsafe_unicode_key.py`:
    // this check is effectively a no-op HERE (not dead code to delete --
    // a documented, tested contract). `context_key` arrived through
    // `decode_untrusted_body` above, which already silently stripped
    // every hidden-format/control character this denylist checks for
    // (R13-F2/R14-F3) -- a JSON-body request carrying e.g. an RTL
    // override key succeeds with the SANITIZED key stored, it never
    // 400s here. The check earns its keep on `update_memory`'s PATH
    // parameter below instead, which is never routed through the JSON
    // sanitizer at all -- that's the one path where a raw disallowed
    // character can still reach this validator intact.
    if has_unsafe_unicode_for_identifier(context_key) {
        return (StatusCode::BAD_REQUEST, Json(unsafe_key_error())).into_response();
    }
    if !is_valid_memory_key(context_key) {
        return (StatusCode::BAD_REQUEST, Json(memory_key_error())).into_response();
    }

    let arguments = json!({
        "context_key": context_key,
        "context_value": context_value,
        "description": description,
    });
    let principal = resolved.dispatch_principal.clone();
    let result = match dispatch_rest_tool(
        &shared,
        "create_project_context",
        arguments,
        Some(&principal),
    )
    .await
    {
        Ok(r) => r,
        Err(()) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "Failed to create memory"})),
            )
                .into_response()
        }
    };

    render_memory_result(
        result,
        || format!("Memory '{context_key}' created successfully"),
        "Failed to create memory",
        None,
    )
}

/// `PUT /api/memories/{context_key}` -- thin adapter over
/// `update_project_context`, matching
/// `agent_mcp/app/routers/memories.py::update_memory_api_route`.
pub async fn update_memory(
    Path(context_key): Path<String>,
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
    body: Bytes,
) -> Response {
    // Unlike `create_memory`'s identical-looking check, this one is
    // LIVE: `context_key` here is an axum `Path` extraction (URL-
    // decoded by axum, never passed through `decode_untrusted_body`),
    // so a raw disallowed character (e.g. a URL-encoded RTL override)
    // reaches this validator intact -- verified live against
    // `tests/test_update_memory_rejects_unsafe_unicode_key_in_url`'s
    // exact payload, which 400s here, not silently sanitized.
    if has_unsafe_unicode_for_identifier(&context_key) {
        return (StatusCode::BAD_REQUEST, Json(unsafe_key_error())).into_response();
    }
    if !is_valid_memory_key(&context_key) {
        return (StatusCode::BAD_REQUEST, Json(memory_key_error())).into_response();
    }

    let data = match decode_untrusted_body(&body) {
        Ok(d) => d,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": e.to_string()})),
            )
                .into_response()
        }
    };

    let context_value = data
        .get("context_value")
        .cloned()
        .unwrap_or(serde_json::Value::Null);
    let description = data.get("description");

    if let Some(resp) = require_str(description, "description") {
        return resp;
    }

    // BL-R22-1: only thread `description` when the caller supplied it,
    // so the tool's partial-update semantics preserve an existing
    // description when this field is omitted entirely.
    let mut arguments = json!({
        "context_key": context_key,
        "context_value": context_value,
    });
    if let Some(desc) = description {
        arguments["description"] = desc.clone();
    }

    let principal = resolved.dispatch_principal.clone();
    let result = match dispatch_rest_tool(
        &shared,
        "update_project_context",
        arguments,
        Some(&principal),
    )
    .await
    {
        Ok(r) => r,
        Err(()) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "Failed to update memory"})),
            )
                .into_response()
        }
    };

    render_memory_result(
        result,
        || format!("Memory '{context_key}' updated successfully"),
        "Failed to update memory",
        None,
    )
}

/// `DELETE /api/memories/{context_key}` -- thin adapter over
/// `delete_project_context`, matching
/// `agent_mcp/app/routers/memories.py::delete_memory_api_route`.
/// `force_delete` is read from the JSON body (default `false`); an
/// empty/absent body is treated as `{}` here (Python:
/// `bool(data.get("force_delete", False)) if isinstance(data, dict)
/// else False` -- this handler's body is ALWAYS a dict once
/// `decode_untrusted_body` accepts it, so the `isinstance` branch is
/// unreachable in practice, matching the note left in PR2's
/// `settings_schema` module about literal-not-stubbed values).
pub async fn delete_memory(
    Path(context_key): Path<String>,
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
    body: Bytes,
) -> Response {
    // DELETE commonly carries no body at all; an empty body is treated
    // as "no force_delete" rather than the 400 create/update would give
    // -- matches Python's own `isinstance(data, dict) else False`
    // fallback, which never actually raises here since a malformed body
    // simply degrades to `force_delete=False`, not a rejected request.
    let force_delete = if body.is_empty() {
        false
    } else {
        match decode_untrusted_body(&body) {
            Ok(data) => data
                .get("force_delete")
                .and_then(|v| v.as_bool())
                .unwrap_or(false),
            Err(_) => false,
        }
    };

    let arguments = json!({
        "context_key": context_key,
        "force_delete": force_delete,
    });
    let principal = resolved.dispatch_principal.clone();
    let result = match dispatch_rest_tool(
        &shared,
        "delete_project_context",
        arguments,
        Some(&principal),
    )
    .await
    {
        Ok(r) => r,
        Err(()) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "Failed to delete memory"})),
            )
                .into_response()
        }
    };

    render_memory_result(
        result,
        || format!("Memory '{context_key}' deleted successfully"),
        "Failed to delete memory",
        Some("Memory"),
    )
}

/// Shared success/error envelope for the 3 memory handlers above --
/// `{"success": true, "message": ...}` on `Ok` (custom message
/// always, matching Python's own per-handler `f"Memory '{key}' ...
/// successfully"` literals, not the tool's own `Ok.message`), or
/// `{"error": ...}` with the status `ToolResult::to_http` assigns and
/// the wording `ToolResult::error_message` assigns, on every other
/// variant. This is Python's OWN bespoke envelope for this router
/// (thinner than the generic `_dispatch_through_tool` shape other
/// routers use -- no `data` field, a fixed success message) -- not a
/// blanket call to `to_http()`.
fn render_memory_result(
    result: ToolResult,
    success_message: impl FnOnce() -> String,
    fallback: &str,
    not_found_label: Option<&str>,
) -> Response {
    if matches!(result, ToolResult::Ok { .. }) {
        return Json(json!({"success": true, "message": success_message()})).into_response();
    }
    let (status, _) = result.to_http();
    let message = result.error_message(fallback, not_found_label);
    (
        StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        Json(json!({"error": message})),
    )
        .into_response()
}

/// Python `if not value:` truthiness over a JSON value pulled from a
/// decoded request body -- `None` (missing key), `null`, `false`,
/// `0`/`0.0`, `""`, `[]`, and `{}` are all falsy; everything else
/// (including a non-empty string/array/object, or any nonzero number)
/// is truthy. Needed because `create_memory`'s "is this field present
/// at all" check is a truthiness check in Python, not a presence
/// check -- see that call site for why collapsing it with the
/// type-guard below would misreport a wrong-typed-but-truthy value.
fn is_json_falsy(value: Option<&serde_json::Value>) -> bool {
    match value {
        None => true,
        Some(serde_json::Value::Null) => true,
        Some(serde_json::Value::Bool(b)) => !b,
        Some(serde_json::Value::String(s)) => s.is_empty(),
        Some(serde_json::Value::Number(n)) => n.as_f64() == Some(0.0),
        Some(serde_json::Value::Array(a)) => a.is_empty(),
        Some(serde_json::Value::Object(o)) => o.is_empty(),
    }
}

// -- /api/schedules (Phase E1 PR 5/14, conexus-rest-schedules) ------

/// `GET /api/schedules` -- every schedule across the project's
/// agents, matching `agent_mcp/app/routers/schedules.py::
/// list_schedules_api_route`. Reads the repository directly (an
/// operator-only, cross-agent, UNSCOPED view -- deliberately not
/// dispatched through the MCP `list_scheduled_directives` tool, which
/// is scoped to the caller's own schedules; reuses that tool's
/// `serialize()` for an identical row shape, nothing else).
pub async fn list_schedules(State(shared): State<Arc<SharedState>>) -> Response {
    let guard = shared.conn.lock().await;
    let rows = match conexus_db::scheduled_directive_repository::list_all(&guard) {
        Ok(rows) => rows,
        Err(_) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "Failed to list schedules"})),
            )
                .into_response()
        }
    };
    drop(guard);
    let schedules: Vec<_> = rows
        .iter()
        .map(conexus_tools::scheduled_directive_tools::serialize)
        .collect();
    Json(json!({"schedules": schedules})).into_response()
}

/// `POST /api/schedules` -- operator creates a schedule for any agent.
/// Matches `create_schedule_api_route`: the whole decoded body is
/// threaded straight through as the tool's arguments.
pub async fn create_schedule(
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
    body: Bytes,
) -> Response {
    let data = match decode_untrusted_body(&body) {
        Ok(d) => d,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": e.to_string()})),
            )
                .into_response()
        }
    };
    let principal = resolved.dispatch_principal.clone();
    dispatch_schedule_tool(
        &shared,
        "create_scheduled_directive",
        Value::Object(data),
        &principal,
        "Failed to create schedule",
    )
    .await
}

/// `PUT /api/schedules/{directive_id}` -- edit / pause / resume.
/// Matches `update_schedule_api_route`: the decoded body plus the
/// path's `directive_id` become the tool's arguments.
pub async fn update_schedule(
    Path(directive_id): Path<String>,
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
    body: Bytes,
) -> Response {
    let mut data = match decode_untrusted_body(&body) {
        Ok(d) => d,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": e.to_string()})),
            )
                .into_response()
        }
    };
    data.insert("directive_id".to_string(), Value::String(directive_id));
    let principal = resolved.dispatch_principal.clone();
    dispatch_schedule_tool(
        &shared,
        "update_scheduled_directive",
        Value::Object(data),
        &principal,
        "Failed to update schedule",
    )
    .await
}

/// `DELETE /api/schedules/{directive_id}` -- remove a schedule
/// permanently. Matches `delete_schedule_api_route`: no body, just
/// the path's `directive_id`.
pub async fn delete_schedule(
    Path(directive_id): Path<String>,
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
) -> Response {
    let principal = resolved.dispatch_principal.clone();
    let arguments = json!({"directive_id": directive_id});
    dispatch_schedule_tool(
        &shared,
        "delete_scheduled_directive",
        arguments,
        &principal,
        "Failed to delete schedule",
    )
    .await
}

/// Shared dispatch + envelope for the 3 mutating `/api/schedules`
/// handlers above. Matches `_tool_result_to_response`: `Ok` ->
/// `{"success": true, ...result.data}` (the tool's OWN data spread at
/// the top level -- `{"directive": {...}}` for create/update,
/// `{"deleted": id}` for delete; NOT the bespoke fixed-message
/// envelope `/api/memories` uses -- each router keeps its own real,
/// pre-existing shape, not a shared one this migration invents).
/// Every other variant -> `to_http`'s status with `{"error": <the
/// body's own "message" field>}`, falling back to "Request rejected"
/// if that field is ever absent (never observed in practice -- every
/// `ToolResult::to_http` body always sets one).
async fn dispatch_schedule_tool(
    shared: &Arc<SharedState>,
    tool_name: &str,
    arguments: Value,
    principal: &conexus_core::principal::Principal,
    fallback_500: &str,
) -> Response {
    let result = match dispatch_rest_tool(shared, tool_name, arguments, Some(principal)).await {
        Ok(r) => r,
        Err(()) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": fallback_500})),
            )
                .into_response()
        }
    };
    if let ToolResult::Ok { data, .. } = &result {
        let mut body = json!({"success": true});
        if let Some(Value::Object(extra)) = data {
            if let Value::Object(map) = &mut body {
                map.extend(extra.clone());
            }
        }
        return Json(body).into_response();
    }
    let (status, http_body) = result.to_http();
    let message = http_body
        .get("message")
        .and_then(Value::as_str)
        .unwrap_or("Request rejected");
    (
        StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        Json(json!({"error": message})),
    )
        .into_response()
}

// -- /api/tasks (Phase E1 PR 6/14, conexus-rest-tasks) ---------------

fn task_row_to_json(row: &conexus_db::task_repository::TaskRow) -> Value {
    json!({
        "task_id": row.task_id,
        "title": row.title,
        "description": row.description,
        "assigned_to": row.assigned_to,
        "created_by": row.created_by,
        "status": row.status,
        "priority": row.priority,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "parent_task": row.parent_task,
        "child_tasks": row.child_tasks,
        "depends_on_tasks": row.depends_on_tasks,
        "notes": row.notes,
    })
}

/// `GET /api/tasks[?assigned_to=][?unassigned=][?assigned=][?status=]
/// [?created_by=][?limit=]` -- matches `all_tasks_api_route` exactly,
/// including its NO-AUTH-by-design gate (Python's own docstring: "the
/// router-level gate is deferred to a follow-up PR"). Reads a bounded
/// SQL superset (`?limit`, shared clamp), then AND-combines the
/// discovery filters in-process -- the same bound-then-filter shape
/// Python uses, not a second independent implementation.
pub async fn list_tasks(
    State(shared): State<Arc<SharedState>>,
    axum::extract::Query(params): axum::extract::Query<std::collections::HashMap<String, String>>,
) -> Response {
    let is_truthy = |key: &str| {
        params
            .get(key)
            .map(|v| matches!(v.to_lowercase().as_str(), "true" | "1" | "yes"))
            .unwrap_or(false)
    };
    let assigned_to_filter = params.get("assigned_to");
    let unassigned_filter = is_truthy("unassigned");
    let assigned_filter = is_truthy("assigned");
    let status_filter = params.get("status");
    let created_by_filter = params.get("created_by");
    let limit = crate::read_limits::clamp_section_limit(params.get("limit").map(String::as_str));

    let guard = shared.conn.lock().await;
    let candidates = match assigned_to_filter {
        Some(agent_id) => {
            conexus_db::task_repository::list_by_agent(&guard, agent_id, None, Some(limit))
        }
        None => conexus_db::task_repository::list_all(&guard, Some(limit)),
    };
    let candidates = match candidates {
        Ok(rows) => rows,
        Err(_) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "Failed to fetch all tasks"})),
            )
                .into_response()
        }
    };
    drop(guard);

    let keep = |t: &conexus_db::task_repository::TaskRow| -> bool {
        if unassigned_filter && !conexus_tools::task_query_engine::is_claimable_task(t) {
            return false;
        }
        if assigned_filter && t.assigned_to.as_deref().unwrap_or("").is_empty() {
            return false;
        }
        if let Some(cb) = created_by_filter {
            if &t.created_by != cb {
                return false;
            }
        }
        if let Some(sf) = status_filter {
            if !conexus_tools::task_tools::status_filter_matches(sf, Some(&t.status)) {
                return false;
            }
        }
        true
    };

    let tasks: Vec<_> = candidates
        .iter()
        .filter(|t| keep(t))
        .map(task_row_to_json)
        .collect();
    Json(tasks).into_response()
}

/// `POST /api/tasks` -- thin adapter over the `create_task` MCP tool,
/// matching `create_task_api_route`.
pub async fn create_task(
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
    body: Bytes,
) -> Response {
    let data = match decode_untrusted_body(&body) {
        Ok(d) => d,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": e.to_string()})),
            )
                .into_response()
        }
    };

    let raw_title = data.get("task_title");
    let description = data
        .get("task_description")
        .cloned()
        .unwrap_or(Value::String(String::new()));
    let priority = data
        .get("priority")
        .cloned()
        .unwrap_or(Value::String("medium".to_string()));
    let assigned_to = data.get("assigned_to").cloned().unwrap_or(Value::Null);
    let parent_task = data.get("parent_task").cloned().unwrap_or(Value::Null);

    for (val, name) in [
        (Some(&description), "task_description"),
        (Some(&priority), "priority"),
        (Some(&assigned_to), "assigned_to"),
        (Some(&parent_task), "parent_task"),
    ] {
        if let Some(resp) = require_str(val, name) {
            return resp;
        }
    }

    let Some(raw_title) = raw_title else {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": "task_title is required"})),
        )
            .into_response();
    };
    let title = raw_title.as_str().unwrap_or("").trim().to_string();
    if title.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({
                "error": "task_title_empty_after_strip",
                "message": "task_title contains only whitespace or non-printable characters after sanitization",
            })),
        )
            .into_response();
    }

    let arguments = json!({
        "task_title": title,
        "task_description": description,
        "priority": priority,
        "assigned_to": assigned_to,
        "parent_task": parent_task,
    });
    let principal = resolved.dispatch_principal.clone();
    let result = match dispatch_rest_tool(&shared, "create_task", arguments, Some(&principal)).await
    {
        Ok(r) => r,
        Err(()) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "Failed to create task"})),
            )
                .into_response()
        }
    };

    if let ToolResult::Ok { data, message } = &result {
        let task_id = data
            .as_ref()
            .and_then(|d| d.get("task_id"))
            .cloned()
            .unwrap_or(Value::Null);
        return Json(json!({
            "success": true,
            "task_id": task_id,
            "message": message.clone().unwrap_or_else(|| format!("Task '{title}' created successfully")),
        }))
        .into_response();
    }
    let (status, _) = result.to_http();
    let message = result.error_message("Failed to create task", Some("Parent task"));
    (
        StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        Json(json!({"error": message})),
    )
        .into_response()
}

/// `GET /api/tasks/{task_id}/delete-preview` -- blast-radius preview
/// before a delete, matching `delete_task_preview_api_route`. Reuses
/// `collect_task_descendants` (the same authoritative walk the delete
/// cascade itself uses) plus two direct queries for the other two
/// refusal conditions (dependents, blocking agents).
pub async fn task_delete_preview(
    Path(task_id): Path<String>,
    State(shared): State<Arc<SharedState>>,
) -> Response {
    let guard = shared.conn.lock().await;

    let title: Option<String> = guard
        .query_row(
            "SELECT title FROM tasks WHERE task_id = ?1",
            [&task_id],
            |r| r.get(0),
        )
        .optional()
        .unwrap_or(None);
    let Some(title) = title else {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({"error": format!("Task '{task_id}' not found")})),
        )
            .into_response();
    };

    let descendants =
        conexus_tools::task_tools::collect_task_descendants(&guard, &task_id).unwrap_or_default();
    let descendant_rows: Vec<Value> = descendants
        .iter()
        .map(|(descendant_id, assigned_to)| {
            let (d_title, d_status): (String, String) = guard
                .query_row(
                    "SELECT title, status FROM tasks WHERE task_id = ?1",
                    [descendant_id],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )
                .unwrap_or_default();
            json!({
                "task_id": descendant_id,
                "title": d_title,
                "status": d_status,
                "assigned_to": assigned_to,
            })
        })
        .collect();

    let mut dependents: Vec<Value> = Vec::new();
    {
        let pattern = format!("%\"{task_id}\"%");
        let mut stmt = guard
            .prepare("SELECT task_id, title FROM tasks WHERE json_extract(depends_on_tasks, '$') LIKE ?1")
            .expect("valid SQL");
        let rows = stmt
            .query_map([&pattern], |r| {
                Ok(json!({"task_id": r.get::<_, String>(0)?, "title": r.get::<_, String>(1)?}))
            })
            .expect("valid query");
        for row in rows.flatten() {
            dependents.push(row);
        }
    }

    let mut blocking_agents: Vec<String> = Vec::new();
    {
        let mut stmt = guard
            .prepare("SELECT agent_id FROM agents WHERE current_task = ?1")
            .expect("valid SQL");
        let rows = stmt
            .query_map([&task_id], |r| r.get::<_, String>(0))
            .expect("valid query");
        for row in rows.flatten() {
            blocking_agents.push(row);
        }
    }
    drop(guard);

    let requires_force =
        !descendant_rows.is_empty() || !dependents.is_empty() || !blocking_agents.is_empty();
    Json(json!({
        "task_id": task_id,
        "title": title,
        "descendant_count": descendant_rows.len(),
        "descendants": descendant_rows,
        "dependent_count": dependents.len(),
        "dependents": dependents,
        "blocking_agents": blocking_agents,
        "requires_force": requires_force,
    }))
    .into_response()
}

/// `DELETE /api/tasks/{task_id}` -- admin deletes a task, matching
/// `delete_task_api_route`. `force_delete` is client-supplied (default
/// `false`) -- a real backstop, not wire-compat theater: the tool's
/// own cascade guard (children/dependents/an agent's `current_task`)
/// only bites when this is `false`, so the operator must have actually
/// seen the delete-preview's blast radius before force-confirming.
pub async fn delete_task(
    Path(task_id): Path<String>,
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
    body: Bytes,
) -> Response {
    let force_delete = if body.is_empty() {
        false
    } else {
        match decode_untrusted_body(&body) {
            Ok(data) => data
                .get("force_delete")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            Err(_) => false,
        }
    };
    let arguments = json!({"task_id": task_id, "force_delete": force_delete});
    let principal = resolved.dispatch_principal.clone();
    dispatch_through_tool(
        &shared,
        "delete_task",
        arguments,
        &principal,
        Some(format!("Task '{task_id}' deleted successfully")),
    )
    .await
}

/// Generic REST->tool dispatch envelope. Port of
/// `_dispatch_helpers._dispatch_through_tool`'s response-shaping half
/// (the auth/dispatch half is `dispatch_rest_tool`): `Ok` ->
/// `{"success": true, "message": <success_message or the tool's own
/// Ok.message>, ["data": ...if present]}`; every other variant -> the
/// shared `to_http` status with `tool_result_error_message`'s wording.
/// The 201-for-create heuristic matches Python's own
/// `tool_name.startswith("create_")` naming convention.
async fn dispatch_through_tool(
    shared: &Arc<SharedState>,
    tool_name: &str,
    arguments: Value,
    principal: &conexus_core::principal::Principal,
    success_message: Option<String>,
) -> Response {
    let result = match dispatch_rest_tool(shared, tool_name, arguments, Some(principal)).await {
        Ok(r) => r,
        Err(()) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "Tool dispatch failed"})),
            )
                .into_response()
        }
    };
    if let ToolResult::Ok { data, message } = &result {
        let mut body = json!({
            "success": true,
            "message": success_message.unwrap_or_else(|| message.clone().unwrap_or_default()),
        });
        if let Some(d) = data {
            if let Value::Object(map) = &mut body {
                map.insert("data".to_string(), d.clone());
            }
        }
        let status = if tool_name.starts_with("create_") && data.is_some() {
            StatusCode::CREATED
        } else {
            StatusCode::OK
        };
        return (status, Json(body)).into_response();
    }
    let (status, http_body) = result.to_http();
    let message = http_body
        .get("message")
        .and_then(Value::as_str)
        .unwrap_or("Request rejected");
    (
        StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        Json(json!({"error": message})),
    )
        .into_response()
}

// -- /api/tokens, /api/settings-data, /api/settings (Phase E1 PR 7/14,
// conexus-rest-settings-store) ---------------------------------------

/// `GET /api/tokens` -- every LIVE agent's bearer token, matching
/// `tokens_api_route`. UNLIKE `/api/settings-data` below, this gates
/// the WHOLE response on confirmed-operator-tier (plaintext bearers
/// are the endpoint's entire contract -- there is no non-secret
/// residue a masked row could return, so a blanket 403 replaces
/// Python's per-row redaction here). Re-derived from the DB directly
/// (`AgentRepository::list_active`, excluding `status == "system"`
/// same as `ViewStatusTool`/`ViewAgentsTool`'s own precedent) rather
/// than Python's in-memory `g.active_agents` cache, which this
/// migration never built -- `list_active`'s `NOT_TERMINAL_SQL` filter
/// is definitionally identical to `is_live_status`.
pub async fn tokens(
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
) -> Response {
    if !resolved.confirmed_operator_tier {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({
                "error": "forbidden",
                "message": "Agent bearer tokens are operator-tier only. Use an \
                    operator-tier bearer (agent CLI / admin script) to read this endpoint.",
            })),
        )
            .into_response();
    }
    let guard = shared.conn.lock().await;
    let rows = match conexus_db::agent_repository::AgentRepository::list_active(&guard) {
        Ok(rows) => rows,
        Err(_) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "Error retrieving tokens"})),
            )
                .into_response()
        }
    };
    drop(guard);
    let agent_tokens: Vec<_> = rows
        .iter()
        .filter(|a| a.status != "system")
        .map(|a| json!({"agent_id": a.agent_id, "token": a.token}))
        .collect();
    Json(json!({"agent_tokens": agent_tokens})).into_response()
}

fn settings_row_to_json(row: &conexus_db::project_settings_repository::ProjectSettingRow) -> Value {
    json!({
        "context_key": row.context_key,
        "value": row.value,
        "description": row.description,
        "created_at": row.created_at,
        "created_by": row.created_by,
        "updated_at": row.updated_at,
        "updated_by": row.updated_by,
    })
}

/// `GET /api/settings-data` -- every `project_settings` row, matching
/// `settings_data_api_route`. CRITICAL (F009, preserved exactly): does
/// NOT gate the whole response on confirmed-operator-tier -- real
/// values go out to every admitted operator; only the genuinely-secret
/// keys (currently none -- `SECRET_SETTING_KEYS` is schema-derived and
/// empty today) redact per-row via the already-ported
/// `redact_settings_row`, shared with the MCP view tool so the two
/// surfaces can't drift.
pub async fn settings_data(
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
) -> Response {
    let guard = shared.conn.lock().await;
    let rows = match conexus_db::project_settings_repository::list_all(&guard) {
        Ok(rows) => rows,
        Err(_) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "Failed to read project settings"})),
            )
                .into_response()
        }
    };
    drop(guard);
    let settings: Vec<_> = rows
        .iter()
        .map(|r| {
            let redacted = conexus_tools::project_settings_tools::redact_settings_row(
                r,
                resolved.confirmed_operator_tier,
            );
            settings_row_to_json(&redacted)
        })
        .collect();
    Json(json!({"settings": settings})).into_response()
}

/// Shared upsert adapter for `POST /api/settings` and `PUT
/// /api/settings/{context_key}` -- both dispatch the gated
/// `update_project_settings` tool. Matches `_dispatch_settings_write`.
async fn dispatch_settings_write(
    shared: &Arc<SharedState>,
    principal: &conexus_core::principal::Principal,
    context_key: &str,
    context_value: Option<&Value>,
    description: Option<&Value>,
) -> Response {
    if let Some(resp) = require_str(Some(&Value::String(context_key.to_string())), "context_key") {
        return resp;
    }
    if let Some(resp) = require_str(description, "description") {
        return resp;
    }
    if has_unsafe_unicode_for_identifier(context_key) {
        return (StatusCode::BAD_REQUEST, Json(unsafe_key_error())).into_response();
    }
    if !is_valid_memory_key(context_key) {
        return (StatusCode::BAD_REQUEST, Json(setting_key_error())).into_response();
    }

    let mut arguments = json!({
        "context_key": context_key,
        "context_value": context_value.cloned().unwrap_or(Value::Null),
    });
    if let Some(desc) = description {
        arguments["description"] = desc.clone();
    }

    let result = match dispatch_rest_tool(
        shared,
        "update_project_settings",
        arguments,
        Some(principal),
    )
    .await
    {
        Ok(r) => r,
        Err(()) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "Failed to update setting"})),
            )
                .into_response()
        }
    };
    if let ToolResult::Ok { message, .. } = &result {
        return Json(json!({
            "success": true,
            "message": message.clone().unwrap_or_else(|| format!("Setting '{context_key}' updated successfully")),
        }))
        .into_response();
    }
    let (status, _) = result.to_http();
    let message = result.error_message("Failed to update setting", None);
    (
        StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        Json(json!({"error": message})),
    )
        .into_response()
}

/// Port of `agent_mcp/utils/string_utils.py::SETTING_KEY_ERROR`.
fn setting_key_error() -> Value {
    json!({
        "error": "invalid_key_character",
        "message": "Setting key may contain only letters, digits, and . _ / - \
            (A-Z a-z 0-9 . _ / -).",
    })
}

/// `POST /api/settings` -- upsert a setting (body carries the key),
/// matching `create_setting_api_route`.
pub async fn create_setting(
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
    body: Bytes,
) -> Response {
    let data = match decode_untrusted_body(&body) {
        Ok(d) => d,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": e.to_string()})),
            )
                .into_response()
        }
    };
    let context_key_value = data.get("context_key");
    if is_json_falsy(context_key_value) {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": "context_key is required"})),
        )
            .into_response();
    }
    // Matches Python's `if not context_key: ...` short-circuit above --
    // by this point `context_key_value` is truthy, but might still be
    // the wrong type; `dispatch_settings_write` runs its own
    // `require_str` check on it, exactly like Python's shared helper.
    let context_key = context_key_value
        .and_then(Value::as_str)
        .unwrap_or_default();
    let principal = resolved.dispatch_principal.clone();
    dispatch_settings_write(
        &shared,
        &principal,
        context_key,
        data.get("context_value"),
        data.get("description"),
    )
    .await
}

/// `PUT /api/settings/{context_key}` -- upsert a setting, matching
/// `update_setting_api_route`.
pub async fn update_setting(
    Path(context_key): Path<String>,
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
    body: Bytes,
) -> Response {
    let data = match decode_untrusted_body(&body) {
        Ok(d) => d,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": e.to_string()})),
            )
                .into_response()
        }
    };
    let principal = resolved.dispatch_principal.clone();
    dispatch_settings_write(
        &shared,
        &principal,
        &context_key,
        data.get("context_value"),
        data.get("description"),
    )
    .await
}

/// `DELETE /api/settings/{context_key}` -- thin adapter over the gated
/// `delete_project_settings` tool, matching `delete_setting_api_route`.
pub async fn delete_setting(
    Path(context_key): Path<String>,
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
) -> Response {
    if has_unsafe_unicode_for_identifier(&context_key) {
        return (StatusCode::BAD_REQUEST, Json(unsafe_key_error())).into_response();
    }
    if !is_valid_memory_key(&context_key) {
        return (StatusCode::BAD_REQUEST, Json(setting_key_error())).into_response();
    }
    let principal = resolved.dispatch_principal.clone();
    let arguments = json!({"context_key": context_key});
    let result = match dispatch_rest_tool(
        &shared,
        "delete_project_settings",
        arguments,
        Some(&principal),
    )
    .await
    {
        Ok(r) => r,
        Err(()) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "Failed to delete setting"})),
            )
                .into_response()
        }
    };
    if let ToolResult::Ok { message, .. } = &result {
        return Json(json!({
            "success": true,
            "message": message.clone().unwrap_or_else(|| format!("Setting '{context_key}' deleted successfully")),
        }))
        .into_response();
    }
    let (status, _) = result.to_http();
    let message = result.error_message("Failed to delete setting", None);
    (
        StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        Json(json!({"error": message})),
    )
        .into_response()
}

// -- /api/status, /api/context-data, /api/terminate-agent,
// /api/update-task-dashboard, /api/create-sample-memories
// (Phase E1 PR 8/14, conexus-rest-composition-status) -----------------

/// `GET /api/status` -- aggregate agent/task counts, matching
/// `simple_status_api_route`. SQL `GROUP BY` aggregates
/// (`count_by_status`/`count_active_by_status`), never a full-table
/// materialise-then-count (pentest R4-F1).
///
/// `last_updated` is UTC RFC3339, not Python's naive local-clock
/// `datetime.now().isoformat()` -- a deliberate divergence, matching
/// every other timestamp this Rust port produces (always explicit UTC
/// RFC3339), rather than introducing this crate's first local-
/// timezone read for one informational field nothing depends on for
/// correctness.
pub async fn simple_status(State(shared): State<Arc<SharedState>>) -> Response {
    let guard = shared.conn.lock().await;
    let task_counts = conexus_db::task_repository::count_by_status(&guard);
    let agent_counts =
        conexus_db::agent_repository::AgentRepository::count_active_by_status(&guard);
    drop(guard);
    let (task_counts, agent_counts) = match (task_counts, agent_counts) {
        (Ok(t), Ok(a)) => (t, a),
        _ => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "Failed to get simple status."})),
            )
                .into_response()
        }
    };
    Json(json!({
        "server_running": true,
        "total_agents": agent_counts.values().sum::<i64>(),
        "active_agents": agent_counts.get("active").copied().unwrap_or(0),
        "total_tasks": task_counts.values().sum::<i64>(),
        "pending_tasks": task_counts.get("pending").copied().unwrap_or(0),
        "completed_tasks": task_counts.get("completed").copied().unwrap_or(0),
        "last_updated": chrono::Utc::now().to_rfc3339(),
    }))
    .into_response()
}

fn context_row_to_json(row: &conexus_db::project_context_repository::ProjectContextRow) -> Value {
    json!({
        "context_key": row.context_key,
        "value": row.value,
        "updated_at": row.updated_at,
        "updated_by": row.updated_by,
        "created_at": row.created_at,
        "created_by": row.created_by,
        "description": row.description,
    })
}

/// `GET /api/context-data` -- project_context rows only, matching
/// `context_data_api_route`. ADR-0017: shared project knowledge,
/// returned AS-IS, no content-based redaction. Bounded via the same
/// `?limit` clamp `/api/tasks`/`/api/all-data` share (pentest R2-F2).
pub async fn context_data(
    State(shared): State<Arc<SharedState>>,
    axum::extract::Query(params): axum::extract::Query<std::collections::HashMap<String, String>>,
) -> Response {
    let limit = crate::read_limits::clamp_section_limit(params.get("limit").map(String::as_str));
    let guard = shared.conn.lock().await;
    let rows = conexus_db::project_context_repository::list_recent(&guard, limit);
    drop(guard);
    match rows {
        Ok(rows) => {
            let data: Vec<_> = rows.iter().map(context_row_to_json).collect();
            Json(data).into_response()
        }
        Err(_) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error": "Failed to fetch context data."})),
        )
            .into_response(),
    }
}

/// `POST /api/terminate-agent` -- thin adapter over the `terminate_agent`
/// MCP tool, matching `terminate_agent_dashboard_api_route`.
pub async fn terminate_agent_dashboard(
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
    body: Bytes,
) -> Response {
    let data = match decode_untrusted_body(&body) {
        Ok(d) => d,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"message": e.to_string()})),
            )
                .into_response()
        }
    };
    let agent_id = data.get("agent_id").and_then(Value::as_str);
    let arguments = match agent_id {
        Some(id) => json!({"agent_id": id}),
        None => json!({}),
    };
    let principal = resolved.dispatch_principal.clone();
    dispatch_through_tool(
        &shared,
        "terminate_agent",
        arguments,
        &principal,
        agent_id.map(|id| format!("Agent '{id}' terminated successfully via dashboard API.")),
    )
    .await
}

/// Human-readable error string for `update_task_dashboard`'s legacy
/// `{"error": ...}` envelope. Port of `_update_task_error_detail`.
fn update_task_error_detail(result: &ToolResult) -> String {
    match result {
        ToolResult::NotFound { .. } => "Task not found".to_string(),
        ToolResult::Conflict { reason } | ToolResult::PermissionDenied { reason } => reason.clone(),
        ToolResult::Invalid { message, .. } => message.clone(),
        _ => "Failed to update task.".to_string(),
    }
}

/// `POST /api/update-task-dashboard` -- thin adapter over `update_task`,
/// matching `update_task_details_api_route`. Wire-level rules
/// (unchanged from the pre-refactor route): `task_id` required; at
/// least one editable field required; `assigned_to: null`/`""`/
/// `"unassigned"` all normalize to the tool's own clear sentinel
/// (`"unassigned"`) so the clear intent survives
/// `decode_untrusted_body`'s null handling, distinct from omitting the
/// field entirely (leave unchanged).
pub async fn update_task_dashboard(
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
    body: Bytes,
) -> Response {
    let data = match decode_untrusted_body(&body) {
        Ok(d) => d,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": e.to_string()})),
            )
                .into_response()
        }
    };

    let task_id = data
        .get("task_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty());
    let Some(task_id) = task_id else {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": "task_id is a required field."})),
        )
            .into_response();
    };

    const EDITABLE_KEYS: [&str; 6] = [
        "status",
        "title",
        "description",
        "priority",
        "notes",
        "assigned_to",
    ];
    if !EDITABLE_KEYS.iter().any(|k| data.contains_key(*k)) {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({
                "error": "at least one editable field is required (status, title, description, priority, notes, assigned_to).",
            })),
        )
            .into_response();
    }

    for field in [
        "task_id",
        "status",
        "title",
        "description",
        "priority",
        "assigned_to",
    ] {
        if let Some(resp) = require_str(data.get(field), field) {
            return resp;
        }
    }

    let assigned_to_arg: Value = if let Some(raw) = data.get("assigned_to") {
        let clears = raw.is_null()
            || matches!(raw, Value::String(s) if matches!(s.trim(), "" | "unassigned"));
        if clears {
            Value::String("unassigned".to_string())
        } else {
            raw.clone()
        }
    } else {
        Value::Null
    };

    let arguments = json!({
        "task_id": task_id,
        "status": data.get("status").cloned().unwrap_or(Value::Null),
        "title": data.get("title").cloned().unwrap_or(Value::Null),
        "description": data.get("description").cloned().unwrap_or(Value::Null),
        "priority": data.get("priority").cloned().unwrap_or(Value::Null),
        "assigned_to": assigned_to_arg,
        "notes": data.get("notes").cloned().unwrap_or(Value::Null),
    });

    let principal = resolved.dispatch_principal.clone();
    let result = match dispatch_rest_tool(&shared, "update_task", arguments, Some(&principal)).await
    {
        Ok(r) => r,
        Err(()) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "Failed to update task."})),
            )
                .into_response()
        }
    };

    if let ToolResult::Ok { message, .. } = &result {
        return Json(json!({
            "success": true,
            "message": message.clone().unwrap_or_else(|| "Task updated successfully via dashboard.".to_string()),
        }))
        .into_response();
    }
    let (status, _) = result.to_http();
    let message = if matches!(result, ToolResult::Failed { .. }) {
        "Failed to update task.".to_string()
    } else {
        update_task_error_detail(&result)
    };
    (
        StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        Json(json!({"error": message})),
    )
        .into_response()
}

/// `POST /api/create-sample-memories` -- seeds 4 hard-coded demo
/// `project_context` rows, matching `create_sample_memories_route`.
/// Dispatches through the gated `bulk_update_project_context` tool
/// (R9-F2: not an ORM-direct write) so viewer-tier callers are denied
/// and the rows are attributed to the real operator.
pub async fn create_sample_memories(
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
) -> Response {
    let sample_memories = json!([
        {
            "context_key": "api.config.base_url",
            "context_value": "https://api.example.com",
            "description": "Main API base URL for external services",
        },
        {
            "context_key": "app.settings.theme",
            "context_value": {"theme": "dark", "accent": "blue"},
            "description": "Application theme preferences",
        },
        {
            "context_key": "database.connection.timeout",
            "context_value": 30,
            "description": "Database connection timeout in seconds",
        },
        {
            "context_key": "cache.redis.config",
            "context_value": {"host": "localhost", "port": 6379, "ttl": 3600},
            "description": "Redis cache configuration",
        },
    ]);
    let created_count = sample_memories.as_array().map(Vec::len).unwrap_or(0);
    let principal = resolved.dispatch_principal.clone();
    let result = match dispatch_rest_tool(
        &shared,
        "bulk_update_project_context",
        json!({"updates": sample_memories}),
        Some(&principal),
    )
    .await
    {
        Ok(r) => r,
        Err(()) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"success": false, "error": "Failed to create sample memories."})),
            )
                .into_response()
        }
    };
    if matches!(result, ToolResult::Ok { .. }) {
        return Json(json!({
            "success": true,
            "message": format!("Created {created_count} sample memory entries"),
            "created_count": created_count,
        }))
        .into_response();
    }
    let (status, body) = result.to_http();
    (
        StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        Json(body),
    )
        .into_response()
}

// -- /api/all-data (Phase E1 PR 9/14, conexus-rest-all-data) ---------

/// `GET /api/all-data` -- the single-call dashboard hydration blob
/// (agents, tasks, context, recent actions, file metadata, file map),
/// matching `all_data_api_route`. The largest aggregation endpoint in
/// this REST surface -- isolated into its own PR deliberately, per
/// the migration plan's own note, since it's the one most likely to
/// hide a byte-shape drift.
///
/// **Documented scope gap, not a silent omission**: Python's presence
/// merge (`_mcp_presence_for`) combines TWO signals -- a parked
/// `wait_for_events` long-poll (the PRIMARY signal, Python's own
/// comment: "the in-memory waiter registry... is the authoritative,
/// zero-persistence record") and a live `GET /mcp` SSE stream via
/// `core.session_registry` (the SECONDARY signal, also the source of
/// `last_mcp_connection`). This port implements ONLY the primary
/// signal (`WaiterRegistry::waiter_count`) -- `session_registry` has
/// no Rust equivalent anywhere in this workspace yet; it belongs to
/// the SSE/pub-sub subsystem Phase E1's own remaining PRs (`/api/
/// events`, `/api/delivery`) will build. An agent connected via SSE
/// only (not currently parked in a wait_for_events poll) shows offline
/// here where Python would show online via the secondary signal --
/// tracked as a real, bounded gap to close once `session_registry`'s
/// Rust equivalent exists, not guessed at or stubbed early.
pub async fn all_data(
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
    axum::extract::Query(params): axum::extract::Query<std::collections::HashMap<String, String>>,
) -> Response {
    let section_limit =
        crate::read_limits::clamp_section_limit(params.get("limit").map(String::as_str));
    let expose_tokens = resolved.confirmed_operator_tier;

    let guard = shared.conn.lock().await;

    let global_loop_on = conexus_db::project_settings_repository::get_bool(
        &guard,
        "config_auto_event_loop_global",
        true,
    );

    let active_token_by_agent: std::collections::HashMap<String, String> = if expose_tokens {
        match conexus_db::agent_repository::AgentRepository::list_active(&guard) {
            Ok(rows) => rows
                .into_iter()
                .filter(|a| a.status != "terminated")
                .map(|a| (a.agent_id, a.token))
                .collect(),
            Err(_) => std::collections::HashMap::new(),
        }
    } else {
        std::collections::HashMap::new()
    };

    // `SELECT * FROM agents` (every row, not just live ones -- Python's
    // real query has no WHERE clause at all) via a bounded, newest-first
    // read matching the SQL Python actually runs here.
    let agent_rows = match conexus_db::agent_repository::AgentRepository::list_all_bounded(
        &guard,
        section_limit,
    ) {
        Ok(rows) => rows,
        Err(_) => {
            drop(guard);
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "Failed to fetch all data."})),
            )
                .into_response();
        }
    };

    let agents_data: Vec<Value> = agent_rows
        .iter()
        .filter(|a| a.agent_id != "admin" && a.status != "tombstone")
        .map(|a| {
            let auto_event_loop = a.auto_event_loop;
            // Authoritative Disconnect: a paused agent (per-agent OFF
            // or global OFF) always reads offline, regardless of the
            // waiter signal -- matches `_mcp_presence_for` exactly.
            let paused = !auto_event_loop || !global_loop_on;
            let online = !paused && shared.waiter_registry.waiter_count(&a.agent_id) > 0;
            json!({
                "agent_id": a.agent_id,
                "created_at": a.created_at,
                "status": a.status,
                "current_task": a.current_task,
                "working_directory": a.working_directory,
                "color": a.color,
                "terminated_at": a.terminated_at,
                "updated_at": a.updated_at,
                "auto_event_loop": auto_event_loop,
                "last_event_seen_at": a.last_event_seen_at,
                "last_activity_at": a.last_activity_at,
                "agent_role": a.agent_role,
                "profile": a.profile,
                "profile_updated_at": a.profile_updated_at,
                "profile_reviewed_at": a.profile_reviewed_at,
                "profile_updated_by": a.profile_updated_by,
                "auth_token": active_token_by_agent.get(&a.agent_id),
                "wait_for_events_in_flight": shared.waiter_registry.waiter_count(&a.agent_id) > 0,
                "online": online,
                // Deferred (see fn doc): no session_registry yet, so
                // there is no SSE-derived connection timestamp to
                // report -- always None, not a guessed value.
                "last_mcp_connection": Option::<String>::None,
                // Was missing entirely from this handler's projection
                // (found while wiring `delivery_transport` in PR 14) --
                // Python's real `_mcp_presence_for` includes this field
                // here too, not just on `/api/agents`.
                "transport_status": shared.delivery_transport.get_status(&a.agent_id),
            })
        })
        .collect();

    let tasks = match conexus_db::task_repository::list_all(&guard, Some(section_limit)) {
        Ok(rows) => rows,
        Err(_) => {
            drop(guard);
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "Failed to fetch all data."})),
            )
                .into_response();
        }
    };
    let tasks_data: Vec<Value> = tasks.iter().map(task_row_to_json).collect();

    let context_rows = conexus_db::project_context_repository::list_recent(&guard, section_limit)
        .unwrap_or_default();
    let context_data: Vec<Value> = context_rows.iter().map(context_row_to_json).collect();

    // "last 100" cap, further narrowed by a smaller `?limit` -- matches
    // Python's `min(100, section_limit)` exactly.
    let actions_cap = section_limit.min(100);
    let actions_rows =
        conexus_db::agent_action_repository::list_recent(&guard, None, None, actions_cap)
            .unwrap_or_default();
    let actions_data: Vec<Value> = actions_rows
        .iter()
        .map(|a| {
            json!({
                "action_id": a.action_id,
                "agent_id": a.agent_id,
                "action_type": a.action_type,
                "task_id": a.task_id,
                "timestamp": a.timestamp,
                "details": a.details,
            })
        })
        .collect();

    let file_metadata_rows =
        conexus_db::file_metadata_repository::list_bounded(&guard, section_limit)
            .unwrap_or_default();
    let file_metadata_data: Vec<Value> = file_metadata_rows
        .iter()
        .map(|f| {
            json!({
                "filepath": f.filepath,
                "metadata": f.metadata,
                "last_updated": f.last_updated,
                "updated_by": f.updated_by,
                "content_hash": f.content_hash,
            })
        })
        .collect();

    drop(guard);

    let file_map: std::collections::HashMap<String, Value> = shared
        .file_map
        .preview(usize::MAX)
        .into_iter()
        .map(|(path, entry)| {
            (
                path,
                json!({
                    "agent_id": entry.agent_id,
                    "timestamp": entry.timestamp,
                    "status": entry.status,
                }),
            )
        })
        .collect();

    Json(json!({
        "agents": agents_data,
        "tasks": tasks_data,
        "context": context_data,
        "actions": actions_data,
        "file_metadata": file_metadata_data,
        "file_map": file_map,
        "timestamp": chrono::Utc::now().to_rfc3339(),
    }))
    .into_response()
}

// -- /api/messages (Phase E1 PR 10/14, conexus-rest-messages) ------
//
// Unlike every prior REST surface in this file, messages.py's own
// handlers do NOT dispatch through the MCP tool layer at all (no
// registered `send_agent_message`-shaped tool covers broadcast fan-out
// + a `sender_id` operator-impersonation override + the `unit_of_work`
// atomicity this router hand-rolls) -- these are thin adapters directly
// over `conexus_db::message_repository`, reusing only
// `conexus_tools::agent_messaging::check_send_message_permission` (the
// ONE shared enforcement path OBS6 requires) rather than the full
// `send_agent_message` write core, which hardcodes the sender from the
// calling `Principal` and has no `sender_id` override seam.

/// One process-wide instance, matching Python's module-level
/// `message_repo` import (a singleton) -- the pagination cache must
/// survive across calls the same way `admin_tools.rs`'s
/// `GET_AGENT_TOKENS_REPO` / `task_tools.rs`'s `VIEW_TASKS_ENGINE` do.
static MESSAGE_REPO: LazyLock<conexus_db::message_repository::MessageRepository> =
    LazyLock::new(conexus_db::message_repository::MessageRepository::new);

const MESSAGE_TYPES: [&str; 6] = [
    "text",
    "system",
    "notification",
    "task_update",
    "assistance_request",
    "stop_command",
];
const MESSAGE_PRIORITIES: [&str; 4] = ["low", "normal", "high", "urgent"];

/// Port of `_message_to_dict`: projects a raw
/// [`conexus_db::message_repository::MessageRow`] into the DISPLAY
/// shape every messages.py read endpoint returns -- a reply
/// (`parent_message_id` set) always shows `subject: null` /
/// `subject_is_placeholder: false` (threaded, not subject-bearing);
/// a root message with no stored subject gets a computed 50-char body
/// preview via `message_subject_view`, flagged as a placeholder so the
/// UI can style it differently from a real, sender-chosen subject.
fn message_row_to_json(row: &conexus_db::message_repository::MessageRow) -> Value {
    let (display_subject, is_placeholder) = if row.parent_message_id.is_some() {
        (None, false)
    } else {
        conexus_db::message_repository::message_subject_view(
            row.subject.as_deref(),
            &row.message_content,
        )
    };
    json!({
        "message_id": row.message_id,
        "sender_id": row.sender_id,
        "recipient_id": row.recipient_id,
        "message_content": row.message_content,
        "message_type": row.message_type,
        "priority": row.priority,
        "timestamp": row.timestamp,
        "delivered": row.delivered,
        "read": row.read,
        "subject": display_subject,
        "subject_is_placeholder": is_placeholder,
        "parent_message_id": row.parent_message_id,
    })
}

/// Port of `int(data.get(field, default))`'s exact failure surface
/// (PF-R14-1/PF-R18-1): a JSON `null` (an explicit `"field": null` --
/// distinct from an ABSENT key, which uses `default`) matches Python's
/// `data.get(field)` returning `None` then `int(None)` raising
/// `TypeError`; a list/dict/object value raises the same `TypeError`;
/// a non-numeric string raises `ValueError`; a non-finite number
/// (`1e400` parses to `inf`) raises `OverflowError`. Every one of
/// those 4 exception types maps to the SAME clean 400 at the call
/// site, so this returns a single `Err(())` for all of them rather
/// than a typed error the caller would just discard anyway.
fn coerce_int_field(value: Option<&Value>, default: i64) -> Result<i64, ()> {
    match value {
        None => Ok(default),
        Some(Value::Null) => Err(()),
        // Python: `bool` is an `int` subclass, so `int(True) == 1`.
        Some(Value::Bool(b)) => Ok(i64::from(*b)),
        Some(Value::Number(n)) => n
            .as_i64()
            .or_else(|| {
                n.as_f64()
                    .filter(|f| f.is_finite())
                    .map(|f| f.trunc() as i64)
            })
            .ok_or(()),
        Some(Value::String(s)) => s.trim().parse::<i64>().map_err(|_| ()),
        Some(Value::Array(_)) | Some(Value::Object(_)) => Err(()),
    }
}

/// A non-cryptographic random message id -- port of
/// `secrets.token_hex(8)`'s ID-SPACE (16 hex chars), not its
/// unpredictability guarantee. Same non-security-boundary rationale as
/// `agent_messaging::rand_u64` (a message id is a primary key, never a
/// capability); duplicated rather than exported across the crate
/// boundary for one nine-line helper.
fn random_message_id() -> String {
    format!("msg_{:016x}", random_id_u64())
}

/// Shared non-cryptographic random-id source for [`random_message_id`]
/// and [`poke_agent_directive`]'s poke ids -- neither is a security
/// boundary (both are primary keys), same rationale as
/// `agent_messaging::rand_u64`.
fn random_id_u64() -> u64 {
    use std::collections::hash_map::RandomState;
    use std::hash::BuildHasher;
    RandomState::new().hash_one(std::time::Instant::now())
}

/// `POST /api/messages/query` -- rich-filter listing, matching
/// `list_messages_api_route`. `limit`/`offset` are read from the BODY
/// here (unlike every other `?limit=` list endpoint in this file) --
/// a real, deliberate Python asymmetry preserved as-is.
pub async fn list_messages(State(shared): State<Arc<SharedState>>, body: Bytes) -> Response {
    let data = match decode_untrusted_body(&body) {
        Ok(d) => d,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": e.to_string()})),
            )
                .into_response()
        }
    };

    let limit = match coerce_int_field(data.get("limit"), 50) {
        Ok(v) => v,
        Err(()) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": "limit must be an integer"})),
            )
                .into_response()
        }
    };
    let offset = match coerce_int_field(data.get("offset"), 0) {
        Ok(v) => v,
        Err(()) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": "offset must be an integer"})),
            )
                .into_response()
        }
    };
    if !(1..=500).contains(&limit) {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": "limit must be 1..500"})),
        )
            .into_response();
    }
    // PF-R14-1: a negative offset is a harmless sqlite no-op, but
    // clamp to a 0 floor for defense-in-depth.
    let offset = offset.max(0);

    // Silently ignored, not a 400, when malformed -- matches Python's
    // `isinstance(filter_between, list) and len(...) == 2 and
    // all(isinstance(x, str) ...)` guard in `_apply_query_filters`.
    let between: Option<(&str, &str)> =
        data.get("between")
            .and_then(Value::as_array)
            .and_then(|arr| match (arr.first(), arr.get(1), arr.len()) {
                (Some(a), Some(b), 2) => Some((a.as_str()?, b.as_str()?)),
                _ => None,
            });
    // Port of `AgentMessage.read.is_(bool(filter_read))` -- present
    // (including an explicit `null`) means "apply the filter", coerced
    // via Python truthiness, not a strict-bool check.
    let read_filter = data.get("read").map(|v| !is_json_falsy(Some(v)));

    let filters = conexus_db::message_repository::MessageQueryFilters {
        from: data.get("from").and_then(Value::as_str),
        to: data.get("to").and_then(Value::as_str),
        between,
        message_type: data.get("type").and_then(Value::as_str),
        priority: data.get("priority").and_then(Value::as_str),
        read: read_filter,
        since: data.get("since").and_then(Value::as_str),
        until: data.get("until").and_then(Value::as_str),
        q: data.get("q").and_then(Value::as_str),
        limit,
        offset,
    };

    let guard = shared.conn.lock().await;
    let rows = MESSAGE_REPO.query(&guard, &filters, false);
    let total = MESSAGE_REPO.count_query(&guard, &filters);
    drop(guard);

    let (rows, total) = match (rows, total) {
        (Ok(r), Ok(t)) => (r, t),
        _ => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "Failed to list messages"})),
            )
                .into_response()
        }
    };

    let messages: Vec<Value> = rows.iter().map(message_row_to_json).collect();
    Json(json!({"messages": messages, "total": total, "limit": limit, "offset": offset}))
        .into_response()
}

/// `POST /api/messages/participants` -- Messages-tab filter dropdown
/// values, matching `list_participants_api_route`. The body is
/// decoded-and-discarded (must be well-formed JSON, matches Python's
/// `_ = await get_sanitized_json_body(request)`); `?limit=` (NOT the
/// body) drives the shared `[1,5000]`/default-500 clamp every other
/// `/api` list read uses (R4-F2).
pub async fn list_participants(
    State(shared): State<Arc<SharedState>>,
    axum::extract::Query(params): axum::extract::Query<std::collections::HashMap<String, String>>,
    body: Bytes,
) -> Response {
    if let Err(e) = decode_untrusted_body(&body) {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": e.to_string()})),
        )
            .into_response();
    }
    let limit = crate::read_limits::clamp_section_limit(params.get("limit").map(String::as_str));

    let guard = shared.conn.lock().await;
    let result = conexus_db::message_repository::list_participants(&guard, limit);
    drop(guard);

    match result {
        Ok(p) => {
            let live: Vec<Value> = p
                .live
                .iter()
                .map(|a| json!({"agent_id": a.agent_id, "status": a.status}))
                .collect();
            Json(json!({"live": live, "tombstones": p.tombstones})).into_response()
        }
        Err(_) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error": "Failed to list participants"})),
        )
            .into_response(),
    }
}

/// `POST /api/messages` -- admin composes a message, matching
/// `create_message_api_route`. Covers broadcast fan-out
/// (`recipient_id: "*"`), the `sender_id` operator-impersonation
/// override (validated via the same `recipient_exists` check a real
/// recipient gets), OBS6's shared `check_send_message_permission`
/// gate run once before either branch, and the post-commit recipient
/// wake for the single-recipient path only -- Python's broadcast
/// branch fires no wake at all, preserved as-is (verified by reading
/// the real handler, not assumed symmetric).
pub async fn create_message(
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
    body: Bytes,
) -> Response {
    let data = match decode_untrusted_body(&body) {
        Ok(d) => d,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": e.to_string()})),
            )
                .into_response()
        }
    };

    let recipient_id_val = data.get("recipient_id");
    let content_val = data.get("message_content");
    let subject_val = data.get("subject");
    let parent_val = data.get("parent_message_id");
    let sender_override_val = data.get("sender_id");

    for (val, name) in [
        (recipient_id_val, "recipient_id"),
        (content_val, "message_content"),
        (subject_val, "subject"),
        (parent_val, "parent_message_id"),
        (sender_override_val, "sender_id"),
    ] {
        if let Some(resp) = require_str(val, name) {
            return resp;
        }
    }

    let Some(recipient_id) = recipient_id_val
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    else {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": "recipient_id is required"})),
        )
            .into_response();
    };
    let Some(content) = content_val
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    else {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": "message_content is required"})),
        )
            .into_response();
    };
    let message_type = data
        .get("message_type")
        .and_then(Value::as_str)
        .unwrap_or("text");
    let priority = data
        .get("priority")
        .and_then(Value::as_str)
        .unwrap_or("normal");
    if !MESSAGE_TYPES.contains(&message_type) {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": format!("message_type must be one of {MESSAGE_TYPES:?}")})),
        )
            .into_response();
    }
    if !MESSAGE_PRIORITIES.contains(&priority) {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": format!("priority must be one of {MESSAGE_PRIORITIES:?}")})),
        )
            .into_response();
    }

    let dispatch_principal = resolved.dispatch_principal.clone();
    let operator_id = resolved.admission.caller_identity();
    let now = chrono::Utc::now().to_rfc3339();

    let guard = shared.conn.lock().await;

    if let Some(denial) = conexus_tools::agent_messaging::check_send_message_permission(
        &guard,
        &dispatch_principal,
        recipient_id,
        content,
        message_type,
    ) {
        drop(guard);
        let (status, _) = denial.to_http();
        let message = denial.error_message("Message rejected", None);
        return (
            StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
            Json(json!({"error": message})),
        )
            .into_response();
    }

    let explicit_subject = subject_val.and_then(Value::as_str);
    let parent_message_id = parent_val.and_then(Value::as_str);
    let override_sender = sender_override_val
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty());

    let (sender_id, acting_as): (String, Option<String>) = if let Some(sender) = override_sender {
        match conexus_db::message_repository::recipient_exists(&guard, sender) {
            Ok(true) => (sender.to_string(), Some(sender.to_string())),
            Ok(false) => {
                drop(guard);
                return (
                    StatusCode::BAD_REQUEST,
                    Json(json!({"error": "sender_id must be an existing agent in this project"})),
                )
                    .into_response();
            }
            Err(_) => {
                drop(guard);
                return (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    Json(json!({"error": "Failed to send message"})),
                )
                    .into_response();
            }
        }
    } else {
        (operator_id.clone(), None)
    };

    if recipient_id == "*" {
        let active = match conexus_db::agent_repository::AgentRepository::list_active(&guard) {
            Ok(rows) => rows,
            Err(_) => {
                drop(guard);
                return (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    Json(json!({"error": "Failed to send message"})),
                )
                    .into_response();
            }
        };
        // `g.active_agents` re-derived from `list_active` (DB-fresh),
        // matching every prior "which agents are active" REST/tool
        // re-derivation this migration has made -- `status == "system"`
        // additionally excluded since a real `g.active_agents` snapshot
        // (only `register_agent` populates it) would never contain the
        // synthetic system pseudo-agent either.
        let recipients: Vec<String> = active
            .into_iter()
            .filter(|a| a.status != "system" && a.agent_id != "admin" && a.agent_id != sender_id)
            .map(|a| a.agent_id)
            .collect();
        let message_ids: Vec<String> = recipients.iter().map(|_| random_message_id()).collect();
        let rows: Vec<conexus_db::message_repository::NewMessage> = recipients
            .iter()
            .zip(message_ids.iter())
            .map(
                |(recipient, message_id)| conexus_db::message_repository::NewMessage {
                    message_id,
                    sender_id: &sender_id,
                    recipient_id: recipient,
                    message_content: content,
                    message_type,
                    priority,
                    timestamp: &now,
                    delivered: false,
                    read: false,
                    subject: None,
                    parent_message_id: None,
                },
            )
            .collect();

        // The whole transaction is confined to this closure so the
        // `Transaction`'s borrow of `guard` provably ends when the
        // closure returns -- letting `guard` drop cleanly right after,
        // regardless of which branch inside fired. `rusqlite::
        // Transaction`'s own `Drop` impl (rollback-on-drop) makes the
        // borrow-checker's conservative liveness region span every
        // branch of an un-scoped `let tx = ...; if ... { drop(guard);
        // return ...}` shape -- confining it here avoids that entirely
        // rather than fighting NLL diagnostics branch by branch.
        let outcome: Result<usize, ()> = (|| {
            let tx = guard.unchecked_transaction().map_err(|_| ())?;
            conexus_db::message_repository::bulk_send(&tx, &rows).map_err(|_| ())?;
            let mut details = json!({"recipients": recipients, "sent_count": recipients.len()});
            if let Some(ref acting) = acting_as {
                details["operator"] = json!(operator_id);
                details["acting_as"] = json!(acting);
            }
            conexus_db::agent_action_repository::log_agent_action(
                &tx,
                &operator_id,
                "broadcast_message_via_dashboard",
                None,
                Some(&details),
                &now,
            )
            .map_err(|_| ())?;
            tx.commit().map_err(|_| ())?;
            Ok(message_ids.len())
        })();
        drop(guard);
        return match outcome {
            Ok(sent_count) => Json(json!({
                "success": true,
                "broadcast": true,
                "sent_count": sent_count,
                "message_ids": message_ids,
                "message": format!("Broadcast sent to {sent_count} agents"),
            }))
            .into_response(),
            Err(()) => (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "Failed to send message"})),
            )
                .into_response(),
        };
    }

    // Reply (parent set) always stores subject NULL, matching
    // `send_agent_message`'s own identical effective-subject rule.
    let effective_subject = if parent_message_id.is_some() {
        None
    } else {
        explicit_subject
    };
    let message_id = random_message_id();

    // Same closure-confinement as the broadcast branch above -- see its
    // comment for why.
    enum SingleSendOutcome {
        Sent,
        RecipientNotFound,
        ParentNotFound,
        Failed,
    }
    let outcome: SingleSendOutcome = (|| {
        let tx = match guard.unchecked_transaction() {
            Ok(tx) => tx,
            Err(_) => return SingleSendOutcome::Failed,
        };
        let sent = conexus_db::message_repository::send(
            &tx,
            conexus_db::message_repository::NewMessage {
                message_id: &message_id,
                sender_id: &sender_id,
                recipient_id,
                message_content: content,
                message_type,
                priority,
                timestamp: &now,
                delivered: false,
                read: false,
                subject: effective_subject,
                parent_message_id,
            },
        );
        match sent {
            Ok(_) => {
                let mut details = json!({"message_id": message_id, "recipient": recipient_id});
                if let Some(ref acting) = acting_as {
                    details["operator"] = json!(operator_id);
                    details["acting_as"] = json!(acting);
                }
                let log_result = conexus_db::agent_action_repository::log_agent_action(
                    &tx,
                    &operator_id,
                    "sent_message_via_dashboard",
                    None,
                    Some(&details),
                    &now,
                );
                if log_result.is_err() || tx.commit().is_err() {
                    return SingleSendOutcome::Failed;
                }
                SingleSendOutcome::Sent
            }
            Err(conexus_db::message_repository::SendMessageError::RecipientNotFound(_)) => {
                SingleSendOutcome::RecipientNotFound
            }
            Err(conexus_db::message_repository::SendMessageError::ParentMessageNotFound(_)) => {
                SingleSendOutcome::ParentNotFound
            }
            Err(conexus_db::message_repository::SendMessageError::Db(_)) => {
                SingleSendOutcome::Failed
            }
        }
    })();
    drop(guard);

    match outcome {
        SingleSendOutcome::Sent => {
            // BL-R8-1: the wake fires only AFTER a successful commit --
            // matches `u.on_commit(_wake_recipient)`'s post-commit-only
            // registration exactly.
            shared.waiter_registry.notify(recipient_id);
            Json(json!({
                "success": true,
                "message_id": message_id,
                "message": format!("Message sent to {recipient_id}"),
            }))
            .into_response()
        }
        SingleSendOutcome::RecipientNotFound => (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "Recipient not found"})),
        )
            .into_response(),
        SingleSendOutcome::ParentNotFound => (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "Parent message not found"})),
        )
            .into_response(),
        SingleSendOutcome::Failed => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error": "Failed to send message"})),
        )
            .into_response(),
    }
}

/// `GET /api/messages/{message_id}/thread` -- the whole conversation,
/// matching `get_message_thread_api_route`. The repo owns the
/// root-walk + recursive-CTE collection; this just funnels the path
/// param through and 404s an empty result (message doesn't exist).
pub async fn get_message_thread(
    Path(message_id): Path<String>,
    State(shared): State<Arc<SharedState>>,
) -> Response {
    let guard = shared.conn.lock().await;
    let thread = conexus_db::message_repository::fetch_thread(&guard, &message_id);
    drop(guard);
    match thread {
        Ok(rows) if !rows.is_empty() => {
            let thread: Vec<Value> = rows.iter().map(message_row_to_json).collect();
            Json(json!({"thread": thread})).into_response()
        }
        Ok(_) => (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "Message not found"})),
        )
            .into_response(),
        Err(_) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error": "Failed to fetch message thread"})),
        )
            .into_response(),
    }
}

/// `PATCH /api/messages/{message_id}` -- flips `read`/`delivered`,
/// matching the PATCH branch of Python's combined
/// `patch_message_api_route`. Shares its generic-error string
/// ("Failed to patch message") with [`delete_message`] since Python's
/// two branches share one `except Exception` handler.
pub async fn patch_message(
    Path(message_id): Path<String>,
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
    body: Bytes,
) -> Response {
    let data = match decode_untrusted_body(&body) {
        Ok(d) => d,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": e.to_string()})),
            )
                .into_response()
        }
    };

    let guard = shared.conn.lock().await;
    if conexus_db::message_repository::get_by_id(&guard, &message_id)
        .unwrap_or(None)
        .is_none()
    {
        drop(guard);
        return (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "Message not found"})),
        )
            .into_response();
    }

    // `'read' in data` (key PRESENCE, including an explicit `null`) --
    // not "value is non-null". `data.get("read")` on this crate's
    // `serde_json::Map` returns `Some(&Value::Null)` for an explicit
    // `null`, matching that presence check exactly; `is_json_falsy`
    // then reproduces Python's `bool(None) == False` truthiness.
    let mut updates: Vec<(&'static str, bool)> = Vec::new();
    if let Some(v) = data.get("read") {
        updates.push(("read", !is_json_falsy(Some(v))));
    }
    if let Some(v) = data.get("delivered") {
        updates.push(("delivered", !is_json_falsy(Some(v))));
    }
    if updates.is_empty() {
        drop(guard);
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": "no updatable field provided (read, delivered)"})),
        )
            .into_response();
    }

    let now = chrono::Utc::now().to_rfc3339();
    let operator_id = resolved.admission.caller_identity();
    let fields: Vec<&str> = updates.iter().map(|(c, _)| *c).collect();

    // Closure-confined transaction -- see `create_message`'s single-send
    // branch comment for why this shape is needed at all.
    let ok: bool = (|| -> bool {
        let tx = match guard.unchecked_transaction() {
            Ok(tx) => tx,
            Err(_) => return false,
        };
        for (col, val) in &updates {
            let result = match *col {
                "delivered" => {
                    conexus_db::message_repository::mark_delivered(&tx, &message_id, *val)
                }
                "read" => conexus_db::message_repository::mark_read(&tx, &message_id, *val),
                _ => unreachable!("updates only ever pushes \"read\"/\"delivered\""),
            };
            if result.is_err() {
                return false;
            }
        }
        let details = json!({"message_id": message_id, "fields": fields});
        let log_result = conexus_db::agent_action_repository::log_agent_action(
            &tx,
            &operator_id,
            "updated_message",
            None,
            Some(&details),
            &now,
        );
        log_result.is_ok() && tx.commit().is_ok()
    })();
    drop(guard);

    if ok {
        Json(json!({"success": true})).into_response()
    } else {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error": "Failed to patch message"})),
        )
            .into_response()
    }
}

/// `DELETE /api/messages/{message_id}` -- removes the row, matching
/// the DELETE branch of Python's combined `patch_message_api_route`.
/// See [`patch_message`]'s doc for why the two are separate axum
/// handlers despite sharing one Python function.
pub async fn delete_message(
    Path(message_id): Path<String>,
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
) -> Response {
    let guard = shared.conn.lock().await;
    if conexus_db::message_repository::get_by_id(&guard, &message_id)
        .unwrap_or(None)
        .is_none()
    {
        drop(guard);
        return (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "Message not found"})),
        )
            .into_response();
    }

    let now = chrono::Utc::now().to_rfc3339();
    let operator_id = resolved.admission.caller_identity();

    // Closure-confined transaction -- see `create_message`'s single-send
    // branch comment for why this shape is needed at all.
    let ok: bool = (|| -> bool {
        let tx = match guard.unchecked_transaction() {
            Ok(tx) => tx,
            Err(_) => return false,
        };
        if conexus_db::message_repository::delete(&tx, &message_id).is_err() {
            return false;
        }
        let details = json!({"message_id": message_id});
        let log_result = conexus_db::agent_action_repository::log_agent_action(
            &tx,
            &operator_id,
            "deleted_message_via_dashboard",
            None,
            Some(&details),
            &now,
        );
        log_result.is_ok() && tx.commit().is_ok()
    })();
    drop(guard);

    if ok {
        Json(json!({"success": true, "deleted": message_id})).into_response()
    } else {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            // Python's shared except-block wording (see module doc).
            Json(json!({"error": "Failed to patch message"})),
        )
            .into_response()
    }
}

// -- /api/agents (Phase E1 PR 11/14, conexus-rest-agents-crud) -----
//
// `agent_mcp/app/routers/agents.py`'s CRUD half: list, register,
// restore, edit, rotate-token, purge-preview, purge. The lifecycle
// half (disconnect/reconnect/directive -- touches live-stream +
// waiter-wake runtime state, "different risk profile" per that
// router's own module doc) is its own follow-up PR.
//
// `register_agent`/`restore_agent`/`edit_agent`/`rotate_agent_token`/
// `purge_agent` are all already-registered MCP tools (unlike
// messages.py's create/broadcast) -- these handlers are thin
// `dispatch_rest_tool` adapters, matching `create_task`/`delete_task`'s
// established shape, each with its own bespoke field-flattening since
// none of their real response envelopes match the generic
// `dispatch_through_tool` shape (a flat `{"success", "agent_id", ...}`
// body, not `{"success", "message", "data": {...}}`).

/// `GET /api/agents[?status=&limit=]` -- matches `agents_list_api_route`
/// EXACTLY, including its genuinely no-auth-by-design admission
/// (Python's own docstring: "the router-level gate is deferred to a
/// follow-up PR") -- mounted on `api_public`, not behind `rest_gate`.
/// `status=tombstone` always returns empty (an internal DB state, never
/// operator-queryable) via `AgentRepository::list_for_dashboard`'s own
/// SQL-level exclusion.
///
/// **Documented scope gap, same shape as `/api/all-data`'s (PR 9)**:
/// Python's `_mcp_presence_for` merges the waiter-registry PRIMARY
/// signal with a `core.session_registry`-derived SECONDARY signal (live
/// SSE streams, also `last_mcp_connection`'s source). `session_registry`
/// has no Rust equivalent yet -- this port implements the primary
/// (waiter) signal only for `online`/`last_mcp_connection`.
/// `transport_status` (a THIRD, independent signal) is real as of
/// PR 14 (`conexus-rest-delivery-transport`)'s `DeliveryTransportHub`.
pub async fn list_agents_dashboard(
    State(shared): State<Arc<SharedState>>,
    axum::extract::Query(params): axum::extract::Query<std::collections::HashMap<String, String>>,
) -> Response {
    let status_filter = params.get("status").map(String::as_str);
    if status_filter == Some("tombstone") {
        return Json(Value::Array(Vec::new())).into_response();
    }
    let limit = crate::read_limits::clamp_section_limit(params.get("limit").map(String::as_str));

    let guard = shared.conn.lock().await;
    let global_loop_on = conexus_db::project_settings_repository::get_bool(
        &guard,
        "config_auto_event_loop_global",
        true,
    );
    let rows = conexus_db::agent_repository::AgentRepository::list_for_dashboard(
        &guard,
        status_filter,
        limit,
    );
    drop(guard);

    let rows = match rows {
        Ok(r) => r,
        Err(_) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "Failed to fetch agents list"})),
            )
                .into_response()
        }
    };

    let agents_data: Vec<Value> = rows
        .iter()
        .map(|a| {
            // Same Authoritative-Disconnect formula as `/api/all-data`
            // (PR 9) -- see this fn's own doc for the documented gap.
            let paused = !a.auto_event_loop || !global_loop_on;
            let online = !paused && shared.waiter_registry.waiter_count(&a.agent_id) > 0;
            json!({
                "agent_id": a.agent_id,
                "status": a.status,
                "color": a.color,
                "created_at": a.created_at,
                "current_task": a.current_task,
                "last_activity_at": a.last_activity_at,
                "auto_event_loop": a.auto_event_loop,
                "online": online,
                "last_mcp_connection": if online { a.last_activity_at.clone() } else { None },
                // Real as of PR 14 (`conexus-rest-delivery-transport`)
                // -- was unconditionally null before that hub existed.
                "transport_status": shared.delivery_transport.get_status(&a.agent_id),
            })
        })
        .collect();

    Json(Value::Array(agents_data)).into_response()
}

/// `POST /api/agents/register` -- operator mints an agent identity,
/// matching `register_agent_dashboard_api_route`. Dispatches the
/// registered `register_agent` tool; the response fields are RENAMED
/// from the tool's own `Ok.data` shape (`token` -> `agent_token`) to
/// match this route's historical wire contract, preserved verbatim.
pub async fn register_agent_dashboard(
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
    body: Bytes,
) -> Response {
    let data = match decode_untrusted_body(&body) {
        Ok(d) => d,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"message": e.to_string()})),
            )
                .into_response()
        }
    };

    let name = data
        .get("name")
        .and_then(Value::as_str)
        .or_else(|| data.get("agent_id").and_then(Value::as_str));
    let role = data
        .get("role")
        .and_then(Value::as_str)
        .or_else(|| data.get("agent_role").and_then(Value::as_str))
        .unwrap_or("worker");
    let project_name = data.get("project_name").and_then(Value::as_str);
    let host = data.get("host").and_then(Value::as_str);
    let mount_prefix = data.get("mount_prefix").and_then(Value::as_str);

    let Some(name) = name.filter(|s| !s.is_empty()) else {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"message": "`name` (agent_id) is required."})),
        )
            .into_response();
    };
    if role != "worker" && role != "manager" {
        return (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({"message": format!("Invalid role {role:?}: must be 'worker' or 'manager'.")})),
        )
            .into_response();
    }

    let mut arguments = json!({"name": name, "role": role});
    if let Some(p) = project_name.filter(|s| !s.is_empty()) {
        arguments["project_name"] = json!(p);
    }
    if let Some(h) = host.filter(|s| !s.is_empty()) {
        arguments["host"] = json!(h);
    }
    if let Some(m) = mount_prefix {
        arguments["mount_prefix"] = json!(m);
    }

    let principal = resolved.dispatch_principal.clone();
    let result =
        match dispatch_rest_tool(&shared, "register_agent", arguments, Some(&principal)).await {
            Ok(r) => r,
            Err(()) => {
                return (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    Json(json!({"message": "Error registering agent"})),
                )
                    .into_response()
            }
        };

    if let ToolResult::Ok { data, message } = &result {
        let payload = data.clone().unwrap_or(Value::Null);
        return Json(json!({
            "message": message.clone().unwrap_or_else(|| format!("Agent '{name}' registered.")),
            "agent_id": payload.get("agent_id"),
            "agent_token": payload.get("token"),
            "agent_role": payload.get("agent_role"),
            "mcp_snippet": payload.get("mcp_snippet"),
            "project_name": payload.get("project_name"),
        }))
        .into_response();
    }
    let (status, _) = result.to_http();
    let message = result.error_message("Error registering agent", None);
    (
        StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        Json(json!({"message": message})),
    )
        .into_response()
}

/// `POST /api/agents/{id}/restore` -- reverse a soft-delete, matching
/// `restore_agent_api_route`.
pub async fn restore_agent(
    Path(agent_id): Path<String>,
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
    body: Bytes,
) -> Response {
    if let Err(e) = decode_untrusted_body(&body) {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": e.to_string()})),
        )
            .into_response();
    }
    let arguments = json!({"agent_id": agent_id});
    let principal = resolved.dispatch_principal.clone();
    let result =
        match dispatch_rest_tool(&shared, "restore_agent", arguments, Some(&principal)).await {
            Ok(r) => r,
            Err(()) => {
                return (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    Json(json!({"error": "Failed to restore agent"})),
                )
                    .into_response()
            }
        };
    if let ToolResult::Ok { data, message } = &result {
        let payload = data.clone().unwrap_or(Value::Null);
        return Json(json!({
            "success": true,
            "agent_id": payload.get("agent_id"),
            "status": payload.get("status"),
            "message": message.clone().unwrap_or_else(|| format!("Agent '{agent_id}' restored")),
        }))
        .into_response();
    }
    let (status, _) = result.to_http();
    let message = result.error_message("Failed to restore agent", Some("Agent"));
    (
        StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        Json(json!({"error": message})),
    )
        .into_response()
}

/// `POST /api/agents/{id}/edit` -- update mutable fields (+ the
/// operator-curated `profile`, which bypasses the tool entirely and
/// goes through `AgentRepository::review_profile` directly, matching
/// Python's own out-of-band handling), matching `edit_agent_api_route`.
pub async fn edit_agent(
    Path(agent_id): Path<String>,
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
    body: Bytes,
) -> Response {
    let data = match decode_untrusted_body(&body) {
        Ok(d) => d,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": e.to_string()})),
            )
                .into_response()
        }
    };

    const EDITABLE_AGENT_FIELDS: [&str; 5] = [
        "color",
        "working_directory",
        "aoe_session_id",
        "auto_event_loop",
        "agent_role",
    ];
    let mut updates: serde_json::Map<String, Value> = serde_json::Map::new();
    for field in EDITABLE_AGENT_FIELDS {
        if let Some(v) = data.get(field) {
            updates.insert(field.to_string(), v.clone());
        }
    }

    let profile_supplied = data.contains_key("profile");
    let profile_value = data.get("profile");
    if profile_supplied {
        if let Some(resp) = require_str(profile_value, "profile") {
            return resp;
        }
    }

    if let Some(role) = updates.get("agent_role").and_then(Value::as_str) {
        if role != "worker" && role != "manager" {
            return (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({"error": format!("Invalid agent_role {role:?}: must be 'worker' or 'manager'.")})),
            )
                .into_response();
        }
    }

    if updates.is_empty() && !profile_supplied {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": format!(
                "No editable fields supplied. Accepts any of: {}, profile",
                EDITABLE_AGENT_FIELDS.join(", ")
            )})),
        )
            .into_response();
    }

    for field in ["color", "working_directory"] {
        if let Some(resp) = require_str(updates.get(field), field) {
            return resp;
        }
    }
    if let Some(v) = updates.get("auto_event_loop") {
        if !v.is_boolean() && !v.is_number() {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": "auto_event_loop must be a boolean"})),
            )
                .into_response();
        }
    }
    if let Some(raw) = updates.get("aoe_session_id") {
        let is_clear = raw.is_null() || raw.as_str() == Some("");
        if is_clear {
            updates.insert("aoe_session_id".to_string(), json!(""));
        } else {
            let valid = raw
                .as_str()
                .filter(|s| {
                    s.len() == 16
                        && s.chars()
                            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
                })
                .is_some();
            if !valid {
                return (
                    StatusCode::BAD_REQUEST,
                    Json(json!({"error": format!(
                        "aoe_session_id must be 16 lowercase hex chars or empty (got {raw:?})"
                    )})),
                )
                    .into_response();
            }
        }
    }

    let mut updated_fields: serde_json::Map<String, Value> = serde_json::Map::new();

    if !updates.is_empty() {
        let mut arguments = Value::Object(updates.clone());
        arguments["agent_id"] = json!(agent_id);
        let principal = resolved.dispatch_principal.clone();
        let result =
            match dispatch_rest_tool(&shared, "edit_agent", arguments, Some(&principal)).await {
                Ok(r) => r,
                Err(()) => {
                    return (
                        StatusCode::INTERNAL_SERVER_ERROR,
                        Json(json!({"error": "Failed to edit agent"})),
                    )
                        .into_response()
                }
            };
        match &result {
            ToolResult::Ok { .. } => {
                // The REST response echoes back the fields THIS request
                // just wrote, from the request's own already-validated
                // values -- decoupled from `EditAgentTool::Ok.data`'s
                // exact shape (a plain field-name array, not a
                // field->value dict) so a future change to that tool's
                // internal response shape can't silently break this
                // route's wire contract.
                for (k, v) in &updates {
                    updated_fields.insert(k.clone(), v.clone());
                }
            }
            _ => {
                let (status, _) = result.to_http();
                let message = result.error_message("Failed to edit agent", Some("Agent"));
                return (
                    StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
                    Json(json!({"error": message})),
                )
                    .into_response();
            }
        }
    }

    if profile_supplied {
        let new_profile = profile_value.and_then(Value::as_str).unwrap_or("");
        let now = chrono::Utc::now().to_rfc3339();
        let operator_id = resolved.admission.caller_identity();
        let guard = shared.conn.lock().await;
        let reviewed = conexus_db::agent_repository::AgentRepository::review_profile(
            &guard,
            &agent_id,
            Some(new_profile).filter(|s| !s.is_empty()),
            Some(&operator_id),
            &now,
        );
        drop(guard);
        match reviewed {
            Ok(Some(result)) => {
                updated_fields.insert("profile".to_string(), json!(result.agent.profile));
            }
            Ok(None) => {
                return (
                    StatusCode::NOT_FOUND,
                    Json(json!({"error": format!("Agent '{agent_id}' not found.")})),
                )
                    .into_response();
            }
            Err(_) => {
                return (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    Json(json!({"error": "Failed to edit agent"})),
                )
                    .into_response();
            }
        }
    }

    let field_names: Vec<&str> = updated_fields.keys().map(String::as_str).collect();
    Json(json!({
        "success": true,
        "agent_id": agent_id,
        "updated": updated_fields,
        "message": format!("Agent '{agent_id}' updated: {}", field_names.join(", ")),
    }))
    .into_response()
}

/// `POST /api/agents/{id}/rotate-token` -- credential-only replacement,
/// matching `rotate_agent_token_api_route`.
pub async fn rotate_agent_token(
    Path(agent_id): Path<String>,
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
    body: Bytes,
) -> Response {
    if let Err(e) = decode_untrusted_body(&body) {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": e.to_string()})),
        )
            .into_response();
    }
    let arguments = json!({"agent_id": agent_id});
    let principal = resolved.dispatch_principal.clone();
    let result = match dispatch_rest_tool(
        &shared,
        "rotate_agent_token",
        arguments,
        Some(&principal),
    )
    .await
    {
        Ok(r) => r,
        Err(()) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "Failed to rotate agent token"})),
            )
                .into_response()
        }
    };
    if let ToolResult::Ok { data, message } = &result {
        let payload = data.clone().unwrap_or(Value::Null);
        return Json(json!({
            "success": true,
            "agent_id": payload.get("agent_id"),
            "agent_token": payload.get("token"),
            "message": message.clone().unwrap_or_else(|| format!("Agent '{agent_id}' token rotated")),
        }))
        .into_response();
    }
    let (status, _) = result.to_http();
    let message = result.error_message("Failed to rotate agent token", Some("Agent"));
    (
        StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        Json(json!({"error": message})),
    )
        .into_response()
}

/// `GET /api/agents/{id}/purge-preview` -- blast-radius counts +
/// samples, matching `purge_preview_api_route`. A direct DB read (no
/// tool dispatch -- there is no `purge_preview` tool, mirroring
/// Python's own direct-cursor implementation).
pub async fn agent_purge_preview(
    Path(agent_id): Path<String>,
    State(shared): State<Arc<SharedState>>,
) -> Response {
    let guard = shared.conn.lock().await;
    let status: Option<String> = guard
        .query_row(
            "SELECT status FROM agents WHERE agent_id = ?1",
            [&agent_id],
            |r| r.get(0),
        )
        .optional()
        .unwrap_or(None);
    let Some(status) = status else {
        drop(guard);
        return (
            StatusCode::NOT_FOUND,
            Json(json!({"error": format!("Agent '{agent_id}' not found")})),
        )
            .into_response();
    };

    let mut preview = conexus_tools::admin_tools::gather_purge_preview(&guard, &agent_id);
    drop(guard);
    preview["agent_id"] = json!(agent_id);
    preview["status"] = json!(status);
    preview["tombstone"] = json!(conexus_tools::admin_tools::purge_tombstone(&agent_id));
    Json(preview).into_response()
}

/// `DELETE /api/agents/{id}?cascade=true` -- hard-delete + tombstone
/// cascade, matching `purge_agent_api_route`. Refuses without the
/// explicit confirmation query param -- wire-level safety kept here,
/// same as Python.
pub async fn purge_agent(
    Path(agent_id): Path<String>,
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
    axum::extract::Query(params): axum::extract::Query<std::collections::HashMap<String, String>>,
    body: Bytes,
) -> Response {
    let cascade_confirmed = params
        .get("cascade")
        .map(|v| v.eq_ignore_ascii_case("true"))
        .unwrap_or(false);
    if !cascade_confirmed {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": "Refusing to hard-delete without cascade=true. Pass ?cascade=true to confirm tombstone cascade."})),
        )
            .into_response();
    }
    if let Err(e) = decode_untrusted_body(&body) {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": e.to_string()})),
        )
            .into_response();
    }

    let arguments = json!({"agent_id": agent_id});
    let principal = resolved.dispatch_principal.clone();
    let result = match dispatch_rest_tool(&shared, "purge_agent", arguments, Some(&principal)).await
    {
        Ok(r) => r,
        Err(()) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "Failed to purge agent"})),
            )
                .into_response()
        }
    };
    if let ToolResult::Ok { data, message } = &result {
        let payload = data.clone().unwrap_or(Value::Null);
        return Json(json!({
            "success": true,
            "agent_id": payload.get("agent_id"),
            "tombstone": payload.get("tombstone"),
            "counts": payload.get("counts"),
            "message": message.clone().unwrap_or_else(|| format!("Agent '{agent_id}' purged")),
        }))
        .into_response();
    }
    let (status, _) = result.to_http();
    let message = result.error_message("Failed to purge agent", Some("Agent"));
    (
        StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        Json(json!({"error": message})),
    )
        .into_response()
}

// -- /api/agents lifecycle (Phase E1 PR 12/14, conexus-rest-agents-lifecycle) --
//
// `agents.py`'s remaining surface: disconnect/reconnect (fleet-wide and
// per-agent) + the ad-hoc directive poke. None of these are registered
// MCP tools -- Python calls their `*_tool_impl` functions directly from
// the REST router, never through `dispatch_tool_call`; the Rust port
// mirrors that shape exactly, calling the new unregistered
// `conexus_tools::admin_tools::{disconnect_agent, reconnect_agent,
// disconnect_all_agents, reconnect_all_agents}` functions directly
// rather than inventing a `dispatch_rest_tool`-style lookup for
// functions that were never meant to be dispatchable by name.

/// `POST /api/agents/disconnect-all` -- pause the whole fleet, matching
/// `disconnect_all_agents_api_route`.
pub async fn disconnect_all_agents(
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
    body: Bytes,
) -> Response {
    if let Err(e) = decode_untrusted_body(&body) {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": e.to_string()})),
        )
            .into_response();
    }
    let now = chrono::Utc::now().to_rfc3339();
    let principal = resolved.dispatch_principal.clone();
    let guard = shared.conn.lock().await;
    let result = conexus_tools::admin_tools::disconnect_all_agents(
        &guard,
        &shared.waiter_registry,
        Some(&principal),
        &now,
    );
    drop(guard);
    if let ToolResult::Ok { data, message } = &result {
        let payload = data.clone().unwrap_or(Value::Null);
        return Json(json!({
            "success": true,
            "closed_streams": payload.get("closed_streams").cloned().unwrap_or(json!(0)),
            "message": message.clone().unwrap_or_else(|| "All agents disconnected".to_string()),
        }))
        .into_response();
    }
    let (status, _) = result.to_http();
    let message = result.error_message("Failed to disconnect all agents", Some("Agent"));
    (
        StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        Json(json!({"error": message})),
    )
        .into_response()
}

/// `POST /api/agents/reconnect-all` -- re-enable the global loop,
/// matching `reconnect_all_agents_api_route`.
pub async fn reconnect_all_agents(
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
    body: Bytes,
) -> Response {
    if let Err(e) = decode_untrusted_body(&body) {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": e.to_string()})),
        )
            .into_response();
    }
    let now = chrono::Utc::now().to_rfc3339();
    let principal = resolved.dispatch_principal.clone();
    let guard = shared.conn.lock().await;
    let result = conexus_tools::admin_tools::reconnect_all_agents(
        &guard,
        &shared.waiter_registry,
        Some(&principal),
        &now,
    );
    drop(guard);
    if let ToolResult::Ok { message, .. } = &result {
        return Json(json!({
            "success": true,
            "message": message.clone().unwrap_or_else(|| "All agents reconnected".to_string()),
        }))
        .into_response();
    }
    let (status, _) = result.to_http();
    let message = result.error_message("Failed to reconnect all agents", Some("Agent"));
    (
        StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        Json(json!({"error": message})),
    )
        .into_response()
}

/// `POST /api/agents/{id}/disconnect` -- pause one agent's monitoring,
/// matching `disconnect_agent_api_route`.
pub async fn disconnect_agent(
    Path(agent_id): Path<String>,
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
    body: Bytes,
) -> Response {
    if let Err(e) = decode_untrusted_body(&body) {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": e.to_string()})),
        )
            .into_response();
    }
    let now = chrono::Utc::now().to_rfc3339();
    let principal = resolved.dispatch_principal.clone();
    let guard = shared.conn.lock().await;
    let result = conexus_tools::admin_tools::disconnect_agent(
        &guard,
        &shared.waiter_registry,
        Some(&principal),
        &agent_id,
        &now,
    );
    drop(guard);
    if let ToolResult::Ok { data, message } = &result {
        let payload = data.clone().unwrap_or(Value::Null);
        return Json(json!({
            "success": true,
            "agent_id": payload.get("agent_id"),
            "closed_streams": payload.get("closed_streams").cloned().unwrap_or(json!(0)),
            "message": message.clone().unwrap_or_else(|| format!("Agent '{agent_id}' disconnected")),
        }))
        .into_response();
    }
    let (status, _) = result.to_http();
    let message = result.error_message("Failed to disconnect agent", Some("Agent"));
    (
        StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        Json(json!({"error": message})),
    )
        .into_response()
}

/// `POST /api/agents/{id}/reconnect` -- re-enable one agent's loop,
/// matching `reconnect_agent_api_route`.
pub async fn reconnect_agent(
    Path(agent_id): Path<String>,
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
    body: Bytes,
) -> Response {
    if let Err(e) = decode_untrusted_body(&body) {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": e.to_string()})),
        )
            .into_response();
    }
    let now = chrono::Utc::now().to_rfc3339();
    let principal = resolved.dispatch_principal.clone();
    let guard = shared.conn.lock().await;
    let result = conexus_tools::admin_tools::reconnect_agent(
        &guard,
        &shared.waiter_registry,
        Some(&principal),
        &agent_id,
        &now,
    );
    drop(guard);
    if let ToolResult::Ok { data, message } = &result {
        let payload = data.clone().unwrap_or(Value::Null);
        return Json(json!({
            "success": true,
            "agent_id": payload.get("agent_id"),
            "message": message.clone().unwrap_or_else(|| format!("Agent '{agent_id}' reconnected")),
        }))
        .into_response();
    }
    let (status, _) = result.to_http();
    let message = result.error_message("Failed to reconnect agent", Some("Agent"));
    (
        StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        Json(json!({"error": message})),
    )
        .into_response()
}

const POKE_PRIORITIES: [&str; 4] = ["low", "normal", "high", "urgent"];

/// `POST /api/agents/{id}/directive` -- operator/admin ad-hoc poke,
/// matching `poke_agent_directive_api_route`. Inserts a
/// `pending_directive` row and fires a waiter-wake so a listening
/// agent gets it immediately; a busy agent picks it up on its next
/// check-in. As of PR 14 (`conexus-rest-delivery-transport`), ALSO
/// pushes the identical `directive` frame onto the worker's live
/// delivery-transport stream if one is open (Python's own "IN
/// ADDITION TO the waiter-wake above" -- a chat-style session
/// connected via a runtime like aoe-bridge, but not currently blocked
/// in `wait_for_events`, now sees the poke immediately too, matching
/// Python's real behavior byte-for-byte instead of the bounded gap
/// PRs 9-13 each had to document here).
pub async fn poke_agent_directive(
    Path(agent_id): Path<String>,
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
    body: Bytes,
) -> Response {
    let data = match decode_untrusted_body(&body) {
        Ok(d) => d,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": e.to_string()})),
            )
                .into_response()
        }
    };

    let prompt = data.get("prompt");
    if let Some(resp) = require_str(prompt, "prompt") {
        return resp;
    }
    let Some(prompt) = prompt.and_then(Value::as_str).filter(|s| !s.is_empty()) else {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": "prompt is required"})),
        )
            .into_response();
    };
    if prompt.chars().count() > 4000 {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": "prompt too long (max 4000 characters)"})),
        )
            .into_response();
    }
    let priority = data
        .get("priority")
        .and_then(Value::as_str)
        .unwrap_or("urgent");
    if !POKE_PRIORITIES.contains(&priority) {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": format!("priority must be one of {POKE_PRIORITIES:?}")})),
        )
            .into_response();
    }

    let now = chrono::Utc::now().to_rfc3339();
    let operator_id = resolved.admission.caller_identity();
    let guard = shared.conn.lock().await;

    let target = conexus_db::agent_repository::AgentRepository::get_by_id(&guard, &agent_id);
    let live =
        matches!(&target, Ok(Some(a)) if a.status != "terminated" && a.status != "tombstone");
    if !live {
        drop(guard);
        return (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "Agent not found"})),
        )
            .into_response();
    }

    let poke_id = format!("poke_{:016x}", random_id_u64());
    let created = (|| -> bool {
        let tx = match guard.unchecked_transaction() {
            Ok(tx) => tx,
            Err(_) => return false,
        };
        if conexus_db::pending_directive_repository::create_poke(
            &tx,
            &poke_id,
            &agent_id,
            prompt,
            Some(priority),
            Some(&operator_id),
            &now,
        )
        .is_err()
        {
            return false;
        }
        let details = json!({"poke_id": poke_id, "agent_id": agent_id, "priority": priority});
        let log_result = conexus_db::agent_action_repository::log_agent_action(
            &tx,
            &operator_id,
            "poke_agent_directive",
            None,
            Some(&details),
            &now,
        );
        log_result.is_ok() && tx.commit().is_ok()
    })();
    drop(guard);

    if !created {
        return (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error": "Failed to send directive"})),
        )
            .into_response();
    }

    shared.waiter_registry.notify(&agent_id);
    let delivered = shared.waiter_registry.waiter_count(&agent_id) > 0;

    // "IN ADDITION TO" the waiter-wake above, independent channels --
    // a worker can be delivery-transport-connected, have a parked
    // waiter, both, or neither (matching Python's own comment on this
    // exact call site).
    if shared.delivery_transport.is_connected(&agent_id) {
        let frame = json!({
            "type": "delivery",
            "reason": "poke_due",
            "directive": conexus_db::pending_directive_repository::poke_event(
                &poke_id, prompt, priority, &now,
            ),
        });
        shared.delivery_transport.push(&agent_id, frame);
    }

    Json(json!({
        "success": true,
        "poke_id": poke_id,
        "agent_id": agent_id,
        "delivered": delivered,
        "message": if delivered {
            format!("Directive delivered to {agent_id}")
        } else {
            format!("Directive queued for {agent_id}")
        },
    }))
    .into_response()
}

// -- /api/events (Phase E1 PR 13/14, conexus-rest-sse-events) ------
//
// Operator dashboard live-update SSE channel. Port of
// `agent_mcp/app/routers/events.py` -- the new
// `crate::operator_events::OperatorEventsHub` pub/sub primitive is
// this port's genuinely new piece (Python's `features.operator_events`
// module); the stream loop itself reuses the already-ported
// `conexus_wakeloop::stream_gates::RevalidatingStream` (Phase D3) --
// the SAME bounded-wait + re-validation seam `wait_for_events` uses,
// matching Python's own "four streams, one shared gate" design even
// though this is a genuinely different STREAM (the operator dashboard
// channel, not an agent's event feed -- never conflated with
// `conexus-wakeloop::waiter_registry`, which stays agent-scoped).

/// How often sse-starlette's Python equivalent pings AND how often
/// this port re-validates -- kept equal (matching Python's own
/// `REVALIDATE_SECONDS = PING_SECONDS`) so revocation latency never
/// exceeds one keepalive interval.
const EVENTS_PING_SECONDS: u64 = 15;

/// RAII cleanup for one operator SSE subscription -- unsubscribes the
/// moment the stream is dropped for ANY reason (client disconnect,
/// stream error, or a clean end), which axum/hyper guarantee happens
/// when the response body stops being polled. A genuine improvement
/// over Python's hand-written `try/finally`: there is no exit path
/// this can forget to run on, because it isn't a path at all -- it's
/// the destructor.
struct EventsUnsubscribeGuard {
    shared: Arc<SharedState>,
    id: u64,
}

impl Drop for EventsUnsubscribeGuard {
    fn drop(&mut self) {
        self.shared.operator_events.unsubscribe(self.id);
    }
}

/// True iff `admission` would still be admitted at `/api/events` right
/// now -- the R5-F1 re-validation `_still_authorized` performs.
///
/// **A genuine re-derivation, not a literal port**: Python re-runs the
/// SAME `require_operator_session` dependency against the connection's
/// ORIGINAL request headers, which only does real work for its THIRD
/// door (a session cookie, checked against a live `router.db` row) --
/// this port's two REMAINING doors (per the operator's own "no cookie
/// door" decision, Phase E1 PR1) have no cookie-shaped analogue:
/// - `Forwarding`: a signed grant with an intentionally SHORT TTL
///   (`DEFAULT_REPLAY_WINDOW_SEC`, ~30s) meant to bound replay of a
///   CAPTURED header value for one proxied request -- not a
///   persistent, revocable session. It was already fully verified
///   once, at connect time; re-checking its own embedded expiry here
///   would revoke every forwarding-admitted stream within ~30 SECONDS
///   of opening, which is not what R5-F1 is protecting against.
///   Stays live for the life of the connection once admitted.
/// - `OperatorBearer`: DOES have real, persistent revocation state --
///   `rotate_agent_token`/`purge_agent` can invalidate it mid-stream,
///   exactly the "a revoked credential must not survive it" case
///   R5-F1 exists for. Re-checked for real: the token must still
///   resolve to a live, non-terminated, manager-role `agents` row.
async fn events_still_authorized(shared: &Arc<SharedState>, admission: &RestPrincipal) -> bool {
    match admission {
        RestPrincipal::Forwarding { .. } => true,
        RestPrincipal::OperatorBearer { bearer_token } => {
            let guard = shared.conn.lock().await;
            let row =
                conexus_db::agent_repository::AgentRepository::get_by_token(&guard, bearer_token);
            matches!(
                row,
                Ok(Some(r)) if r.agent_role == "manager" && r.status != "terminated" && r.status != "tombstone"
            )
        }
    }
}

/// `GET /api/events` -- a long-lived `text/event-stream` the dashboard
/// opens; every dashboard-scoped mutation (REST or MCP, see
/// `server.rs::publish_dashboard_change`) publishes a
/// `notifications/resources/updated` envelope onto the hub this drains.
pub async fn operator_events_stream(
    State(shared): State<Arc<SharedState>>,
    Extension(resolved): Extension<ResolvedRestPrincipal>,
) -> impl IntoResponse {
    use axum::response::sse::{Event, KeepAlive, Sse};
    use conexus_wakeloop::stream_gates::{Liveness, RevalidatingStream, StreamSlice};

    let now = chrono::Utc::now().to_rfc3339();
    let user_id = resolved.admission.caller_identity();
    let sub = shared.operator_events.subscribe(Some(user_id), &now);
    let guard = EventsUnsubscribeGuard {
        shared: shared.clone(),
        id: sub.id,
    };

    let admission = resolved.admission.clone();
    let shared_for_liveness = shared.clone();
    let gate = RevalidatingStream::new(
        sub.receiver,
        move || {
            let shared = shared_for_liveness.clone();
            let admission = admission.clone();
            Box::pin(async move {
                if events_still_authorized(&shared, &admission).await {
                    Liveness::live()
                } else {
                    Liveness::revoked("operator session no longer valid")
                }
            })
        },
        || EVENTS_PING_SECONDS as f64,
    );

    let stream = futures_util::stream::unfold((gate, guard), move |(mut gate, guard)| async move {
        loop {
            match gate.next_slice(None).await {
                Ok(StreamSlice::Idle) => continue,
                Ok(StreamSlice::Item(payload)) => {
                    let event = Event::default().data(payload.to_string());
                    return Some((Ok::<Event, std::convert::Infallible>(event), (gate, guard)));
                }
                // Revoked -- end the stream. `guard` drops here,
                // unsubscribing; sse-starlette's Python equivalent
                // relies on a hand-written `finally` for the same
                // effect (see `EventsUnsubscribeGuard`'s own doc).
                Err(_revoked) => return None,
            }
        }
    });

    Sse::new(stream)
        .keep_alive(KeepAlive::new().interval(std::time::Duration::from_secs(EVENTS_PING_SECONDS)))
}

/// `GET /api/events/status` -- operator observability for the
/// live-update channel: the count of live dashboard SSE streams plus a
/// per-stream snapshot (`user_id`/`connected_at`/`age_seconds`/
/// `queue_depth`). A JSON REST endpoint, unlike the `events` stream
/// itself.
pub async fn operator_events_status(State(shared): State<Arc<SharedState>>) -> Response {
    Json(json!({
        "connected": shared.operator_events.subscriber_count(),
        "subscribers": shared.operator_events.snapshot(chrono::Utc::now()),
    }))
    .into_response()
}

// -- /api/delivery (Phase E1 PR 14/14, conexus-rest-delivery-transport) --
//
// The per-worker fallback push channel (ADR-0021). Gated by
// `crate::delivery_gate::require_delivery_agent_bearer` -- a THIRD
// `/api` admission door (see that module's own doc), never
// `rest_gate` (which explicitly rejects a worker bearer) or
// `auth_gate` (which also admits a forwarding header, meaningless for
// a channel keyed by `agent_id`). Mounted as its own sub-router in
// `main.rs`, outside both `api_public`/`api_authenticated`.

/// How often this stream re-validates its bearer -- kept equal to the
/// keepalive ping, matching `operator_events`'s own precedent and
/// Python's identical `REVALIDATE_SECONDS = PING_SECONDS`.
const DELIVERY_PING_SECONDS: u64 = 15;

/// RAII cleanup for one delivery-stream subscription -- see
/// `EventsUnsubscribeGuard`'s own doc for why this is the destructor,
/// not a hand-written `finally`.
struct DeliveryUnsubscribeGuard {
    shared: Arc<SharedState>,
    id: u64,
}

impl Drop for DeliveryUnsubscribeGuard {
    fn drop(&mut self) {
        self.shared.delivery_transport.unsubscribe(self.id);
    }
}

/// `GET /api/delivery/stream` -- streams skinny delivery frames to the
/// worker's runtime as SSE, matching `delivery_stream`. R13-F2: the
/// re-validation predicate is `AgentRepository::is_live` (the same
/// canonical `LIVE_AGENT_SQL`/`NOT_TERMINAL_SQL` DB-backed check
/// `delivery_gate` itself used to admit the connection) -- a stream
/// opened before revocation must tear down, not survive it
/// (AC-R29-1's class).
pub async fn delivery_stream(
    State(shared): State<Arc<SharedState>>,
    Extension(identity): Extension<crate::delivery_gate::ResolvedDeliveryIdentity>,
) -> impl IntoResponse {
    use axum::response::sse::{Event, KeepAlive, Sse};
    use conexus_wakeloop::stream_gates::{Liveness, RevalidatingStream, StreamSlice};

    let agent_id = identity.agent_id;
    let sub = shared.delivery_transport.subscribe(&agent_id);
    let guard = DeliveryUnsubscribeGuard {
        shared: shared.clone(),
        id: sub.id,
    };

    let agent_id_for_liveness = agent_id.clone();
    let shared_for_liveness = shared.clone();
    let gate = RevalidatingStream::new(
        sub.receiver,
        move || {
            let shared = shared_for_liveness.clone();
            let agent_id = agent_id_for_liveness.clone();
            Box::pin(async move {
                let guard = shared.conn.lock().await;
                let live =
                    conexus_db::agent_repository::AgentRepository::is_live(&guard, &agent_id)
                        .unwrap_or(false);
                if live {
                    Liveness::live()
                } else {
                    Liveness::revoked("agent bearer no longer live")
                }
            })
        },
        || DELIVERY_PING_SECONDS as f64,
    );

    let stream = futures_util::stream::unfold((gate, guard), move |(mut gate, guard)| async move {
        loop {
            match gate.next_slice(None).await {
                Ok(StreamSlice::Idle) => continue,
                Ok(StreamSlice::Item(payload)) => {
                    let event = Event::default().data(payload.to_string());
                    return Some((Ok::<Event, std::convert::Infallible>(event), (gate, guard)));
                }
                Err(_revoked) => return None,
            }
        }
    });

    Sse::new(stream).keep_alive(
        KeepAlive::new().interval(std::time::Duration::from_secs(DELIVERY_PING_SECONDS)),
    )
}

/// `POST /api/delivery/status` -- records the worker's runtime-reported
/// `transport-status`, matching `delivery_status`.
pub async fn delivery_status(
    State(shared): State<Arc<SharedState>>,
    Extension(identity): Extension<crate::delivery_gate::ResolvedDeliveryIdentity>,
    body: Bytes,
) -> Response {
    let data = match decode_untrusted_body(&body) {
        Ok(d) => d,
        Err(_) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"detail": "request body must be a JSON object"})),
            )
                .into_response()
        }
    };
    let Some(status) = data.get("status").and_then(Value::as_str) else {
        return (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({"detail": format!("status must be one of {:?}", crate::delivery_transport::VALID_STATUSES)})),
        )
            .into_response();
    };
    if !crate::delivery_transport::VALID_STATUSES.contains(&status) {
        return (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({"detail": format!("status must be one of {:?}", crate::delivery_transport::VALID_STATUSES)})),
        )
            .into_response();
    }
    shared
        .delivery_transport
        .set_status(&identity.agent_id, status);
    Json(json!({"ok": true, "agent_id": identity.agent_id, "status": status})).into_response()
}
