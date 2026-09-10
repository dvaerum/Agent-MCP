//! `/mcp` HTTP-layer identity gate. Port of `agent_mcp/app/main_app.py`'s
//! `AuthHeaderMiddleware` responsibility #3 ("Gate `/mcp` at the HTTP
//! layer... must carry either a per-agent bearer... OR a verified
//! forwarding header") + responsibility #4 (stamp the resolved
//! Principal for downstream handlers) -- combined into one axum
//! middleware layered in front of the rmcp `nest_service`, matching
//! `pikvm_mcp_server::http_server::require_auth`'s own shape (a
//! `middleware::from_fn_with_state` wrapping the `/mcp` nest, not a
//! hand-rolled transport).
//!
//! A request with no admitted identity is rejected here, before rmcp's
//! own JSON-RPC framing ever runs -- there is no anonymous path onto
//! `/mcp`, matching Python's own gate exactly.

use std::sync::Arc;

use axum::extract::{Request, State};
use axum::http::StatusCode;
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use axum::Json;

use conexus_core::principal::Principal;

use crate::principal_resolve::resolve_principal;
use crate::server::SharedState;

/// The resolved caller identity, stamped onto the request's
/// extensions by this middleware and read back by
/// `ConexusServer::call_tool` via `RequestContext::extensions` (see
/// that module's doc comment for why this round-trips through rmcp's
/// own `Parts`-threading rather than needing a second DB lookup).
#[derive(Clone)]
pub struct ResolvedPrincipal(pub Principal);

fn unauthorized_response(reason: &str) -> Response {
    let body = Json(serde_json::json!({
        "jsonrpc": "2.0",
        "error": {"code": -32001, "message": reason},
        "id": null,
    }));
    (StatusCode::UNAUTHORIZED, body).into_response()
}

pub async fn require_identity(
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
    // Phase G: `resolve_principal` is `async fn` now (it needs to
    // `.await` a sea-orm `project_settings` read for wake-loop
    // eligibility) and does its OWN internal locking of `shared.conn`
    // in a confined scope -- passing the un-locked `&AsyncMutex<..>`
    // handle straight through here, rather than pre-locking a guard
    // and handing that in, is what keeps its generated future `Send`
    // (see `resolve_principal`'s own doc comment for the full rule).
    let resolved = resolve_principal(
        &shared.conn,
        &shared.sea_orm_db,
        authorization.as_deref(),
        forwarding_header_value.as_deref(),
        shared.forwarding_hmac_key.as_deref(),
        now_unix,
    )
    .await;

    match resolved {
        Ok(principal) => {
            request
                .extensions_mut()
                .insert(ResolvedPrincipal(principal));
            next.run(request).await
        }
        Err(rejected) => unauthorized_response(&rejected.reason),
    }
}
