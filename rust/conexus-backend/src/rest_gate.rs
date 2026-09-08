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

/// test_sec_r28_composition_read_authz.py / test_sec_composition_secret_
/// exposure.py's auth-gate scenarios ("no auth at all -> 401",
/// "a signed forwarding operator -> 200", "operator-tier bearer can
/// write"): this middleware's 401-vs-200 admission decision runs BEFORE
/// any handler, so calling a handler function directly (the pattern the
/// rest of this crate's handler-level tests use) bypasses it entirely --
/// these need a real bound socket + the actual middleware-wrapped router,
/// mirroring `uds::serve_router_unix`'s own test pattern exactly (same
/// hyper-over-UnixStream client, no extra Unix-socket connector crate).
#[cfg(test)]
mod tests {
    use super::*;

    /// A real temp-file-backed connection shared with a sea-orm
    /// connection to the SAME file -- `/create-sample-memories` writes
    /// `project_context` rows through `ctx.sea_orm_db` (Phase G), so
    /// two independent `:memory:` connections would leave this
    /// module's own rusqlite-side assertions unable to see them (this
    /// exact bug has bitten every prior PR in this migration). The
    /// tempdir is deliberately leaked via `keep()` rather than
    /// threaded through every call site.
    async fn test_shared_state(forwarding_hmac_key: Option<Vec<u8>>) -> Arc<SharedState> {
        let dir = tempfile::tempdir().unwrap().keep();
        let conn = rusqlite::Connection::open(dir.join("test.db")).unwrap();
        conexus_db::schema::init_schema(&conn).unwrap();
        let sea_orm_db =
            sea_orm::Database::connect(format!("sqlite://{}", dir.join("test.db").display()))
                .await
                .unwrap();
        Arc::new(SharedState {
            conn: tokio::sync::Mutex::new(conn),
            forwarding_hmac_key,
            waiter_registry: conexus_wakeloop::waiter_registry::WaiterRegistry::new(),
            file_map: conexus_wakeloop::file_map::FileMap::new(),
            project_dir: std::env::temp_dir(),
            operator_events: crate::operator_events::OperatorEventsHub::new(),
            delivery_transport: crate::delivery_transport::DeliveryTransportHub::new(),
            sea_orm_db,
        })
    }

    /// Bind a minimal `/status` + `/context-data` + `/create-sample-
    /// memories` app -- three routes spanning both round-28's finding
    /// (`/status` had NO auth dep at all) and the secret-exposure
    /// finding's two other gated endpoints -- behind THIS module's real
    /// `require_rest_identity` layer, on a real UDS. The returned
    /// `TempDir` must be kept alive by the caller for the socket file to
    /// keep existing.
    async fn spawn_test_app(shared: Arc<SharedState>) -> (tempfile::TempDir, std::path::PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let socket_path = dir.path().join("gate-test.sock");
        let app = axum::Router::new()
            .route(
                "/status",
                axum::routing::get(crate::rest_handlers::simple_status),
            )
            .route(
                "/context-data",
                axum::routing::get(crate::rest_handlers::context_data),
            )
            .route(
                "/create-sample-memories",
                axum::routing::post(crate::rest_handlers::create_sample_memories),
            )
            .layer(axum::middleware::from_fn_with_state(
                shared.clone(),
                require_rest_identity,
            ))
            .with_state(shared);
        let path_for_server = socket_path.clone();
        tokio::spawn(async move {
            let _ = crate::uds::serve_router_unix(&path_for_server, app).await;
        });
        for _ in 0..50 {
            if socket_path.exists() {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        (dir, socket_path)
    }

    /// Drive one real HTTP request over the UDS via hyper's client
    /// (same connect-and-handshake shape as `uds.rs`'s own test) and
    /// return just the status code -- every test here only needs that.
    async fn request_status(
        socket_path: &std::path::Path,
        method: &str,
        uri: &str,
        header: Option<(&str, &str)>,
    ) -> u16 {
        let stream = tokio::net::UnixStream::connect(socket_path).await.unwrap();
        let io = hyper_util::rt::TokioIo::new(stream);
        let (mut sender, connection) = hyper::client::conn::http1::handshake(io).await.unwrap();
        tokio::spawn(async move {
            let _ = connection.await;
        });
        let mut builder = hyper::Request::builder().method(method).uri(uri);
        if let Some((name, value)) = header {
            builder = builder.header(name, value);
        }
        let request = builder.body(axum::body::Body::empty()).unwrap();
        let response = sender.send_request(request).await.unwrap();
        response.status().as_u16()
    }

    #[tokio::test]
    async fn unauthenticated_status_request_is_rejected_with_401() {
        let shared = test_shared_state(Some(b"gate-test-key-bytes".to_vec())).await;
        let (_dir, socket_path) = spawn_test_app(shared).await;
        assert_eq!(
            request_status(&socket_path, "GET", "/status", None).await,
            401
        );
    }

    #[tokio::test]
    async fn unauthenticated_context_data_request_is_rejected_with_401() {
        let shared = test_shared_state(Some(b"gate-test-key-bytes".to_vec())).await;
        let (_dir, socket_path) = spawn_test_app(shared).await;
        assert_eq!(
            request_status(&socket_path, "GET", "/context-data", None).await,
            401
        );
    }

    #[tokio::test]
    async fn unauthenticated_create_sample_memories_is_rejected_and_writes_nothing() {
        let shared = test_shared_state(Some(b"gate-test-key-bytes".to_vec())).await;
        let (_dir, socket_path) = spawn_test_app(shared.clone()).await;
        assert_eq!(
            request_status(&socket_path, "POST", "/create-sample-memories", None).await,
            401
        );
        let guard = shared.conn.lock().await;
        let n: i64 = guard
            .query_row(
                "SELECT COUNT(*) FROM project_context WHERE context_key = 'api.config.base_url'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(n, 0, "unauthenticated caller wrote sample memories");
    }

    #[tokio::test]
    async fn signed_forwarding_operator_admits_status_with_200() {
        let key = b"gate-test-key-bytes".to_vec();
        let shared = test_shared_state(Some(key.clone())).await;
        let (_dir, socket_path) = spawn_test_app(shared).await;
        let now = chrono::Utc::now().timestamp() as u64;
        let header = conexus_auth::forwarding_header::sign(
            "op1",
            conexus_auth::forwarding_header::ForwardedRole::Operator,
            &key,
            now,
            30,
        );
        assert_eq!(
            request_status(
                &socket_path,
                "GET",
                "/status",
                Some((conexus_auth::forwarding_header::HEADER_NAME, &header)),
            )
            .await,
            200
        );
    }

    #[tokio::test]
    async fn authenticated_operator_bearer_can_create_sample_memories() {
        let shared = test_shared_state(None).await;
        {
            let guard = shared.conn.lock().await;
            guard
                .execute(
                    "INSERT INTO agents (token, agent_id, created_at, status, \
                     working_directory, agent_role) VALUES \
                     ('mgr-tok', 'manager', '2026-01-01T00:00:00Z', 'active', '/tmp', 'manager')",
                    [],
                )
                .unwrap();
        }
        let (_dir, socket_path) = spawn_test_app(shared.clone()).await;
        assert_eq!(
            request_status(
                &socket_path,
                "POST",
                "/create-sample-memories",
                Some(("Authorization", "Bearer mgr-tok")),
            )
            .await,
            200
        );
        let guard = shared.conn.lock().await;
        let n: i64 = guard
            .query_row(
                "SELECT COUNT(*) FROM project_context WHERE context_key = 'api.config.base_url'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(n, 1);
    }
}
