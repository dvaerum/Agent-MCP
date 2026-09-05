//! UDS-dialing HTTP client -- a genuinely new primitive (Phase E2
//! PR 7): the missing half of `conexus-backend::uds.rs`, which only
//! implements the SERVER side of a Unix socket. Nothing in this
//! workspace dials one as a client in PRODUCTION code yet, though the
//! exact hyper-over-`UnixStream` pattern already exists as a
//! test-only tool in `conexus-backend::uds.rs`'s own dev-dependencies
//! (used to drive its accept loop end-to-end); this promotes that
//! proven pattern into a real, reusable client. No off-the-shelf HTTP
//! client (`reqwest` included) speaks Unix sockets without an extra
//! connector crate, so hyper's own low-level client API, dialing a
//! real `tokio::net::UnixStream` directly, is the natural choice --
//! matching the server side's own low-level `hyper::server::conn`
//! usage rather than introducing a second HTTP stack.
//!
//! **One connection per call, no pooling** -- matches Python's own
//! `_proxy_to_backend` (`agent_mcp/router/app.py`), which constructs a
//! fresh `UnixConnector` + `ClientSession` PER PROXIED REQUEST (scoped
//! inside the function, not a process-wide pool), not a simplification
//! introduced here.
//!
//! **The request body type is `Full<Bytes>` deliberately, not a
//! streaming body** -- this is what makes the R7-F3 body-buffer
//! invariant (PR8's own subject: the full request body must be read
//! BEFORE the forwarding-header resolver runs, so a slow-drip caller
//! can't hold a demotion-window open across the proxy hop) a
//! STRUCTURAL guarantee at this client's own call site, not just
//! something PR8's implementation has to remember: nothing can call
//! [`send`] with an unbuffered/streaming request body in the first
//! place -- the type simply won't accept one. The RESPONSE body
//! ([`hyper::body::Incoming`]) stays a genuine stream on purpose: an
//! SSE endpoint's response must be forwarded chunk-by-chunk, never
//! buffered whole (Python's own R8-F2 comment on `GET /mcp`'s infinite
//! `text/event-stream` response is explicit that buffering it would
//! simply never return).

// No caller yet -- PR8 (`proxy-core`) is the first real consumer,
// same helpers-ahead-of-their-first-consumer precedent as every other
// not-yet-wired module in this crate.
#![allow(dead_code)]

use std::path::Path;

use bytes::Bytes;
use http_body_util::Full;
use hyper::body::Incoming;
use hyper::{Request, Response};
use hyper_util::rt::TokioIo;
use tokio::net::UnixStream;

/// A pre-buffered request body -- see the module doc for why this is
/// the ONLY body type [`send`] accepts.
pub type UdsRequestBody = Full<Bytes>;

#[derive(Debug)]
pub enum UdsClientError {
    Connect(std::io::Error),
    Handshake(hyper::Error),
    Send(hyper::Error),
}

impl std::fmt::Display for UdsClientError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            UdsClientError::Connect(e) => write!(f, "failed to connect to backend socket: {e}"),
            UdsClientError::Handshake(e) => write!(f, "HTTP handshake with backend failed: {e}"),
            UdsClientError::Send(e) => write!(f, "sending request to backend failed: {e}"),
        }
    }
}

impl std::error::Error for UdsClientError {}

/// Dial `sock_path` fresh and send ONE HTTP/1.1 request over it,
/// returning the response with its body left as a live stream
/// (`hyper::body::Incoming`) for the caller to forward chunk-by-chunk
/// or buffer, whichever the situation calls for -- this function makes
/// no streaming-vs-buffering decision on the RESPONSE side; that's the
/// caller's job (PR8's `proxy-core`).
///
/// Spawns a background task to drive the connection's own I/O loop
/// (hyper's client API splits "send a request" from "drive the
/// connection" into two separate futures deliberately, so the caller
/// can pipeline several requests over one handshake -- not used here,
/// since every call opens a fresh connection, but the split is
/// unavoidable at this API layer either way).
pub async fn send(
    sock_path: &Path,
    request: Request<UdsRequestBody>,
) -> Result<Response<Incoming>, UdsClientError> {
    let stream = UnixStream::connect(sock_path)
        .await
        .map_err(UdsClientError::Connect)?;
    let io = TokioIo::new(stream);
    let (mut sender, connection) = hyper::client::conn::http1::handshake(io)
        .await
        .map_err(UdsClientError::Handshake)?;
    tokio::spawn(async move {
        // Best-effort: a connection-drive error here means the peer
        // closed or the socket died -- already surfaced to the actual
        // caller via `send_request`'s own error below when it applies;
        // this task exists only to poll the connection to completion.
        if let Err(e) = connection.await {
            eprintln!("conexus-router: uds proxy connection error: {e}");
        }
    });
    sender
        .send_request(request)
        .await
        .map_err(UdsClientError::Send)
}

#[cfg(test)]
mod tests {
    use super::*;
    use http_body_util::BodyExt;

    /// A minimal real HTTP/1 server bound to a Unix socket, driven by
    /// the exact same low-level hyper server API `conexus-backend::
    /// uds.rs` uses in production -- proves `send()` against a genuine
    /// peer, not a mock.
    async fn spawn_echo_server(sock_path: std::path::PathBuf) {
        let listener = tokio::net::UnixListener::bind(&sock_path).unwrap();
        tokio::spawn(async move {
            loop {
                let Ok((stream, _)) = listener.accept().await else {
                    return;
                };
                tokio::spawn(async move {
                    let io = TokioIo::new(stream);
                    let svc = hyper::service::service_fn(|req: Request<Incoming>| async move {
                        let method = req.method().to_string();
                        let uri = req.uri().to_string();
                        let body_bytes = req.into_body().collect().await.unwrap().to_bytes();
                        let body =
                            format!("{method} {uri} {}", String::from_utf8_lossy(&body_bytes));
                        Ok::<_, std::convert::Infallible>(Response::new(Full::new(Bytes::from(
                            body,
                        ))))
                    });
                    let _ = hyper::server::conn::http1::Builder::new()
                        .serve_connection(io, svc)
                        .await;
                });
            }
        });
        // Give the accept loop a moment to actually be listening.
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }

    #[tokio::test]
    async fn send_round_trips_a_real_request_over_a_real_unix_socket() {
        let dir = tempfile::tempdir().unwrap();
        let sock_path = dir.path().join("backend.sock");
        spawn_echo_server(sock_path.clone()).await;

        let request = Request::builder()
            .method("POST")
            .uri("/mcp")
            .body(Full::new(Bytes::from_static(b"hello")))
            .unwrap();
        let response = send(&sock_path, request).await.unwrap();
        assert_eq!(response.status(), 200);
        let body = response.into_body().collect().await.unwrap().to_bytes();
        assert_eq!(body.as_ref(), b"POST /mcp hello");
    }

    #[tokio::test]
    async fn send_reports_a_connect_error_for_a_missing_socket() {
        let dir = tempfile::tempdir().unwrap();
        let sock_path = dir.path().join("nonexistent.sock");
        let request = Request::builder()
            .uri("/mcp")
            .body(Full::new(Bytes::new()))
            .unwrap();
        let err = send(&sock_path, request).await.unwrap_err();
        assert!(matches!(err, UdsClientError::Connect(_)));
    }

    #[tokio::test]
    async fn send_opens_a_fresh_connection_per_call() {
        // Two sequential calls against the same server must both
        // succeed independently -- proving no connection state (or a
        // stale, already-consumed sender) leaks across calls the way
        // a pooling client's reused-connection bugs might.
        let dir = tempfile::tempdir().unwrap();
        let sock_path = dir.path().join("backend.sock");
        spawn_echo_server(sock_path.clone()).await;

        for i in 0..2 {
            let request = Request::builder()
                .uri(format!("/call-{i}"))
                .body(Full::new(Bytes::new()))
                .unwrap();
            let response = send(&sock_path, request).await.unwrap();
            let body = response.into_body().collect().await.unwrap().to_bytes();
            assert!(String::from_utf8_lossy(&body).contains(&format!("/call-{i}")));
        }
    }
}
