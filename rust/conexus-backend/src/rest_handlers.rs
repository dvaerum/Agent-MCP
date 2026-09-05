//! `/api` REST handlers (Phase E1, prancy-napping-pie). PR 2/14:
//! `/api/prompts/catalog` and `/api/settings-schema` -- the first two
//! real endpoints, chosen because neither touches the DB (zero
//! mutation risk) and together they exercise both halves of the
//! `/api` mount: `prompts/catalog` is genuinely UNAUTHENTICATED
//! (Python's own docstring: "the router-level gate is deferred to a
//! follow-up PR" -- one of the 3 confirmed no-auth-by-design REST
//! endpoints), `settings-schema` requires the `rest_gate` door.

use std::sync::Arc;

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
pub async fn settings_schema(
    Extension(resolved): Extension<ResolvedRestPrincipal>,
) -> impl IntoResponse {
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
