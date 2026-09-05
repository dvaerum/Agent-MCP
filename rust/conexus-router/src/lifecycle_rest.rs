//! Real axum handlers for `admin_api.py`'s project-lifecycle REST
//! surface. Phase E2, `conexus-router-lifecycle-rest-basic` (PR23
//! step 6b of the 10-PR app-wiring breakdown, first slice of 6). Pure
//! wiring over already-built, already-tested decision functions
//! (`project_reads`/`project_gate`/`perm_gates`) -- this module adds
//! no new decision logic of its own.
//!
//! **`health_handler` takes no `Extension<GateIdentity>`** -- it's
//! wired into `state.rs`'s `extra_exact_paths`, so
//! `session_gate_layer` resolves it to `SessionGateOutcome::
//! PassThrough` and never inserts an identity extension at all
//! (matching Python's own `public_route` registration,
//! `admin_api.py:1723-1728`).
//!
//! **`create_project_handler` runs `project_gate::require_capability`
//! as its own first line, THEN `perm_gates::read_body_and_revalidate`
//! around the body-read** -- not a redundant double-check: this
//! mirrors Python's real two-decorator/one-body-fusion shape exactly
//! (`project_lifecycle_gate = require_capability(...)` wraps the
//! whole handler; `read_body_and_revalidate` re-checks AFTER the
//! body-read yield point, closing the TOCTOU window between entry and
//! that await -- gap 5 from this PR's own research, confirmed
//! harmless but real).

use std::collections::HashMap;
use std::sync::Arc;

use axum::extract::{Extension, Path, Query, State};
use axum::http::HeaderMap;
use axum::response::{IntoResponse, Response};
use bytes::Bytes;
use chrono::Utc;
use conexus_core::capability::Capability;

use crate::lifecycle;
use crate::mcp_handler::{HandlerBody, HandlerResponse};
use crate::orchestrator::primitives::{backend_impl_for, run_systemctl, unit_name};
use crate::perm_gates::{self, RevalidationProject, RevalidationSpec};
use crate::project_gate::{self, CreateProjectOutcome, GateError};
use crate::project_reads;
use crate::project_rename;
use crate::project_teardown::{self, MutationPrecheck};
use crate::session_gate::GateIdentity;
use crate::state::RouterState;

fn internal_error(e: impl std::fmt::Display) -> HandlerResponse {
    HandlerResponse {
        status: 500,
        headers: Vec::new(),
        body: HandlerBody::Json(serde_json::json!({
            "success": false,
            "error": "internal",
            "message": e.to_string(),
        })),
    }
}

impl From<GateError> for HandlerResponse {
    fn from(e: GateError) -> Self {
        internal_error(e)
    }
}

fn cookie_header(headers: &HeaderMap) -> Option<&str> {
    headers.get("cookie").and_then(|v| v.to_str().ok())
}

/// Port of `health_handler`. Genuinely unauthenticated -- see this
/// module's own doc for why no `Extension<GateIdentity>` is taken.
pub async fn health_handler(State(state): State<Arc<RouterState>>) -> Response {
    project_reads::health_response(state.mcp_handler_config.single_tenant_name.as_deref())
        .into_response()
}

/// Port of `list_projects_handler` -- session-gated, but no
/// capability check at all (every authenticated caller can list the
/// projects visible to THEM; `visible_project_names` does the actual
/// scoping).
pub async fn list_projects_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
) -> Response {
    let conn = state.conn.lock().await;
    match project_reads::list_projects_response(
        &conn,
        &state.registry,
        state.mcp_handler_config.single_tenant_name.as_deref(),
        identity.is_sysadmin,
        Some(identity.user.user_id.as_str()),
    ) {
        Ok(resp) => resp.into_response(),
        Err(e) => HandlerResponse::from(e).into_response(),
    }
}

/// Port of `create_project_handler`.
///
/// **Found-and-fixed bug (this PR)**: the original version of this
/// handler never checked `disables_write_endpoint` at all -- ADR-0008
/// single-tenant mode disables every project-lifecycle WRITE endpoint
/// (the deploy's topology is fixed for its lifetime), and Python's
/// real handler runs this check as its own very first line, before
/// even the body-read. Confirmed real, not theoretical: a single-
/// tenant deploy's `create_project` should always 410, and didn't.
pub async fn create_project_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let single_tenant_name = state.mcp_handler_config.single_tenant_name.as_deref();
    if let Err(resp) = project_gate::require_capability(
        &identity,
        single_tenant_name,
        Capability::SystemProjectsManage,
    ) {
        return resp.into_response();
    }
    if crate::single_tenant::disables_write_endpoint(single_tenant_name) {
        return crate::single_tenant::single_tenant_disabled_response(single_tenant_name)
            .into_response();
    }

    let conn = state.conn.lock().await;
    let now = Utc::now();
    let now_str = now.to_rfc3339();
    let spec = RevalidationSpec {
        stale_user_id: &identity.user.user_id,
        cookie_header: cookie_header(&headers),
        now: &now_str,
        cap: Capability::SystemProjectsManage,
        project: None,
    };
    let (parsed, _principal) = match perm_gates::read_body_and_revalidate(&conn, &body, &spec) {
        Ok(v) => v,
        Err(resp) => return resp.into_response(),
    };

    let outcome = match project_gate::decide_create_project(
        &conn,
        &state.registry,
        &state.default_workspace_parent,
        identity.is_sysadmin,
        Some(identity.user.user_id.as_str()),
        parsed.get("name"),
        now,
    ) {
        Ok(o) => o,
        Err(e) => return HandlerResponse::from(e).into_response(),
    };

    match outcome {
        CreateProjectOutcome::Created {
            name,
            workspace_label,
        } => lifecycle::success_envelope(
            serde_json::json!({"project": {"name": name, "workspace": workspace_label}}),
            201,
        )
        .into_response(),
        CreateProjectOutcome::Rejected(resp) => resp.into_response(),
    }
}

/// Resolve the real systemd unit for `name`'s backend and run
/// `systemctl <args> <unit>` through the router's configured
/// program/mode/timeout -- the one real yield point both
/// `delete_project_handler`/`stop_project_handler` fuse via
/// `perm_gates::revalidate_after`.
async fn systemctl_on_backend(
    state: &RouterState,
    name: &str,
    args: &[&str],
) -> Result<crate::orchestrator::primitives::SystemctlResult, HandlerResponse> {
    let backend_impl = backend_impl_for(&state.registry, name)
        .map_err(|e| HandlerResponse::from(GateError::from(e)))?;
    let unit = unit_name(name, "backend", &backend_impl)
        .map_err(|e| internal_error(format!("could not resolve unit for {name:?}: {e:?}")))?;
    let mut full_args: Vec<&str> = args.to_vec();
    full_args.push(&unit);
    Ok(run_systemctl(
        &state.ensure_config.systemctl_program,
        state.ensure_config.systemctl_mode,
        &full_args,
        state.ensure_config.systemctl_timeout,
    )
    .await)
}

/// Port of `delete_project_handler`.
pub async fn delete_project_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
    Path(name): Path<String>,
    Query(params): Query<HashMap<String, String>>,
    headers: HeaderMap,
) -> Response {
    let single_tenant_name = state.mcp_handler_config.single_tenant_name.as_deref();
    if crate::single_tenant::disables_write_endpoint(single_tenant_name) {
        return crate::single_tenant::single_tenant_disabled_response(single_tenant_name)
            .into_response();
    }

    let workspace = {
        let conn = state.conn.lock().await;
        match project_teardown::project_mutation_precheck(
            &conn,
            &state.registry,
            &state.runtime,
            identity.is_sysadmin,
            Some(&identity.user.user_id),
            &name,
        ) {
            Ok(MutationPrecheck::Rejected(resp)) => return resp.into_response(),
            Ok(MutationPrecheck::Proceed) => {}
            Err(e) => return HandlerResponse::from(e).into_response(),
        }
        match state.registry.get(&name) {
            Ok(Some(row)) => row.workspace,
            Ok(None) => {
                // Unreachable in practice (Proceed already confirmed the
                // row exists) -- fail closed rather than panic.
                return lifecycle::error_envelope(
                    lifecycle::LifecycleError::NotRegistered,
                    &format!("unknown project: {name:?}"),
                    None,
                )
                .into_response();
            }
            Err(e) => return HandlerResponse::from(GateError::from(e)).into_response(),
        }
    };

    let want_delete = project_teardown::parse_delete_workspace_flag(
        params.get("delete_workspace").map(String::as_str),
    );
    let workspace_outcome = project_teardown::maybe_delete_workspace(
        std::path::Path::new(&workspace),
        &state.default_workspace_parent,
        want_delete,
    );

    let now = Utc::now();
    let now_str = now.to_rfc3339();
    let spec = RevalidationSpec {
        stale_user_id: &identity.user.user_id,
        cookie_header: cookie_header(&headers),
        now: &now_str,
        cap: Capability::SystemProjectsManage,
        project: Some(RevalidationProject {
            project_name: &name,
            min_role: Some("operator"),
        }),
    };

    let (_lock_guard, _principal) =
        match perm_gates::revalidated_lock(&state.runtime, &state.conn, &name, "backend", &spec)
            .await
        {
            Ok(v) => v,
            Err(resp) => return resp.into_response(),
        };
    if let Some(resp) = project_teardown::active_sessions_recheck(&state.runtime, &name) {
        return resp.into_response();
    }

    // Delete ignores the systemctl-stop RESULT entirely (unlike stop
    // below) -- the unregister/purge proceeds unconditionally, even if
    // the unit was already inactive or the stop itself failed. Only
    // the REVALIDATION half of `revalidate_after` can still deny.
    let stop_awaitable = systemctl_stop_ignoring_result(&state, &name);
    let (_ignored, revalidate_result) =
        perm_gates::revalidate_after(stop_awaitable, &state.conn, &spec).await;
    if let Err(resp) = revalidate_result {
        return resp.into_response();
    }

    let finish_result = {
        let conn = state.conn.lock().await;
        project_teardown::finish_delete_project(
            &conn,
            &state.registry,
            &state.runtime,
            &name,
            &state.sock_dir,
            state.token_dir.as_deref(),
        )
    };
    if let Err(e) = finish_result {
        return HandlerResponse::from(e).into_response();
    }

    let mut payload = serde_json::json!({
        "unregistered": name,
        "workspace_deleted": workspace_outcome.deleted,
    });
    if let Some(reason) = workspace_outcome.skipped_reason {
        payload["workspace_delete_skipped_reason"] = serde_json::Value::String(reason);
    }
    lifecycle::success_envelope(payload, 200).into_response()
}

/// `systemctl stop <unit>`, mapping a resolution failure to `()`
/// rather than a `HandlerResponse` -- delete's own contract is to
/// ignore the stop result either way, so there's nothing for its
/// caller to branch on.
async fn systemctl_stop_ignoring_result(state: &RouterState, name: &str) {
    let _ = systemctl_on_backend(state, name, &["stop"]).await;
}

/// Port of `stop_project_handler`. Unlike delete, this DOES branch on
/// the systemctl-stop result -- a nonzero return code is a 500 with a
/// static message (SD-R15-1: never the raw unit path or systemd
/// stderr), and `finish_stop_project` is never called in that case.
pub async fn stop_project_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
    Path(name): Path<String>,
    headers: HeaderMap,
) -> Response {
    let workspace_check = {
        let conn = state.conn.lock().await;
        project_teardown::project_mutation_precheck(
            &conn,
            &state.registry,
            &state.runtime,
            identity.is_sysadmin,
            Some(&identity.user.user_id),
            &name,
        )
    };
    match workspace_check {
        Ok(MutationPrecheck::Rejected(resp)) => return resp.into_response(),
        Ok(MutationPrecheck::Proceed) => {}
        Err(e) => return HandlerResponse::from(e).into_response(),
    }

    let now = Utc::now();
    let now_str = now.to_rfc3339();
    let spec = RevalidationSpec {
        stale_user_id: &identity.user.user_id,
        cookie_header: cookie_header(&headers),
        now: &now_str,
        cap: Capability::SystemProjectsManage,
        project: Some(RevalidationProject {
            project_name: &name,
            min_role: Some("operator"),
        }),
    };

    let (_lock_guard, _principal) =
        match perm_gates::revalidated_lock(&state.runtime, &state.conn, &name, "backend", &spec)
            .await
        {
            Ok(v) => v,
            Err(resp) => return resp.into_response(),
        };
    if let Some(resp) = project_teardown::active_sessions_recheck(&state.runtime, &name) {
        return resp.into_response();
    }

    let is_active_awaitable = systemctl_is_active(&state, &name);
    let (is_active, revalidate_result) =
        perm_gates::revalidate_after(is_active_awaitable, &state.conn, &spec).await;
    if let Err(resp) = revalidate_result {
        return resp.into_response();
    }

    if is_active {
        let stop_awaitable = systemctl_on_backend(&state, &name, &["stop"]);
        let (stop_result, revalidate_result) =
            perm_gates::revalidate_after(stop_awaitable, &state.conn, &spec).await;
        if let Err(resp) = revalidate_result {
            return resp.into_response();
        }
        match stop_result {
            Ok(r) if r.success() => {}
            Ok(_) | Err(_) => {
                return internal_error("failed to stop project backend").into_response();
            }
        }
    }

    project_teardown::finish_stop_project(&state.runtime, &name);
    lifecycle::success_envelope(serde_json::json!({"stopped": name}), 200).into_response()
}

/// `systemctl is-active <unit>` for `name`'s backend, folding a
/// unit-resolution failure into `false` (treated as "not active" --
/// `stop_project_handler` skips the destructive stop call either way,
/// matching Python's own `_is_active` returning `False` on any
/// subprocess error).
async fn systemctl_is_active(state: &RouterState, name: &str) -> bool {
    systemctl_on_backend(state, name, &["is-active"])
        .await
        .map(|r| r.success())
        .unwrap_or(false)
}

/// Port of `rename_project_handler` -- the largest single handler in
/// `admin_api.py` (~470 LOC). TWO real yield points, both already
/// fully decided by `project_rename.rs` (PR 19): a body-read
/// (`read_body_and_revalidate`, project-scoped on `old_name` this
/// time -- unlike `create_project_handler`'s project-less call) and
/// an in-lock `systemctl stop` (`revalidated_lock`/`revalidate_after`,
/// identical shape to delete/stop above). `rename_precheck` re-runs
/// its OWN `deny_cross_tenant_project_read` internally -- the same
/// gap-5 duplicate-check pattern already documented on
/// `create_project_handler`, harmless since no yield point separates
/// the two calls.
pub async fn rename_project_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
    Path(old_name): Path<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let single_tenant_name = state.mcp_handler_config.single_tenant_name.as_deref();
    if crate::single_tenant::disables_write_endpoint(single_tenant_name) {
        return crate::single_tenant::single_tenant_disabled_response(single_tenant_name)
            .into_response();
    }

    let now = Utc::now();
    let now_str = now.to_rfc3339();
    let spec = RevalidationSpec {
        stale_user_id: &identity.user.user_id,
        cookie_header: cookie_header(&headers),
        now: &now_str,
        cap: Capability::SystemProjectsManage,
        project: Some(RevalidationProject {
            project_name: &old_name,
            min_role: Some("operator"),
        }),
    };

    let precheck_ok = {
        let conn = state.conn.lock().await;
        let (parsed, _principal) = match perm_gates::read_body_and_revalidate(&conn, &body, &spec) {
            Ok(v) => v,
            Err(resp) => return resp.into_response(),
        };
        match project_rename::rename_precheck(
            &conn,
            &state.registry,
            &state.runtime,
            identity.is_sysadmin,
            Some(&identity.user.user_id),
            &old_name,
            parsed.get("name"),
            parsed.get("grace_days"),
            now,
        ) {
            Ok(project_rename::RenamePrecheck::Rejected(resp)) => return resp.into_response(),
            Ok(project_rename::RenamePrecheck::Proceed(ok)) => ok,
            Err(e) => return HandlerResponse::from(e).into_response(),
        }
    };
    let new_name = precheck_ok.new_name;
    let grace_days = precheck_ok.grace_days;

    let (_lock_guard, _principal) = match perm_gates::revalidated_lock(
        &state.runtime,
        &state.conn,
        &old_name,
        "backend",
        &spec,
    )
    .await
    {
        Ok(v) => v,
        Err(resp) => return resp.into_response(),
    };

    let old_row = {
        let conn = state.conn.lock().await;
        match project_rename::rename_toctou_recheck(
            &conn,
            &state.registry,
            &state.runtime,
            identity.is_sysadmin,
            Some(&identity.user.user_id),
            &old_name,
            &new_name,
            now,
        ) {
            Ok(project_rename::RenameToctou::Rejected(resp)) => return resp.into_response(),
            Ok(project_rename::RenameToctou::Proceed(ok)) => ok.old_row,
            Err(e) => return HandlerResponse::from(e).into_response(),
        }
    };

    let stop_awaitable = systemctl_stop_ignoring_result(&state, &old_name);
    let (_ignored, revalidate_result) =
        perm_gates::revalidate_after(stop_awaitable, &state.conn, &spec).await;
    if let Err(resp) = revalidate_result {
        return resp.into_response();
    }

    let outcome = {
        let conn = state.conn.lock().await;
        project_rename::finish_rename_project(
            &conn,
            &state.registry,
            &state.runtime,
            identity.is_sysadmin,
            Some(&identity.user.user_id),
            &old_name,
            &new_name,
            grace_days,
            &old_row,
            &state.sock_dir,
            state.token_dir.as_deref(),
            now,
        )
    };
    match outcome {
        Ok(project_rename::RenameOutcome::Renamed {
            from,
            to,
            grace_days,
            alias_expires_at,
        }) => lifecycle::success_envelope(
            serde_json::json!({
                "renamed": {"from": from, "to": to},
                "alias": {"name": from, "grace_days": grace_days, "expires_at": alias_expires_at},
            }),
            200,
        )
        .into_response(),
        Ok(project_rename::RenameOutcome::Rejected(resp)) => resp.into_response(),
        Err(e) => HandlerResponse::from(e).into_response(),
    }
}

/// Port of `alias_usage_handler`. Read-only, no capability check
/// (session-gated + membership-scoped only, matching Python).
pub async fn alias_usage_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
    Path(name): Path<String>,
    Query(params): Query<HashMap<String, String>>,
) -> Response {
    let alias = params.get("alias").map(String::as_str).unwrap_or("");
    let conn = state.conn.lock().await;
    match project_reads::decide_alias_usage(
        &conn,
        &state.registry,
        identity.is_sysadmin,
        Some(&identity.user.user_id),
        &name,
        alias,
        Utc::now(),
    ) {
        Ok(project_reads::AliasUsageOutcome::Found {
            alias,
            project,
            expires_at,
            agents,
        }) => HandlerResponse {
            status: 200,
            headers: vec![("Cache-Control".to_string(), "no-store".to_string())],
            body: HandlerBody::Json(serde_json::json!({
                "alias": alias,
                "project": project,
                "expires_at": expires_at,
                "agents": agents,
            })),
        }
        .into_response(),
        Ok(project_reads::AliasUsageOutcome::Rejected(resp)) => resp.into_response(),
        Err(e) => HandlerResponse::from(e).into_response(),
    }
}

/// Port of `overview_handler` -- the last piece of PR23 step 6 (gap
/// 11). Genuinely new logic, not just wiring: for each project
/// visible to the caller, resolves a REAL `systemctl is-active` await
/// (this crate's own established async-yield-point pattern, matching
/// `systemctl_on_backend` above) then assembles
/// `project_reads::build_project_summary`'s fully-synchronous
/// remainder.
///
/// **Deliberate, documented gap**: the process-local
/// `_overview_cache` (a `(expiry, envelope)` tuple coalescing
/// dashboard first-paint fan-out) has NO Rust equivalent here --
/// this handler always rebuilds fresh. Not silently dropped: caching
/// is a pure latency optimization Python needed because it filters
/// membership AFTER building the full cross-tenant envelope (so one
/// cached build serves every caller regardless of their own
/// visibility); this port instead filters to the caller's visible
/// projects FIRST and only resolves `is_active`/counts for THOSE, a
/// real efficiency gain the cache existed to approximate for Python.
/// Revisit only if a real production request-volume measurement
/// shows the per-request systemctl fan-out is a genuine bottleneck --
/// not assumed speculatively.
pub async fn overview_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
) -> Response {
    let single_tenant_name = state.mcp_handler_config.single_tenant_name.as_deref();
    let rows = match state.registry.list() {
        Ok(rows) => rows,
        Err(e) => return HandlerResponse::from(GateError::from(e)).into_response(),
    };
    let names: Vec<String> = rows.iter().map(|r| r.name.clone()).collect();
    let visible = {
        let conn = state.conn.lock().await;
        project_reads::visible_project_names(
            &conn,
            single_tenant_name,
            identity.is_sysadmin,
            Some(&identity.user.user_id),
            &names,
        )
    };

    let now = std::time::SystemTime::now();
    let mut projects_out = Vec::new();
    for row in rows.iter().filter(|r| visible.contains(&r.name)) {
        let running = systemctl_on_backend(&state, &row.name, &["is-active"])
            .await
            .map(|r| r.success())
            .unwrap_or(false);
        let last_activity = state
            .runtime
            .snapshot(&row.name)
            .and_then(|rt| rt.last_active.get("backend").copied());
        projects_out.push(project_reads::build_project_summary(
            row,
            &state.default_workspace_parent,
            running,
            last_activity,
            now,
        ));
    }

    let mut envelope = serde_json::json!({
        "projects": projects_out,
        "multi_tenant": single_tenant_name.is_none(),
    });
    if let Some(name) = single_tenant_name {
        envelope["single_tenant_name"] = serde_json::Value::String(name.to_string());
    }
    HandlerResponse {
        status: 200,
        headers: vec![("Cache-Control".to_string(), "no-store".to_string())],
        body: HandlerBody::Json(envelope),
    }
    .into_response()
}

/// Port of `remove_alias_handler` -- closes gap 10 (no prior Rust
/// coverage at all) via the new `project_reads::decide_remove_alias`.
pub async fn remove_alias_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
    Path((name, alias)): Path<(String, String)>,
) -> Response {
    let single_tenant_name = state.mcp_handler_config.single_tenant_name.as_deref();
    if crate::single_tenant::disables_write_endpoint(single_tenant_name) {
        return crate::single_tenant::single_tenant_disabled_response(single_tenant_name)
            .into_response();
    }
    let conn = state.conn.lock().await;
    match project_reads::decide_remove_alias(
        &conn,
        &state.registry,
        identity.is_sysadmin,
        Some(&identity.user.user_id),
        &name,
        &alias,
    ) {
        Ok(project_reads::RemoveAliasOutcome::Removed(resp)) => resp.into_response(),
        Ok(project_reads::RemoveAliasOutcome::Rejected(resp)) => resp.into_response(),
        Err(e) => HandlerResponse::from(e).into_response(),
    }
}
