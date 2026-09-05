//! `/api/delivery/*` HTTP-layer identity gate. Port of
//! `agent_mcp/app/routers/delivery.py::require_agent_bearer`.
//!
//! A THIRD `/api` admission shape, distinct from both `/mcp`'s
//! `auth_gate` (admits any valid agent bearer OR forwarding header)
//! and `rest_gate` (admits a forwarding header or an OPERATOR-TIER
//! bearer only, explicitly rejecting a worker bearer) -- this door
//! admits a WORKER's own bearer token, the same one its MCP tools
//! use. `/api/delivery/*` is per-worker fallback-transport plumbing
//! (ADR-0021), not a dashboard surface, so neither existing gate
//! shape fits: mounting it under `rest_gate` would 401 every real
//! caller (a worker bearer), and `auth_gate` also admits the
//! forwarding header, which has no meaning here (this channel is
//! keyed by `agent_id`, which a forwarding-header caller doesn't
//! carry). A standalone, single-purpose gate is the correct shape,
//! not a third branch bolted onto either existing one.

use std::sync::Arc;

use axum::extract::{Request, State};
use axum::http::StatusCode;
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use axum::Json;

use crate::server::SharedState;

/// The resolved delivery-transport caller identity -- just the
/// `agent_id`, stamped onto the request's extensions and read back by
/// both `/api/delivery/*` handlers. No `Principal`/capability
/// resolution here (unlike `rest_gate`'s `ResolvedRestPrincipal`):
/// this channel has no notion of authorization beyond "a live agent's
/// own bearer, for its own stream" -- there is nothing else to gate.
#[derive(Clone)]
pub struct ResolvedDeliveryIdentity {
    pub agent_id: String,
}

fn unauthorized() -> Response {
    (
        StatusCode::UNAUTHORIZED,
        Json(serde_json::json!({"detail": "agent bearer required"})),
    )
        .into_response()
}

/// Port of `require_agent_bearer`. R13-F2: existence is not liveness
/// -- resolving the token to a row (`AgentRepository::get_by_token`,
/// "NOT an auth gate" per its own docs) is only step one; the row must
/// also satisfy `AgentRepository::is_live` (Python's `LIVE_AGENT_SQL`,
/// byte-for-byte the same `status NOT IN ('terminated', 'tombstone')`
/// predicate `is_active_agent` uses) so a terminated/tombstone bearer
/// -- which still RESOLVES here even though it 401s on `/mcp` -- is
/// rejected too, closing the same liveness-vs-existence class on this
/// path. A `__tombstone_` -prefixed token (the purge-cascade's FK
/// placeholder, never a real bearer) is rejected up front, before a
/// DB round-trip, matching Python's own belt-and-braces ordering.
pub async fn require_delivery_agent_bearer(
    State(shared): State<Arc<SharedState>>,
    mut request: Request,
    next: Next,
) -> Response {
    let token = request
        .headers()
        .get(axum::http::header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| {
            v.strip_prefix("Bearer ")
                .or_else(|| v.strip_prefix("bearer "))
        })
        .map(str::trim);

    let Some(token) = token.filter(|t| !t.is_empty()) else {
        return unauthorized();
    };
    if token.starts_with("__tombstone_") {
        return unauthorized();
    }

    let guard = shared.conn.lock().await;
    let row = conexus_db::agent_repository::AgentRepository::get_by_token(&guard, token);
    drop(guard);

    match row {
        Ok(Some(row)) if row.status != "terminated" && row.status != "tombstone" => {
            request.extensions_mut().insert(ResolvedDeliveryIdentity {
                agent_id: row.agent_id,
            });
            next.run(request).await
        }
        _ => unauthorized(),
    }
}
