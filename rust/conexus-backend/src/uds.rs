//! Unix-domain-socket bind + serve loop. Ported from this operator's
//! own proven, shipping pattern in the sibling repo `m365-bridge`
//! (`src/jmap/server.rs::bind_unix_listener`/`serve_router_unix`) --
//! not invented fresh, since that code already fixed a real found bug
//! (a stale-socket removal that never checked whether a LIVE process
//! still owned the path, confirmed live 2026-09-01).
//!
//! `nix/module.nix`'s existing `agent-mcp@<name>.service` unit already
//! owns the socket's `RuntimeDirectory` (decision: `conexus@<name>.
//! service` shares that same path, see the migration plan's Phase D1
//! decisions) -- this module's own parent-dir creation/chmod is
//! therefore normally a no-op against an already-systemd-managed
//! directory, and only matters for a bare `cargo run`/local dev
//! invocation.

use std::os::unix::fs::PermissionsExt;
use std::path::Path;

use anyhow::{Context, Result};
use axum::Router;
use tokio::net::UnixListener;

/// Remove a stale socket file at `path` before a fresh `bind()`, but
/// refuse -- fail loud, no retry -- if a LIVE process still answers
/// there. A successful connect means something is genuinely
/// listening (never touch it); a connection error means it's a stale
/// file from a previous, no-longer-running process (safe to remove).
/// No auto-recovery is deliberate: systemd's own restart/backoff
/// policy handles a transient race; a genuine duplicate instance must
/// never silently coexist or get rebound over.
fn remove_stale_socket_or_fail(path: &Path) -> Result<()> {
    if path.exists() {
        if std::os::unix::net::UnixStream::connect(path).is_ok() {
            anyhow::bail!(
                "socket {} already in use by a live process -- refusing to rebind",
                path.display()
            );
        }
        match std::fs::remove_file(path) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => {
                return Err(anyhow::Error::new(e)
                    .context(format!("remove stale socket {}", path.display())))
            }
        }
    }
    Ok(())
}

/// Bind a Unix-domain socket: create the parent dir `0700`, remove any
/// stale socket, bind, chmod the socket `0600`. The filesystem perms
/// ARE the same-user guard -- only this uid can open the socket, so no
/// per-connection peer-cred check is needed.
pub fn bind_unix_listener(path: &Path) -> Result<UnixListener> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .with_context(|| format!("create socket parent dir {}", parent.display()))?;
        std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o700))
            .with_context(|| format!("chmod 0700 {}", parent.display()))?;
    }
    remove_stale_socket_or_fail(path)?;
    let listener =
        UnixListener::bind(path).with_context(|| format!("bind unix socket {}", path.display()))?;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))
        .with_context(|| format!("chmod 0600 {}", path.display()))?;
    Ok(listener)
}

/// Accept loop: bridge each accepted stream to `app` via hyper's
/// HTTP/1 server (Streamable HTTP's SSE stream doesn't need HTTP/2).
/// Runs until the listener errors (process shutdown / socket removed
/// out from under it).
pub async fn serve_router_unix(socket_path: &Path, app: Router) -> Result<()> {
    let listener = bind_unix_listener(socket_path)?;
    loop {
        let (stream, _peer) = listener.accept().await?;
        let app = app.clone();
        tokio::spawn(async move {
            let svc = hyper_util::service::TowerToHyperService::new(app);
            let io = hyper_util::rt::TokioIo::new(stream);
            if let Err(e) = hyper::server::conn::http1::Builder::new()
                .serve_connection(io, svc)
                .await
            {
                eprintln!("conexus-backend: connection error: {e}");
            }
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn no_file_at_path_is_a_no_op() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("nonexistent.sock");
        assert!(remove_stale_socket_or_fail(&path).is_ok());
        assert!(!path.exists());
    }

    #[test]
    fn a_stale_unconnectable_socket_file_is_removed() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("stale.sock");
        {
            let _listener = std::os::unix::net::UnixListener::bind(&path).unwrap();
        }
        assert!(path.exists());
        assert!(remove_stale_socket_or_fail(&path).is_ok());
        assert!(!path.exists());
    }

    #[test]
    fn a_live_listening_socket_is_never_touched() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("live.sock");
        let listener = std::os::unix::net::UnixListener::bind(&path).unwrap();
        let err = remove_stale_socket_or_fail(&path).expect_err("a live socket must be refused");
        assert!(err.to_string().contains("already in use"));
        assert!(path.exists());
        drop(listener);
    }

    #[tokio::test]
    async fn bind_unix_listener_creates_parent_dir_and_chmods_the_socket_0600() {
        let dir = tempfile::tempdir().unwrap();
        let socket_path = dir.path().join("nested").join("backend.sock");
        let _listener = bind_unix_listener(&socket_path).unwrap();
        assert!(socket_path.exists());
        let mode = std::fs::metadata(&socket_path)
            .unwrap()
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(mode, 0o600);
    }

    #[tokio::test]
    async fn serve_router_unix_answers_a_real_http_request() {
        let dir = tempfile::tempdir().unwrap();
        let socket_path = dir.path().join("backend.sock");
        let app = Router::new().route("/health", axum::routing::get(|| async { "ok" }));
        let path_for_server = socket_path.clone();
        tokio::spawn(async move {
            let _ = serve_router_unix(&path_for_server, app).await;
        });
        // Give the accept loop a moment to bind.
        for _ in 0..50 {
            if socket_path.exists() {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }

        // No off-the-shelf HTTP client speaks Unix sockets without an
        // extra connector crate; drive the request with hyper's client
        // directly over the socket instead, matching the same
        // accept-loop-bridging shape the production server uses.
        let stream = tokio::net::UnixStream::connect(&socket_path).await.unwrap();
        let io = hyper_util::rt::TokioIo::new(stream);
        let (mut sender, connection) = hyper::client::conn::http1::handshake(io).await.unwrap();
        tokio::spawn(async move {
            let _ = connection.await;
        });
        let request = hyper::Request::builder()
            .uri("/health")
            .body(axum::body::Body::empty())
            .unwrap();
        let response = sender.send_request(request).await.unwrap();
        assert_eq!(response.status(), 200);
    }
}
