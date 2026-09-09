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
    // Release the DB lock before the (unrelated) async membership
    // grant below -- `decide_create_project` stays deliberately
    // synchronous (see its own doc: `&Connection` is not `Send`, so
    // mixing it with an internal `.await` there breaks axum's
    // `Handler: Send` bound), so the best-effort grant happens HERE
    // instead, once `conn`'s borrow has genuinely ended.
    drop(conn);

    if let CreateProjectOutcome::Created { ref name, .. } = outcome {
        // Best-effort, matching Python: a membership-grant failure is
        // logged, never surfaced as a create failure.
        let _ = crate::identity::add_project_membership(
            &state.sea_orm_db,
            &identity.user.user_id,
            name,
        )
        .await;
    }

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
///
/// **Found-and-fixed bug (this PR)**: the outer `system.projects.
/// manage` capability check ran ONLY inside `revalidated_lock`
/// (well past `maybe_delete_workspace`'s real, recursive
/// `remove_dir_all` on `?delete_workspace=true`) -- unlike Python,
/// where `gated(project_lifecycle_gate(delete_project_handler))`
/// wraps the ENTIRE handler at registration time, so the capability
/// is confirmed before a single line of the handler body (workspace
/// deletion included) ever runs. A project OPERATOR-tier member with
/// no deployment-wide delegation of that capability -- a real,
/// legitimate role this codebase's own docs describe as needing an
/// EXTRA explicit group grant to reach lifecycle mutations at all --
/// could trigger a real recursive workspace delete and only THEN get
/// denied, by which point the damage was already done. Moved the
/// check to this handler's own first line (mirrors
/// `create_project_handler`'s established pattern) so no destructive
/// step of any kind can run before it.
pub async fn delete_project_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
    Path(name): Path<String>,
    Query(params): Query<HashMap<String, String>>,
    headers: HeaderMap,
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

    // `finish_delete_project` no longer needs `conn` at all (see its
    // own doc) -- the best-effort `project_membership` purge
    // (AZ-R13-1) happens below instead, via `state.sea_orm_db`.
    if let Err(e) = project_teardown::finish_delete_project(
        &state.registry,
        &state.runtime,
        &name,
        &state.sock_dir,
        state.token_dir.as_deref(),
    ) {
        return HandlerResponse::from(e).into_response();
    }
    let _ = crate::identity::remove_project_membership_by_project(&state.sea_orm_db, &name).await;

    // SC-R8-1 (mirrors admin_api.py's `ensure_locks.pop((name,
    // "backend"), None)`): drop the per-name ensure lock too -- but
    // only AFTER `_lock_guard` has actually released it (dropping the
    // guard explicitly here, rather than waiting for it to fall out
    // of scope at the end of the function, makes that ordering
    // load-bearing rather than incidental). `finish_delete_project`'s
    // own `store.forget(name, keep_hmac: false, keep_lock: true)`
    // deliberately does NOT do this -- it runs WHILE the lock is
    // still held, and popping the DashMap entry out from under a live
    // guard would desync it from whatever `Arc` a concurrent waiter
    // already cloned. Without this pop, create+delete of N distinct
    // project names leaks N lock objects forever.
    drop(_lock_guard);
    state.runtime.drop_ensure_lock(&name, "backend");

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
    // Found-and-fixed bug (this PR, matches the sibling fix on
    // `delete_project_handler` above): this capability check was
    // previously reached ONLY via `revalidated_lock`, well after the
    // cross-tenant membership precheck below -- unlike Python's
    // `gated(project_lifecycle_gate(stop_project_handler))`, which
    // confirms the capability before the handler body runs at all. No
    // destructive step preceded the old, later check here (unlike
    // delete's workspace removal), but the OBSERVABLE contract still
    // diverged: a caller with membership but no delegated capability
    // got a membership-shaped denial instead of the capability-shaped
    // one Python (and `test_sec_router_admin_authz.py`) expects.
    let single_tenant_name = state.mcp_handler_config.single_tenant_name.as_deref();
    if let Err(resp) = project_gate::require_capability(
        &identity,
        single_tenant_name,
        Capability::SystemProjectsManage,
    ) {
        return resp.into_response();
    }

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
        if let Some(resp) = stop_result_to_failure_response(&stop_result) {
            return resp;
        }
    }

    project_teardown::finish_stop_project(&state.runtime, &name);
    lifecycle::success_envelope(serde_json::json!({"stopped": name}), 200).into_response()
}

/// SD-R15-1: map a completed `systemctl stop` outcome to the client-
/// facing early return. `None` means "the stop succeeded, keep
/// going"; `Some(response)` is the 500 to return immediately. The
/// message is always this fixed literal -- NEVER built from
/// `stop_result`'s stderr/error detail (mirroring `_ensure`'s own
/// SC-R8-2 sibling in `orchestrator::ensure`) -- extracted into its
/// own pure function so that property has a real regression test
/// without needing a full `RouterState`/axum request round-trip.
fn stop_result_to_failure_response(
    stop_result: &Result<crate::orchestrator::primitives::SystemctlResult, HandlerResponse>,
) -> Option<Response> {
    match stop_result {
        Ok(r) if r.success() => None,
        Ok(_) | Err(_) => Some(internal_error("failed to stop project backend").into_response()),
    }
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
        // `conn`'s block-scoped borrow ends here -- `finish_rename_project`
        // stays deliberately synchronous (see its own doc), so the
        // best-effort `project_membership` rekey (AZ-R13-1) happens
        // below instead, once `conn` has genuinely gone out of scope.
    };
    match outcome {
        Ok(project_rename::RenameOutcome::Renamed {
            from,
            to,
            grace_days,
            alias_expires_at,
        }) => {
            let _ =
                crate::identity::rename_project_membership_project(&state.sea_orm_db, &from, &to)
                    .await;
            // SC-R8-1 (mirrors delete): drop the per-OLD-name ensure
            // lock now that `_lock_guard` has actually released it --
            // see the identical comment on `delete_project_handler`
            // above for why the ordering (drop the guard, THEN pop
            // the DashMap entry) is load-bearing. Only reached on the
            // success path -- a `Rejected` outcome below leaves
            // `old_name` a live project whose lock must stay (mirrors
            // `admin_api.py`'s own placement of this exact pop).
            drop(_lock_guard);
            state.runtime.drop_ensure_lock(&old_name, "backend");
            lifecycle::success_envelope(
                serde_json::json!({
                    "renamed": {"from": from, "to": to},
                    "alias": {"name": from, "grace_days": grace_days, "expires_at": alias_expires_at},
                }),
                200,
            )
            .into_response()
        }
        Ok(project_rename::RenameOutcome::Rejected(resp)) => resp.into_response(),
        Err(e) => HandlerResponse::from(e).into_response(),
    }
}

/// Port of `alias_usage_handler`.
///
/// **Found-and-fixed bug (this PR)**: this module's own earlier doc
/// claimed "no capability check at all... matching Python", but the
/// real, CURRENT `admin_api.py` registers this route as `gated(
/// project_lifecycle_gate(alias_usage_handler))` (SEC FINDING 1,
/// 2026-07-09) -- the deployment-wide `system.projects.manage`
/// capability gate applies here too, same as `stop`/`rename`/
/// `delete`. Without it, ANY project member (viewer included --
/// `decide_alias_usage`'s own entry gate has no `min_role`) could
/// read the alias-usage roster with no delegated capability at all.
/// Membership-scoping (closes the cross-tenant existence oracle for
/// a HIDDEN project) still runs too, inside `decide_alias_usage`.
pub async fn alias_usage_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
    Path(name): Path<String>,
    Query(params): Query<HashMap<String, String>>,
) -> Response {
    let single_tenant_name = state.mcp_handler_config.single_tenant_name.as_deref();
    if let Err(resp) = project_gate::require_capability(
        &identity,
        single_tenant_name,
        Capability::SystemProjectsManage,
    ) {
        return resp.into_response();
    }
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
///
/// **Found-and-fixed bug (this PR)**: same class as
/// `alias_usage_handler` above -- the CURRENT `admin_api.py` registers
/// `DELETE .../aliases/{alias}` as `gated(project_lifecycle_gate(
/// remove_alias_handler))` too, and this port had no capability check
/// at all. `decide_remove_alias`'s own `min_role: Some("operator")`
/// closes the membership-rank half; this closes the capability half.
pub async fn remove_alias_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
    Path((name, alias)): Path<(String, String)>,
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::orchestrator::primitives::SystemctlResult;

    // -- SD-R15-1: systemctl stderr must not reach the client body ----

    #[tokio::test]
    async fn stop_result_to_failure_response_never_reflects_systemctl_stderr() {
        let secret = "/nix/store/SECRET-unit-path/agent-mcp-leaky-backend.service";
        let stop_result: Result<SystemctlResult, HandlerResponse> = Ok(SystemctlResult {
            returncode: 1,
            stdout: String::new(),
            stderr: format!("Failed at step EXEC spawning {secret}: No such file"),
        });

        let resp = stop_result_to_failure_response(&stop_result)
            .expect("a failed systemctl stop must produce a failure response");
        assert_eq!(resp.status(), 500);

        let body = http_body_util::BodyExt::collect(resp.into_body())
            .await
            .unwrap()
            .to_bytes();
        let text = String::from_utf8_lossy(&body);
        assert!(
            !text.contains(secret),
            "systemctl stderr leaked into body: {text:?}"
        );
        assert!(
            !text.contains("Failed at step EXEC"),
            "systemctl exec-step detail leaked into body: {text:?}"
        );
        assert!(
            !text.contains("leaky"),
            "unit/project name leaked into body: {text:?}"
        );
    }

    #[test]
    fn stop_result_to_failure_response_is_none_on_a_clean_stop() {
        let stop_result: Result<SystemctlResult, HandlerResponse> = Ok(SystemctlResult {
            returncode: 0,
            stdout: String::new(),
            stderr: String::new(),
        });
        assert!(stop_result_to_failure_response(&stop_result).is_none());
    }

    #[tokio::test]
    async fn stop_result_to_failure_response_handles_a_resolution_error_generically_too() {
        // The `Err` arm (unit-name resolution failure) must be just as
        // generic as the `Ok(non-zero)` arm -- same fixed message,
        // regardless of whatever detail the resolution error itself
        // carries.
        let stop_result: Result<SystemctlResult, HandlerResponse> = Err(internal_error(
            "could not resolve unit for \"leaky\": UnsupportedRole",
        ));
        let resp = stop_result_to_failure_response(&stop_result)
            .expect("a resolution error must produce a failure response");
        assert_eq!(resp.status(), 500);
        let body = http_body_util::BodyExt::collect(resp.into_body())
            .await
            .unwrap()
            .to_bytes();
        let text = String::from_utf8_lossy(&body);
        assert!(
            !text.contains("UnsupportedRole") && !text.contains("leaky"),
            "resolution-error detail leaked into body: {text:?}"
        );
    }
}

/// Real handler-level tests -- each of these calls a `pub async fn`
/// handler directly (no `axum::Router`/`oneshot` needed: every
/// argument here is an ordinary extractor value a caller can
/// construct by hand), proving the wiring fixes in this module's own
/// doc comments above rather than re-testing the pure decision
/// functions (`project_gate`/`project_teardown`/`project_rename`
/// already have exhaustive coverage of those in their own modules).
///
/// Three `test_sec_*` pentest-regression findings land here:
///
/// - `test_sec_router_admin_authz.py` (SEC FINDING 1): `stop`/
///   `aliases` (GET)/`aliases/{alias}` (DELETE) must be gated on
///   `system.projects.manage`, matching the sibling create/rename/
///   delete lifecycle routes -- found completely MISSING for the two
///   alias routes, and present only as a LATE (post-destructive-step)
///   re-check for delete/stop.
/// - `test_sec_r8_lifecycle_hygiene.py` (SC-R8-1): delete/rename must
///   drop their own `ensure_locks` entry once the surrounding lock
///   releases, or create+delete/rename of N distinct names leaks N
///   lock objects forever.
#[cfg(test)]
mod handler_tests {
    use super::*;
    use crate::identity::{self, UserRow};
    use crate::orchestrator::ensure::EnsureConfig;
    use crate::project_registry::ProjectRegistry;
    use crate::rate_limit::RateLimitConfig;
    use crate::session_gate::GateIdentity;
    use crate::state::RouterStateConfig;
    use conexus_core::capability::Capabilities;
    use conexus_core::principal::{Principal, PrincipalKind};
    use conexus_db::schema::init_router_schema;
    use std::collections::HashSet;

    const NOW_STR: &str = "2026-01-01T00:00:00.000+00:00";

    fn test_state_config(dir: &std::path::Path) -> RouterStateConfig {
        RouterStateConfig {
            sock_dir: dir.join("sockets"),
            dashboard_dir: None,
            external_url: None,
            idle_sec: 14400,
            asset_prefix: None,
            single_tenant_name: None,
            single_tenant_workspace: None,
            max_streams_per_agent: 4,
            max_streams_global: 64,
            default_workspace_parent: dir.join("workspaces"),
            token_dir: None,
        }
    }

    /// `_dir` must outlive the state (the project registry's backing
    /// file, and every real workspace dir a test creates, live under
    /// it).
    async fn test_state() -> (tempfile::TempDir, Arc<RouterState>) {
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        init_router_schema(&conn).unwrap();
        let dir = tempfile::TempDir::new().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let sea_orm_db = sea_orm::Database::connect("sqlite::memory:").await.unwrap();
        let state = Arc::new(RouterState::new(
            conn,
            sea_orm_db,
            registry,
            RateLimitConfig::resolve_from_process_env(),
            EnsureConfig::from_env(|_| None),
            test_state_config(dir.path()),
        ));
        (dir, state)
    }

    /// Like [`test_state`], but `conn`/`sea_orm_db` are two HANDLES
    /// onto the SAME real, tempfile-backed SQLite file, not two
    /// independent `sqlite::memory:` databases. `test_state`'s
    /// in-memory `sea_orm_db` is schema-less AND disconnected from
    /// `conn`'s own in-memory database (each `:memory:` connection
    /// gets its own private database) -- fine for every existing test
    /// in this module (none of them read back a `sea_orm_db` write),
    /// but the router step-4 PR C `project_membership` conversion is
    /// the FIRST code in this module that actually writes through
    /// `state.sea_orm_db` for real (`decide_create_project`'s grant /
    /// `finish_rename_project`'s rekey / `finish_delete_project`'s
    /// purge, all fired by the handlers below) -- a test that wants to
    /// observe one of those writes needs a `sea_orm_db` that shares
    /// real state with `conn`, hence this second helper rather than
    /// changing `test_state` itself (touching every existing caller's
    /// fixture is unrelated churn this PR doesn't need).
    async fn test_state_file_backed() -> (tempfile::TempDir, Arc<RouterState>) {
        let dir = tempfile::TempDir::new().unwrap();
        let db_path = dir.path().join("router.db");
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        init_router_schema(&conn).unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let sea_orm_db = sea_orm::Database::connect(format!("sqlite://{}", db_path.display()))
            .await
            .unwrap();
        let state = Arc::new(RouterState::new(
            conn,
            sea_orm_db,
            registry,
            RateLimitConfig::resolve_from_process_env(),
            EnsureConfig::from_env(|_| None),
            test_state_config(dir.path()),
        ));
        (dir, state)
    }

    /// Builds a `GateIdentity` directly, exactly like
    /// `project_gate.rs`'s own `identity_with` helper -- `require_
    /// capability` is a pure in-memory check on `principal.
    /// capabilities`, so a caller-lacks/holds-the-capability scenario
    /// needs no DB group/capability grant plumbing at all. `user_id`
    /// only needs to resolve to a REAL row when the test expects the
    /// handler to reach a later fresh-DB revalidation
    /// (`perm_gates::revalidated_lock`/`read_body_and_revalidate`).
    fn identity_for(user_id: &str, is_sysadmin: bool, caps: HashSet<Capability>) -> GateIdentity {
        GateIdentity {
            user: UserRow {
                user_id: user_id.to_string(),
                username: user_id.to_string(),
                email: None,
                password_hash: None,
                created_at: NOW_STR.to_string(),
                last_login_at: None,
                is_sysadmin,
                sso_subject: None,
            },
            is_sysadmin,
            project: None,
            project_role: None,
            principal: Principal {
                kind: PrincipalKind::OperatorSession,
                user_id: Some(user_id.to_string()),
                agent_id: None,
                project_name: None,
                project_role: None,
                agent_role: None,
                can_wake_loop: false,
                source_token: None,
                capabilities: if is_sysadmin {
                    Capabilities::Sysadmin
                } else {
                    Capabilities::Set(caps)
                },
            },
        }
    }

    /// A real sysadmin user, seeded as the first (auto-bootstrapped)
    /// row so `revalidated_lock`/`read_body_and_revalidate`'s own
    /// fresh DB-based re-derivation ALSO sees them as sysadmin -- for
    /// tests that need a genuine end-to-end success path, not just an
    /// entry-gate denial.
    async fn seed_real_sysadmin(state: &RouterState, username: &str) -> String {
        let mut conn = state.conn.lock().await;
        identity::create_user(
            &mut conn,
            username,
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW_STR,
        )
        .unwrap()
    }

    fn register(state: &RouterState, name: &str, workspace: &std::path::Path) {
        std::fs::create_dir_all(workspace).unwrap();
        state
            .registry
            .register(name, &workspace.to_string_lossy(), "python", Utc::now())
            .unwrap();
    }

    async fn json_body(resp: Response) -> serde_json::Value {
        let body = http_body_util::BodyExt::collect(resp.into_body())
            .await
            .unwrap()
            .to_bytes();
        serde_json::from_slice(&body).unwrap()
    }

    // -- test_sec_router_admin_authz.py: the capability gate must be
    // the FIRST thing every one of these handlers checks -----------

    #[tokio::test]
    async fn delete_project_handler_denies_a_non_cap_caller_before_any_destructive_step() {
        let (dir, state) = test_state().await;
        let ws = dir.path().join("workspaces").join("proj-a");
        register(&state, "proj-a", &ws);
        std::fs::write(ws.join("marker.txt"), b"still here").unwrap();

        // No sysadmin bit, no `system.projects.manage` grant at all --
        // exactly `test_cross_tenant_viewer_denied`'s "vera" shape.
        let identity = identity_for("vera", false, HashSet::new());
        let resp = delete_project_handler(
            State(state.clone()),
            Extension(identity),
            Path("proj-a".to_string()),
            Query(HashMap::from([(
                "delete_workspace".to_string(),
                "true".to_string(),
            )])),
            HeaderMap::new(),
        )
        .await;

        assert_eq!(resp.status(), 403);
        let body = json_body(resp).await;
        assert_eq!(body["error"], "forbidden");
        assert!(body["message"]
            .as_str()
            .unwrap()
            .contains("system.projects.manage"));
        // The found-and-fixed bug: this workspace must NEVER have been
        // touched -- the capability denial must land before any
        // destructive step, not after.
        assert!(
            ws.join("marker.txt").exists(),
            "workspace was deleted despite the caller lacking the capability"
        );
        assert!(state.registry.get("proj-a").unwrap().is_some());
    }

    #[tokio::test]
    async fn stop_project_handler_denies_a_non_cap_caller() {
        let (_dir, state) = test_state().await;
        let identity = identity_for("vera", false, HashSet::new());
        let resp = stop_project_handler(
            State(state.clone()),
            Extension(identity),
            Path("no-such-project".to_string()),
            HeaderMap::new(),
        )
        .await;
        assert_eq!(resp.status(), 403);
        let body = json_body(resp).await;
        assert_eq!(body["error"], "forbidden");
        assert!(body["message"]
            .as_str()
            .unwrap()
            .contains("system.projects.manage"));
    }

    #[tokio::test]
    async fn alias_usage_handler_denies_a_non_cap_caller() {
        let (_dir, state) = test_state().await;
        let identity = identity_for("vera", false, HashSet::new());
        let resp = alias_usage_handler(
            State(state.clone()),
            Extension(identity),
            Path("victim".to_string()),
            Query(HashMap::from([(
                "alias".to_string(),
                "oldname".to_string(),
            )])),
        )
        .await;
        assert_eq!(resp.status(), 403);
        let body = json_body(resp).await;
        assert_eq!(body["error"], "forbidden");
    }

    #[tokio::test]
    async fn remove_alias_handler_denies_a_non_cap_caller() {
        let (_dir, state) = test_state().await;
        let identity = identity_for("vera", false, HashSet::new());
        let resp = remove_alias_handler(
            State(state.clone()),
            Extension(identity),
            Path(("victim".to_string(), "oldname".to_string())),
        )
        .await;
        assert_eq!(resp.status(), 403);
        let body = json_body(resp).await;
        assert_eq!(body["error"], "forbidden");
    }

    #[tokio::test]
    async fn alias_usage_handler_admits_a_cap_holding_non_sysadmin_member() {
        // Regression: a delegated (non-sysadmin) cap-holder who is
        // ALSO a resolved project member must still be admitted --
        // the new capability gate must not over-reject the legitimate
        // Wave-9 delegation shape every other lifecycle route already
        // supports.
        let (dir, state) = test_state().await;
        register(
            &state,
            "victim",
            &dir.path().join("workspaces").join("victim"),
        );
        let uid = {
            let mut conn = state.conn.lock().await;
            let uid = identity::create_user(
                &mut conn,
                "alice",
                "correct horse battery staple",
                None,
                false,
                true, // first user -> sysadmin bootstrap; harmless, we override is_sysadmin below
                &[],
                NOW_STR,
            )
            .unwrap();
            conn.execute(
                "INSERT INTO project_membership (project_name, user_id, role) VALUES ('victim', ?1, 'viewer')",
                [&uid],
            )
            .unwrap();
            // Undo the bootstrap auto-sysadmin so this is a genuine
            // delegated-cap-only (non-sysadmin) caller.
            conn.execute(
                "UPDATE users SET is_sysadmin = 0 WHERE user_id = ?1",
                [&uid],
            )
            .unwrap();
            uid
        };
        let identity = identity_for(
            &uid,
            false,
            HashSet::from([Capability::SystemProjectsManage]),
        );
        let resp = alias_usage_handler(
            State(state.clone()),
            Extension(identity),
            Path("victim".to_string()),
            Query(HashMap::from([(
                "alias".to_string(),
                "oldname".to_string(),
            )])),
        )
        .await;
        // Denied for a DIFFERENT reason (no such alias) is fine -- the
        // point is it must not be 403 forbidden (the cap gate must not
        // be what stops this caller).
        assert_ne!(resp.status(), 403, "{:?}", json_body(resp).await);
    }

    // -- test_sec_r8_lifecycle_hygiene.py (SC-R8-1): delete/rename pop
    // their own ensure_locks entry once the surrounding lock releases

    #[tokio::test]
    async fn delete_project_handler_drops_its_own_ensure_lock_on_success() {
        let (dir, state) = test_state().await;
        let uid = seed_real_sysadmin(&state, "root").await;
        register(
            &state,
            "proj-a",
            &dir.path().join("workspaces").join("proj-a"),
        );
        let before = state.runtime.ensure_lock("proj-a", "backend");

        let identity = identity_for(&uid, true, HashSet::new());
        let resp = delete_project_handler(
            State(state.clone()),
            Extension(identity),
            Path("proj-a".to_string()),
            Query(HashMap::new()),
            HeaderMap::new(),
        )
        .await;
        assert_eq!(resp.status(), 200, "{:?}", json_body(resp).await);

        let after = state.runtime.ensure_lock("proj-a", "backend");
        assert!(
            !std::sync::Arc::ptr_eq(&before, &after),
            "delete must drop its own ensure_locks entry -- a fresh call \
             must mint a NEW lock instance, not find the leaked old one"
        );
    }

    #[tokio::test]
    async fn delete_project_handler_leaves_a_sibling_project_lock_untouched() {
        let (dir, state) = test_state().await;
        let uid = seed_real_sysadmin(&state, "root").await;
        register(&state, "gone", &dir.path().join("workspaces").join("gone"));
        register(
            &state,
            "stays",
            &dir.path().join("workspaces").join("stays"),
        );
        let stays_lock_before = state.runtime.ensure_lock("stays", "backend");

        let identity = identity_for(&uid, true, HashSet::new());
        let resp = delete_project_handler(
            State(state.clone()),
            Extension(identity),
            Path("gone".to_string()),
            Query(HashMap::new()),
            HeaderMap::new(),
        )
        .await;
        assert_eq!(resp.status(), 200);

        let stays_lock_after = state.runtime.ensure_lock("stays", "backend");
        assert!(std::sync::Arc::ptr_eq(
            &stays_lock_before,
            &stays_lock_after
        ));
    }

    #[tokio::test]
    async fn rename_project_handler_drops_its_own_ensure_lock_on_success() {
        let (dir, state) = test_state().await;
        let uid = seed_real_sysadmin(&state, "root").await;
        register(
            &state,
            "old-name",
            &dir.path().join("workspaces").join("old-name"),
        );
        let before = state.runtime.ensure_lock("old-name", "backend");

        let identity = identity_for(&uid, true, HashSet::new());
        let resp = rename_project_handler(
            State(state.clone()),
            Extension(identity),
            Path("old-name".to_string()),
            HeaderMap::new(),
            Bytes::from_static(br#"{"name": "new-name", "grace_days": 7}"#),
        )
        .await;
        assert_eq!(resp.status(), 200, "{:?}", json_body(resp).await);

        let after = state.runtime.ensure_lock("old-name", "backend");
        assert!(
            !std::sync::Arc::ptr_eq(&before, &after),
            "rename must drop its own ensure_locks entry keyed on OLD_NAME"
        );
    }

    #[tokio::test]
    async fn rename_project_handler_never_touches_the_lock_when_rejected_pre_lock() {
        // Sanity check on the placement of the pop: a rename REJECTED
        // by `rename_precheck` (here, a name collision -- renaming
        // "old-name" onto itself collides with its own registry row)
        // never reaches `revalidated_lock` at all, so no lock is ever
        // minted for it -- the pop only ever runs on the SUCCESS path,
        // mirroring `admin_api.py`'s own placement of this exact call
        // strictly after the block that acquires the lock.
        let (dir, state) = test_state().await;
        let uid = seed_real_sysadmin(&state, "root").await;
        register(
            &state,
            "old-name",
            &dir.path().join("workspaces").join("old-name"),
        );

        let identity = identity_for(&uid, true, HashSet::new());
        let resp = rename_project_handler(
            State(state.clone()),
            Extension(identity),
            Path("old-name".to_string()),
            HeaderMap::new(),
            Bytes::from_static(br#"{"name": "old-name", "grace_days": 7}"#), // collides with itself
        )
        .await;
        assert_eq!(resp.status(), 409, "{:?}", json_body(resp).await);
        assert!(state.registry.get("old-name").unwrap().is_some());
    }

    // ====================================================================
    // Genuine end-to-end TOCTOU races, testing the real handlers directly
    // (test_sec_r3f3_active_conns_toctou.py / test_sec_r7f1_project_
    // lifecycle_toctou.py / test_sec_r13f1_rename_lock_toctou.py /
    // test_sec_r36_lifecycle_parity.py / test_sec_r9f5_rename_workspace_
    // desync.py, the last already covered by project_registry.rs's own
    // R9-F5 tests -- not duplicated here).
    //
    // Synchronization technique: tokio's `Mutex` exposes no `asyncio.
    // Lock`-style waiter introspection, so unlike the Python originals'
    // `_wait_until_contended` poll on `lock._waiters`, these tests use
    // `tokio::task::yield_now()` to give the (default `current_thread`,
    // single-worker, cooperatively-scheduled) test runtime exactly the
    // ticks needed to run the spawned task up to its OWN blocking
    // `mutex.lock_owned().await` -- there are zero other await points
    // between a handler's entry and that call, so this is deterministic
    // under `current_thread`, not a timing guess.
    // ====================================================================

    use conexus_db::{group_capability_repository, group_membership_repository};

    /// Seeds a genuinely non-sysadmin delegate carrying `system.
    /// projects.manage` via a REAL group-capability grant (mutable --
    /// these tests revoke it mid-race) plus a real `role`-tier
    /// `project_membership` row on `project`. Returns `(user_id,
    /// group_id, GateIdentity)` -- the identity's own `principal.
    /// capabilities` only matters for the entry-time `require_
    /// capability` check; every later re-check
    /// (`project_mutation_precheck`/`revalidated_lock`/`rename_
    /// precheck`) re-derives fresh from the DB rows this seeds.
    async fn seed_delegate_with_membership(
        state: &RouterState,
        username: &str,
        project: &str,
        role: &str,
    ) -> (String, String, GateIdentity) {
        let mut conn = state.conn.lock().await;
        let is_empty: i64 = conn
            .query_row("SELECT COUNT(*) AS n FROM users", [], |r| r.get(0))
            .unwrap();
        if is_empty == 0 {
            identity::create_user(
                &mut conn,
                "__test_first_sysadmin",
                "ignoredsentinelpassword",
                None,
                false,
                true,
                &[],
                NOW_STR,
            )
            .unwrap();
        }
        let uid = identity::create_user(
            &mut conn,
            username,
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW_STR,
        )
        .unwrap();
        let group = group_membership_repository::create_group(
            &conn,
            &format!("g-{username}"),
            false,
            NOW_STR,
        )
        .unwrap();
        group_capability_repository::replace(
            &conn,
            &group.group_id,
            [Capability::SystemProjectsManage.as_str()],
        )
        .unwrap();
        group_membership_repository::add_group_member(
            &conn,
            &group.group_id,
            Some(&uid),
            None,
            NOW_STR,
        )
        .unwrap();
        conn.execute(
            "INSERT INTO project_membership (project_name, user_id, role) VALUES (?1, ?2, ?3)",
            (project, &uid, role),
        )
        .unwrap();
        let identity = identity_for(
            &uid,
            false,
            HashSet::from([Capability::SystemProjectsManage]),
        );
        (uid, group.group_id, identity)
    }

    async fn revoke_delegate_capability(state: &RouterState, group_id: &str) {
        let conn = state.conn.lock().await;
        group_capability_repository::replace(&conn, group_id, std::iter::empty::<&str>()).unwrap();
    }

    async fn revoke_delegate_membership(state: &RouterState, project: &str, user_id: &str) {
        let conn = state.conn.lock().await;
        conn.execute(
            "DELETE FROM project_membership WHERE project_name = ?1 AND user_id = ?2",
            (project, user_id),
        )
        .unwrap();
    }

    /// Give the `current_thread` test runtime enough ticks to run a
    /// just-spawned task up to its own blocking lock-acquire await --
    /// see this section's own module doc for why this is deterministic
    /// here, not a timing guess.
    async fn let_spawned_task_reach_its_lock_wait() {
        for _ in 0..8 {
            tokio::task::yield_now().await;
        }
    }

    // -- test_sec_r3f3_active_conns_toctou.py: the active_conns guard
    // must be re-checked from INSIDE the lock, immediately before the
    // destructive step -- a connection landing in the outside-check-
    // passed/destructive-op-not-yet-run window must still get the clean
    // 409, never let the destructive op run underneath it. -------------

    #[tokio::test]
    async fn delete_project_handler_toctou_race_active_conns_lands_before_stop_gets_409() {
        let (dir, state) = test_state().await;
        let uid = seed_real_sysadmin(&state, "root").await;
        register(
            &state,
            "racer-del",
            &dir.path().join("workspaces").join("racer-del"),
        );
        let identity = identity_for(&uid, true, HashSet::new());

        let lock = state.runtime.ensure_lock("racer-del", "backend");
        let held = lock.lock_owned().await;

        let state2 = state.clone();
        let task = tokio::spawn(async move {
            delete_project_handler(
                State(state2),
                Extension(identity),
                Path("racer-del".to_string()),
                Query(HashMap::new()),
                HeaderMap::new(),
            )
            .await
        });
        let_spawned_task_reach_its_lock_wait().await;

        // Simulate a connection establishing in the exact window the
        // OUTSIDE active_conns check cannot see.
        state
            .runtime
            .with_runtime_mut("racer-del", |rt| rt.active_conns = 1);
        drop(held);

        let resp = task.await.unwrap();
        assert_eq!(resp.status(), 409, "{:?}", json_body(resp).await);
        assert!(
            state.registry.get("racer-del").unwrap().is_some(),
            "the destructive delete must NOT have run"
        );
    }

    #[tokio::test]
    async fn rename_project_handler_toctou_race_active_conns_lands_before_stop_gets_409() {
        let (dir, state) = test_state().await;
        let uid = seed_real_sysadmin(&state, "root").await;
        register(
            &state,
            "racer-ren",
            &dir.path().join("workspaces").join("racer-ren"),
        );
        let identity = identity_for(&uid, true, HashSet::new());

        let lock = state.runtime.ensure_lock("racer-ren", "backend");
        let held = lock.lock_owned().await;

        let state2 = state.clone();
        let task = tokio::spawn(async move {
            rename_project_handler(
                State(state2),
                Extension(identity),
                Path("racer-ren".to_string()),
                HeaderMap::new(),
                Bytes::from_static(br#"{"name": "racer-ren-2"}"#),
            )
            .await
        });
        let_spawned_task_reach_its_lock_wait().await;

        state
            .runtime
            .with_runtime_mut("racer-ren", |rt| rt.active_conns = 3);
        drop(held);

        let resp = task.await.unwrap();
        assert_eq!(resp.status(), 409, "{:?}", json_body(resp).await);
        assert!(state.registry.get("racer-ren").unwrap().is_some());
        assert!(state.registry.get("racer-ren-2").unwrap().is_none());
    }

    #[tokio::test]
    async fn stop_project_handler_toctou_race_active_conns_lands_before_stop_gets_409() {
        let (dir, state) = test_state().await;
        let uid = seed_real_sysadmin(&state, "root").await;
        register(
            &state,
            "racer-stop",
            &dir.path().join("workspaces").join("racer-stop"),
        );
        let identity = identity_for(&uid, true, HashSet::new());

        let lock = state.runtime.ensure_lock("racer-stop", "backend");
        let held = lock.lock_owned().await;

        let state2 = state.clone();
        let task = tokio::spawn(async move {
            stop_project_handler(
                State(state2),
                Extension(identity),
                Path("racer-stop".to_string()),
                HeaderMap::new(),
            )
            .await
        });
        let_spawned_task_reach_its_lock_wait().await;

        state
            .runtime
            .with_runtime_mut("racer-stop", |rt| rt.active_conns = 1);
        drop(held);

        let resp = task.await.unwrap();
        assert_eq!(resp.status(), 409, "{:?}", json_body(resp).await);
        assert!(state.registry.get("racer-stop").unwrap().is_some());
    }

    // -- test_sec_r7f1_project_lifecycle_toctou.py Tests E/F: a caller
    // whose group-delegated capability is revoked WHILE their delete/
    // stop request is blocked acquiring the per-project ensure_lock
    // must be re-checked before the destructive op runs. -----------------

    #[tokio::test]
    async fn delete_project_handler_denies_a_capability_revoked_while_blocked_on_the_lock() {
        let (dir, state) = test_state().await;
        register(
            &state,
            "race-delete-project",
            &dir.path().join("workspaces").join("race-delete-project"),
        );
        let (_uid, group_id, identity) =
            seed_delegate_with_membership(&state, "alice", "race-delete-project", "operator").await;

        let lock = state.runtime.ensure_lock("race-delete-project", "backend");
        let held = lock.lock_owned().await;

        let state2 = state.clone();
        let task = tokio::spawn(async move {
            delete_project_handler(
                State(state2),
                Extension(identity),
                Path("race-delete-project".to_string()),
                Query(HashMap::new()),
                HeaderMap::new(),
            )
            .await
        });
        let_spawned_task_reach_its_lock_wait().await;

        revoke_delegate_capability(&state, &group_id).await;
        drop(held);

        let resp = task.await.unwrap();
        assert_eq!(resp.status(), 403, "{:?}", json_body(resp).await);
        assert!(
            state.registry.get("race-delete-project").unwrap().is_some(),
            "project must NOT have been deleted off alice's stale, pre-revocation grant"
        );
    }

    #[tokio::test]
    async fn stop_project_handler_denies_a_capability_revoked_while_blocked_on_the_lock() {
        let (dir, state) = test_state().await;
        register(
            &state,
            "race-stop-project",
            &dir.path().join("workspaces").join("race-stop-project"),
        );
        let (_uid, group_id, identity) =
            seed_delegate_with_membership(&state, "alice", "race-stop-project", "operator").await;

        let lock = state.runtime.ensure_lock("race-stop-project", "backend");
        let held = lock.lock_owned().await;

        let state2 = state.clone();
        let task = tokio::spawn(async move {
            stop_project_handler(
                State(state2),
                Extension(identity),
                Path("race-stop-project".to_string()),
                HeaderMap::new(),
            )
            .await
        });
        let_spawned_task_reach_its_lock_wait().await;

        revoke_delegate_capability(&state, &group_id).await;
        drop(held);

        let resp = task.await.unwrap();
        assert_eq!(resp.status(), 403, "{:?}", json_body(resp).await);
    }

    // -- test_sec_r13f1_rename_lock_toctou.py: rename's SECOND, wholly
    // independent yield point -- acquiring the ensure_lock itself --
    // must be re-checked too, not just the body-read (rename's FIRST
    // yield point, already covered by the create/rename-shaped tests
    // elsewhere). Both the capability-only and membership-only
    // revocation shapes the finding calls out. ---------------------------

    #[tokio::test]
    async fn rename_project_handler_denies_a_capability_revoked_while_blocked_on_the_lock() {
        let (dir, state) = test_state().await;
        register(
            &state,
            "race-rename-lock",
            &dir.path().join("workspaces").join("race-rename-lock"),
        );
        let (_uid, group_id, identity) =
            seed_delegate_with_membership(&state, "alice", "race-rename-lock", "operator").await;

        let lock = state.runtime.ensure_lock("race-rename-lock", "backend");
        let held = lock.lock_owned().await;

        let state2 = state.clone();
        let task = tokio::spawn(async move {
            rename_project_handler(
                State(state2),
                Extension(identity),
                Path("race-rename-lock".to_string()),
                HeaderMap::new(),
                Bytes::from_static(br#"{"name": "renamed-race-lock", "grace_days": 7}"#),
            )
            .await
        });
        let_spawned_task_reach_its_lock_wait().await;

        revoke_delegate_capability(&state, &group_id).await;
        drop(held);

        let resp = task.await.unwrap();
        assert_eq!(resp.status(), 403, "{:?}", json_body(resp).await);
        assert!(
            state.registry.get("race-rename-lock").unwrap().is_some(),
            "project must NOT have been renamed off alice's stale, pre-revocation grant"
        );
        assert!(state.registry.get("renamed-race-lock").unwrap().is_none());
    }

    #[tokio::test]
    async fn rename_project_handler_denies_a_membership_revoked_while_blocked_on_the_lock() {
        let (dir, state) = test_state().await;
        register(
            &state,
            "race-rename-lock-membership",
            &dir.path()
                .join("workspaces")
                .join("race-rename-lock-membership"),
        );
        let (uid, _group_id, identity) = seed_delegate_with_membership(
            &state,
            "alice",
            "race-rename-lock-membership",
            "operator",
        )
        .await;

        let lock = state
            .runtime
            .ensure_lock("race-rename-lock-membership", "backend");
        let held = lock.lock_owned().await;

        let state2 = state.clone();
        let task = tokio::spawn(async move {
            rename_project_handler(
                State(state2),
                Extension(identity),
                Path("race-rename-lock-membership".to_string()),
                HeaderMap::new(),
                Bytes::from_static(br#"{"name": "renamed-race-lock-membership", "grace_days": 7}"#),
            )
            .await
        });
        let_spawned_task_reach_its_lock_wait().await;

        revoke_delegate_membership(&state, "race-rename-lock-membership", &uid).await;
        drop(held);

        let resp = task.await.unwrap();
        // Capability is left intact; only membership is stripped. Verified
        // against the real Python source
        // (`agent_mcp/router/admin_api.py::_revalidate_capability_and_
        // membership_or_403`): it composes `revalidate_capability_or_403`
        // with `_deny_cross_tenant_project_read` -- the SAME function used
        // at entry time -- which returns the uniform 404 `unknown_project`
        // envelope whenever the caller's resolved role is `None`, with NO
        // structural distinction between "never a member" (entry time) and
        // "membership revoked mid-flight" (this test). An earlier version
        // of this test (and `perm_gates.rs`'s own
        // `revalidate_after_catches_a_membership_revocation_that_lands_
        // during_a_real_concurrent_await`) wrongly asserted a deliberate
        // 403-for-mid-flight-revocation divergence that does not exist in
        // Python -- both were corrected to 404 once the real source was
        // read directly rather than assumed.
        assert_eq!(resp.status(), 404, "{:?}", json_body(resp).await);
        assert!(
            state
                .registry
                .get("race-rename-lock-membership")
                .unwrap()
                .is_some(),
            "project must NOT have been renamed off alice's stale, pre-revocation membership"
        );
        assert!(state
            .registry
            .get("renamed-race-lock-membership")
            .unwrap()
            .is_none());
    }

    // -- test_sec_r36_lifecycle_parity.py (BL-R36-1): stop must pop the
    // per-name orchestrator state exactly like delete/rename -- no
    // concurrency needed, a direct handler call proves it. ---------------

    #[tokio::test]
    async fn stop_project_handler_pops_the_project_orchestrator_state() {
        let (dir, state) = test_state().await;
        let uid = seed_real_sysadmin(&state, "root").await;
        register(
            &state,
            "winddown",
            &dir.path().join("workspaces").join("winddown"),
        );
        state.runtime.with_runtime_mut("winddown", |rt| {
            rt.last_active
                .insert("backend".into(), std::time::SystemTime::now())
        });
        assert!(state.runtime.snapshot("winddown").is_some());

        let identity = identity_for(&uid, true, HashSet::new());
        let resp = stop_project_handler(
            State(state.clone()),
            Extension(identity),
            Path("winddown".to_string()),
            HeaderMap::new(),
        )
        .await;
        assert_eq!(resp.status(), 200, "{:?}", json_body(resp).await);
        assert!(
            state.runtime.snapshot("winddown").is_none(),
            "stop must clear the per-name orchestrator state (BL-R36-1)"
        );
    }

    // -- test_sec_r35_rename_warmstart_lock.py (BL-R35-1): rename must
    // pop the OLD-name orchestrator state and purge its runtime dir,
    // exactly like delete does -- direct handler calls, no concurrency
    // needed to prove the state mutation itself (the concurrent-warm-
    // start-doesn't-start-a-backend half of BL-R35-1 is proven at the
    // `orchestrator::ensure` layer by `ensure_aborts_when_project_
    // renamed_while_lock_held`, and the lock-is-HELD-across-the-stop
    // half follows structurally from `revalidated_lock`'s own guard
    // never dropping until after `finish_rename_project` returns --
    // see this handler's own SC-R8-1 comment on `drop(_lock_guard)`). --

    #[tokio::test]
    async fn rename_project_handler_pops_the_old_name_orchestrator_state() {
        let (dir, state) = test_state().await;
        let uid = seed_real_sysadmin(&state, "root").await;
        register(
            &state,
            "ephemeral",
            &dir.path().join("workspaces").join("ephemeral"),
        );
        state.runtime.with_runtime_mut("ephemeral", |rt| {
            rt.last_active
                .insert("backend".into(), std::time::SystemTime::now())
        });
        assert!(state.runtime.snapshot("ephemeral").is_some());

        let identity = identity_for(&uid, true, HashSet::new());
        let resp = rename_project_handler(
            State(state.clone()),
            Extension(identity),
            Path("ephemeral".to_string()),
            HeaderMap::new(),
            Bytes::from_static(br#"{"name": "renamed"}"#),
        )
        .await;
        assert_eq!(resp.status(), 200, "{:?}", json_body(resp).await);
        assert!(
            state.runtime.snapshot("ephemeral").is_none(),
            "rename must clear the OLD-name orchestrator state (BL-R35-1)"
        );
    }

    #[tokio::test]
    async fn rename_project_handler_purges_the_old_name_runtime_dir() {
        let (dir, state) = test_state().await;
        let uid = seed_real_sysadmin(&state, "root").await;
        register(&state, "runt", &dir.path().join("workspaces").join("runt"));
        let runtime_dir = state.sock_dir.join("runt");
        std::fs::create_dir_all(&runtime_dir).unwrap();
        std::fs::write(runtime_dir.join("backend.sock"), b"").unwrap();
        std::fs::write(runtime_dir.join("forwarding_hmac"), b"k".repeat(32)).unwrap();

        let identity = identity_for(&uid, true, HashSet::new());
        let resp = rename_project_handler(
            State(state.clone()),
            Extension(identity),
            Path("runt".to_string()),
            HeaderMap::new(),
            Bytes::from_static(br#"{"name": "grown"}"#),
        )
        .await;
        assert_eq!(resp.status(), 200, "{:?}", json_body(resp).await);
        assert!(
            !runtime_dir.exists(),
            "rename must purge the OLD-name runtime dir (stale socket + HMAC key, BL-R35-1)"
        );
    }

    // ====================================================================
    // Genuine concurrent-REQUEST races that need a real, controllable
    // `systemctl stop` shell-out to park one request mid-critical-section
    // while a second lands (test_sec_r36_lifecycle_parity.py's PF-R36-1 /
    // test_sec_r37_rename_error_mapping.py's PF-R37-1) -- mirrors
    // `orchestrator::ensure`'s own `write_fake_systemctl_blocking_on_is_
    // active` idiom, applied to `stop` instead.
    // ====================================================================

    fn fast_ensure_config(program: &std::path::Path) -> EnsureConfig {
        EnsureConfig {
            systemctl_program: program.to_str().unwrap().to_string(),
            systemctl_mode: crate::orchestrator::primitives::SystemctlMode::User,
            systemctl_timeout: std::time::Duration::from_secs(5),
            ensure_failure_cooldown: std::time::Duration::from_millis(200),
            boot_grace: std::time::Duration::from_millis(150),
            socket_poll_attempts: 5,
            max_restart_attempts: 5,
            giveup_cooldown: std::time::Duration::from_secs(600),
        }
    }

    async fn test_state_with_ensure_config(
        ensure_config: EnsureConfig,
    ) -> (tempfile::TempDir, Arc<RouterState>) {
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        init_router_schema(&conn).unwrap();
        let dir = tempfile::TempDir::new().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let sea_orm_db = sea_orm::Database::connect("sqlite::memory:").await.unwrap();
        let state = Arc::new(RouterState::new(
            conn,
            sea_orm_db,
            registry,
            RateLimitConfig::resolve_from_process_env(),
            ensure_config,
            test_state_config(dir.path()),
        ));
        (dir, state)
    }

    /// A disposable fake `systemctl`: `stop` against a unit whose name
    /// contains `block_unit_substr` touches `started` then blocks
    /// (bounded, so a genuine regression fails the test rather than
    /// hanging the suite) until `release` appears; every OTHER `stop`
    /// (a different project's unit) and every `is-active` returns
    /// immediately with the caller-chosen codes.
    fn write_fake_systemctl_blocking_on_stop(
        dir: &std::path::Path,
        block_unit_substr: &str,
        started: &std::path::Path,
        release: &std::path::Path,
        is_active_rc: i32,
    ) -> (std::path::PathBuf, std::path::PathBuf) {
        let log = dir.join("calls.log");
        let script_path = dir.join("fake-systemctl-stop-block.sh");
        let script = format!(
            r#"#!/bin/sh
echo "$@" >> "{log}"
verb=""
unit=""
for a in "$@"; do
  case "$a" in
    is-active|start|restart|stop) verb="$a" ;;
    --user) ;;
    *) unit="$a" ;;
  esac
done
if [ "$verb" = "stop" ]; then
  case "$unit" in
    *{block_unit_substr}*)
      touch "{started}"
      i=0
      while [ ! -f "{release}" ] && [ $i -lt 200 ]; do
        sleep 0.05
        i=$((i+1))
      done
      ;;
  esac
  exit 0
fi
if [ "$verb" = "is-active" ]; then
  exit {is_active_rc}
fi
exit 0
"#,
            log = log.display(),
            started = started.display(),
            release = release.display(),
        );
        std::fs::write(&script_path, script).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut perms = std::fs::metadata(&script_path).unwrap().permissions();
            perms.set_mode(0o755);
            std::fs::set_permissions(&script_path, perms).unwrap();
        }
        (script_path, log)
    }

    async fn wait_for_marker(path: &std::path::Path, what: &str) {
        for _ in 0..200 {
            if path.exists() {
                return;
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        panic!("{what} never happened -- marker file {path:?} was never created");
    }

    // -- test_sec_r36_lifecycle_parity.py (PF-R36-1): two concurrent
    // renames of the SAME project. The winner renames it away; the
    // loser, on acquiring the lock next, must re-check existence INSIDE
    // the lock and return a clean 404 -- never let `_REGISTRY.rename`'s
    // `UnknownProject` escape as a 500. -----------------------------------

    #[tokio::test]
    async fn concurrent_rename_of_the_same_project_loser_gets_404_not_500() {
        let dir = tempfile::tempdir().unwrap();
        let started = dir.path().join("started");
        let release = dir.path().join("release");
        let (program, _log) =
            write_fake_systemctl_blocking_on_stop(dir.path(), "contended", &started, &release, 3);
        let (state_dir, state) = test_state_with_ensure_config(fast_ensure_config(&program)).await;
        let uid = seed_real_sysadmin(&state, "root").await;
        register(
            &state,
            "contended",
            &state_dir.path().join("workspaces").join("contended"),
        );

        // Winner: contended -> winner. Parks in its systemctl stop
        // (holding the ensure_lock).
        let winner_state = state.clone();
        let winner_identity = identity_for(&uid, true, HashSet::new());
        let winner = tokio::spawn(async move {
            rename_project_handler(
                State(winner_state),
                Extension(winner_identity),
                Path("contended".to_string()),
                HeaderMap::new(),
                Bytes::from_static(br#"{"name": "winner"}"#),
            )
            .await
        });
        wait_for_marker(&started, "winner rename never reached its systemctl stop").await;

        // Loser: contended -> loser. Passes its outside-lock probe
        // (contended is still registered -- winner is parked BEFORE its
        // registry.rename), then blocks on the SAME ensure_lock.
        let loser_state = state.clone();
        let loser_identity = identity_for(&uid, true, HashSet::new());
        let loser = tokio::spawn(async move {
            rename_project_handler(
                State(loser_state),
                Extension(loser_identity),
                Path("contended".to_string()),
                HeaderMap::new(),
                Bytes::from_static(br#"{"name": "loser"}"#),
            )
            .await
        });
        let_spawned_task_reach_its_lock_wait().await;

        std::fs::write(&release, b"go").unwrap();
        let winner_resp = winner.await.unwrap();
        assert_eq!(
            winner_resp.status(),
            200,
            "{:?}",
            json_body(winner_resp).await
        );

        let loser_resp = loser.await.unwrap();
        assert_ne!(
            loser_resp.status(),
            500,
            "the losing concurrent rename must not surface a 500"
        );
        assert_eq!(
            loser_resp.status(),
            404,
            "{:?}",
            json_body(loser_resp).await
        );
    }

    // -- test_sec_r37_rename_error_mapping.py (PF-R37-1): the registry's
    // atomic ProjectNameTaken guard is the ONLY backstop for two renames
    // with DIFFERENT old names racing the SAME new name (`ensure_lock`
    // keys on old_name, so they never serialize against each other) --
    // it must map to 409 name_taken, never a 500. -------------------------

    #[tokio::test]
    async fn concurrent_rename_of_different_projects_to_the_same_new_name_loser_gets_409_not_500() {
        let dir = tempfile::tempdir().unwrap();
        let started = dir.path().join("started");
        let release = dir.path().join("release");
        // Only the LOSER's unit (losesrc) blocks; the winner's own stop
        // call runs to completion immediately.
        let (program, _log) =
            write_fake_systemctl_blocking_on_stop(dir.path(), "losesrc", &started, &release, 3);
        let (state_dir, state) = test_state_with_ensure_config(fast_ensure_config(&program)).await;
        let uid = seed_real_sysadmin(&state, "root").await;
        register(
            &state,
            "winsrc",
            &state_dir.path().join("ws").join("ws_winsrc"),
        );
        register(
            &state,
            "losesrc",
            &state_dir.path().join("ws").join("ws_losesrc"),
        );

        // Loser: losesrc -> shared. Parks in its systemctl stop.
        let loser_state = state.clone();
        let loser_identity = identity_for(&uid, true, HashSet::new());
        let loser = tokio::spawn(async move {
            rename_project_handler(
                State(loser_state),
                Extension(loser_identity),
                Path("losesrc".to_string()),
                HeaderMap::new(),
                Bytes::from_static(br#"{"name": "shared"}"#),
            )
            .await
        });
        wait_for_marker(&started, "loser rename never reached its systemctl stop").await;

        // Winner: winsrc -> shared. Its own stop call doesn't block, so
        // it runs to completion, claiming "shared" as a real project.
        let winner_identity = identity_for(&uid, true, HashSet::new());
        let winner_resp = rename_project_handler(
            State(state.clone()),
            Extension(winner_identity),
            Path("winsrc".to_string()),
            HeaderMap::new(),
            Bytes::from_static(br#"{"name": "shared"}"#),
        )
        .await;
        assert_eq!(
            winner_resp.status(),
            200,
            "{:?}",
            json_body(winner_resp).await
        );

        // Release the loser; its registry.rename() now hits the
        // already-taken name.
        std::fs::write(&release, b"go").unwrap();
        let loser_resp = loser.await.unwrap();
        assert_ne!(
            loser_resp.status(),
            500,
            "the losing concurrent rename must not surface a 500"
        );
        assert_eq!(
            loser_resp.status(),
            409,
            "{:?}",
            json_body(loser_resp).await
        );
    }

    #[tokio::test]
    async fn rename_racing_a_create_of_the_same_new_name_gets_409_not_500() {
        let dir = tempfile::tempdir().unwrap();
        let started = dir.path().join("started");
        let release = dir.path().join("release");
        let (program, _log) =
            write_fake_systemctl_blocking_on_stop(dir.path(), "mover", &started, &release, 3);
        let (state_dir, state) = test_state_with_ensure_config(fast_ensure_config(&program)).await;
        let uid = seed_real_sysadmin(&state, "root").await;
        register(
            &state,
            "mover",
            &state_dir.path().join("ws").join("ws_mover"),
        );

        // Rename mover -> fresh. Parks in its systemctl stop.
        let rename_state = state.clone();
        let rename_identity = identity_for(&uid, true, HashSet::new());
        let rename_task = tokio::spawn(async move {
            rename_project_handler(
                State(rename_state),
                Extension(rename_identity),
                Path("mover".to_string()),
                HeaderMap::new(),
                Bytes::from_static(br#"{"name": "fresh"}"#),
            )
            .await
        });
        wait_for_marker(&started, "rename never reached its systemctl stop").await;

        // Create "fresh" as a real project while the rename is parked.
        let create_identity = identity_for(&uid, true, HashSet::new());
        let create_resp = create_project_handler(
            State(state.clone()),
            Extension(create_identity),
            HeaderMap::new(),
            Bytes::from_static(br#"{"name": "fresh"}"#),
        )
        .await;
        assert_eq!(
            create_resp.status(),
            201,
            "{:?}",
            json_body(create_resp).await
        );

        std::fs::write(&release, b"go").unwrap();
        let rename_resp = rename_task.await.unwrap();
        assert_ne!(
            rename_resp.status(),
            500,
            "a rename racing a create of the same new name must not 500"
        );
        assert_eq!(
            rename_resp.status(),
            409,
            "{:?}",
            json_body(rename_resp).await
        );
    }

    /// PF-R20-1 (test_sec_r20_json_recursion_depth.py, Site 1: `POST
    /// /api/router/projects`): Python's `json.loads` raises an
    /// uncaught `RecursionError` (a `RuntimeError`, not caught by the
    /// `except json.JSONDecodeError` guard) on a body nested past the
    /// interpreter's recursion limit -- a bare 500. In Rust the
    /// concern is worse in kind (a native stack overflow would abort
    /// the WHOLE PROCESS, not just fail one request -- see
    /// `json_sanitize.rs`'s own module doc), but this handler already
    /// routes its body through `perm_gates::read_body_and_revalidate`
    /// -> `json_sanitize::decode_untrusted_body`, whose pre-parse
    /// raw-byte nesting-depth scan rejects a body this deep BEFORE
    /// `serde_json` ever sees it. Verified here end-to-end through the
    /// real handler (not just `json_sanitize`'s own unit tests) with
    /// the identical ~10k-deep repro shape the Python finding used.
    #[tokio::test]
    async fn create_project_handler_denies_deep_json_with_a_clean_400_not_a_crash() {
        let (_dir, state) = test_state().await;
        let uid = seed_real_sysadmin(&state, "root").await;
        let identity = identity_for(&uid, true, HashSet::new());

        const DEEP_DEPTH: usize = 10_000;
        let mut deep_body = "[".repeat(DEEP_DEPTH);
        deep_body.push_str(&"]".repeat(DEEP_DEPTH));

        let resp = create_project_handler(
            State(state.clone()),
            Extension(identity),
            HeaderMap::new(),
            Bytes::from(deep_body),
        )
        .await;
        assert_eq!(resp.status(), 400, "{:?}", json_body(resp).await);
    }

    // -- Phase G (router step 4 PR C): project_membership now flows
    // through `state.sea_orm_db`, not the legacy rusqlite `conn` --
    // verify the REAL handlers persist it end-to-end, not just
    // `identity.rs`'s own unit tests of the underlying functions. ----

    #[tokio::test]
    async fn create_project_handler_grants_the_creator_membership_via_sea_orm() {
        let (_dir, state) = test_state_file_backed().await;
        let uid = seed_real_sysadmin(&state, "root").await;
        let identity = identity_for(&uid, true, HashSet::new());

        let resp = create_project_handler(
            State(state.clone()),
            Extension(identity),
            HeaderMap::new(),
            Bytes::from_static(br#"{"name": "proj-new"}"#),
        )
        .await;
        assert_eq!(resp.status(), 201, "{:?}", json_body(resp).await);

        let role =
            identity::project_membership_role(&state.sea_orm_db, "proj-new", Some(&uid), None)
                .await
                .unwrap();
        assert_eq!(
            role.as_deref(),
            Some("operator"),
            "the creator must be granted operator-tier membership via sea-orm"
        );
    }

    #[tokio::test]
    async fn rename_project_handler_rekeys_project_membership_via_sea_orm() {
        let (dir, state) = test_state_file_backed().await;
        let uid = seed_real_sysadmin(&state, "root").await;
        register(
            &state,
            "old-name",
            &dir.path().join("workspaces").join("old-name"),
        );
        identity::add_project_membership(&state.sea_orm_db, &uid, "old-name")
            .await
            .unwrap();

        let identity = identity_for(&uid, true, HashSet::new());
        let resp = rename_project_handler(
            State(state.clone()),
            Extension(identity),
            Path("old-name".to_string()),
            HeaderMap::new(),
            Bytes::from_static(br#"{"name": "new-name", "grace_days": 7}"#),
        )
        .await;
        assert_eq!(resp.status(), 200, "{:?}", json_body(resp).await);

        assert!(
            identity::project_membership_role(&state.sea_orm_db, "old-name", Some(&uid), None)
                .await
                .unwrap()
                .is_none(),
            "the OLD project name must carry no membership row after rename"
        );
        assert_eq!(
            identity::project_membership_role(&state.sea_orm_db, "new-name", Some(&uid), None)
                .await
                .unwrap()
                .as_deref(),
            Some("operator"),
            "the rekey must land under the NEW project name via sea-orm"
        );
    }

    #[tokio::test]
    async fn delete_project_handler_purges_project_membership_via_sea_orm() {
        let (dir, state) = test_state_file_backed().await;
        let uid = seed_real_sysadmin(&state, "root").await;
        register(
            &state,
            "proj-a",
            &dir.path().join("workspaces").join("proj-a"),
        );
        identity::add_project_membership(&state.sea_orm_db, &uid, "proj-a")
            .await
            .unwrap();
        assert!(
            identity::project_membership_role(&state.sea_orm_db, "proj-a", Some(&uid), None)
                .await
                .unwrap()
                .is_some(),
            "fixture setup sanity check"
        );

        let identity = identity_for(&uid, true, HashSet::new());
        let resp = delete_project_handler(
            State(state.clone()),
            Extension(identity),
            Path("proj-a".to_string()),
            Query(HashMap::new()),
            HeaderMap::new(),
        )
        .await;
        assert_eq!(resp.status(), 200, "{:?}", json_body(resp).await);

        assert!(
            identity::project_membership_role(&state.sea_orm_db, "proj-a", Some(&uid), None)
                .await
                .unwrap()
                .is_none(),
            "delete must purge every project_membership row via sea-orm"
        );
    }
}
