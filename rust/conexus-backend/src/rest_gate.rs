//! `/api` HTTP-layer identity gate. Port of `agent_mcp/app/deps.py::
//! require_operator_session`'s HTTP-layer responsibility, narrowed to
//! the two doors [`crate::rest_principal`] keeps (forwarding-header +
//! operator-tier bearer — see that module's doc for why the cookie
//! door is dropped).
//!
//! Structurally identical to [`crate::auth_gate::require_identity`]
//! (`/mcp`'s gate): resolve on the way in, stamp the extensions,
//! reject before any handler runs. Deliberately a SEPARATE middleware
//! rather than a generalization of `auth_gate::require_identity` —
//! the two doors resolve to a different type ([`RestPrincipal`], not
//! [`Principal`]) with different admission rules (a worker bearer is
//! valid on `/mcp`, rejected here) and a different error body shape
//! (REST's `{"error": ..., "message": ...}` vs `/mcp`'s JSON-RPC
//! envelope) — collapsing them would either leak MCP's JSON-RPC error
//! shape onto REST responses or weaken `/mcp`'s worker-admission.

use std::sync::Arc;

use axum::extract::{Request, State};
use axum::http::StatusCode;
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use axum::Json;

use conexus_core::principal::Principal;

use crate::rest_principal::{
    build_dispatch_principal, is_confirmed_operator_tier, resolve_rest_principal, RestPrincipal,
};
use crate::server::SharedState;

/// The resolved REST caller identity, stamped onto the request's
/// extensions by this middleware and read back by each `/api` handler.
/// Carries all three facts a handler might need, precomputed once per
/// request rather than re-derived by every handler that wants one:
/// the raw admission (which door, for anything door-specific), the
/// dispatch-ready `Principal` (what `_dispatch_through_tool`-shaped
/// handlers pass to the tool dispatcher), and the REST-specific
/// confirmed-operator-tier flag (the secret-exposure gate a handful of
/// endpoints consult). Mirrors `auth_gate::ResolvedPrincipal`'s own
/// "stamp once in the gate" shape for `/mcp`.
// PR1 (this scaffold) has no `/api` handler yet to read these back out
// via `Extension<ResolvedRestPrincipal>` -- the very next PR
// (`conexus-rest-settings-static`) is the first real consumer. `pub`
// alone doesn't exempt a BINARY crate's items from dead_code the way
// it does in this workspace's library crates (every prior
// "helper ahead of its first consumer" PR was in a lib crate, where
// `pub` items count as the crate's public API and are never dead by
// definition) -- this is genuinely the first such case in a binary
// crate, so there's no established pattern to match here.
#[allow(dead_code)]
#[derive(Clone)]
pub struct ResolvedRestPrincipal {
    pub admission: RestPrincipal,
    pub dispatch_principal: Principal,
    pub confirmed_operator_tier: bool,
}

fn unauthorized_response(reason: &str) -> Response {
    let body = Json(serde_json::json!({
        "error": "login_required",
        "message": reason,
    }));
    (StatusCode::UNAUTHORIZED, body).into_response()
}

pub async fn require_rest_identity(
    State(shared): State<Arc<SharedState>>,
    mut request: Request,
    next: Next,
) -> Response {
    let authorization = request
        .headers()
        .get(axum::http::header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .map(str::to_string);
    let forwarding_header_value = request
        .headers()
        .get(conexus_auth::forwarding_header::HEADER_NAME)
        .and_then(|v| v.to_str().ok())
        .map(str::to_string);

    let now_unix = chrono::Utc::now().timestamp() as u64;
    let conn = shared.conn.lock().await;
    let resolved = resolve_rest_principal(
        &conn,
        authorization.as_deref(),
        forwarding_header_value.as_deref(),
        shared.forwarding_hmac_key.as_deref(),
        now_unix,
    );
    drop(conn);

    match resolved {
        Ok(admission) => {
            let dispatch_principal = build_dispatch_principal(&admission);
            let confirmed_operator_tier = is_confirmed_operator_tier(&admission);
            request.extensions_mut().insert(ResolvedRestPrincipal {
                admission,
                dispatch_principal,
                confirmed_operator_tier,
            });
            next.run(request).await
        }
        Err(rejected) => unauthorized_response(&rejected.reason),
    }
}
