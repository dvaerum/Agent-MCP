//! Security response headers for every router response. Port of
//! `agent_mcp/router/security_headers.py` (302 LOC, Phase E2 PR 15,
//! `conexus-router-headers-misc`).
//!
//! Framework-agnostic: [`security_headers`] enumerates the intended
//! header set as plain data ([`SecurityHeader`], with an explicit
//! `overwrite` flag distinguishing the one header -- `Server` -- that
//! must win even over a handler's own value from every other header,
//! which only fills in when the handler didn't already set one). PR
//! 23's real axum middleware applies this list to an actual response
//! header map (via `Entry::or_insert` for `overwrite: false`, a plain
//! assignment for `overwrite: true`) and additionally owns the
//! exception-classification wrapper (`security_headers_middleware`'s
//! own `HTTPException`/`ConnectionError`/generic-`Exception` catch
//! ladder) -- that ladder is inherently a `tower`/axum error-handling
//! concern with no framework-agnostic equivalent to port ahead of a
//! real handler stack.
//!
//! **Re-derived, not ported**: Python's `SERVER_SOFTWARE` monkeypatch
//! (neutralising aiohttp's own version-disclosing `Server:
//! Python/... aiohttp/...` default, including the parser-rejected-
//! request path that bypasses the whole middleware chain) has no
//! `hyper`/axum equivalent to patch -- neither emits a version-
//! disclosing `Server` header by default in the first place, so the
//! vulnerability class this patch closes doesn't exist on this stack.
//! `SERVER_BANNER`/the `overwrite: true` `Server` entry are kept
//! anyway (a neutral, deliberate banner is still better than
//! whatever a fronting proxy might otherwise pass through unmodified).
#![allow(dead_code)]

/// Pragmatic-but-protective default CSP. `script-src`/`style-src`
/// allow `'unsafe-inline'` because the dashboard is a static Next.js
/// export (inline hydration `<script>`s, an inline login/setup
/// `<style>` block) that can't use per-response nonces; every other
/// directive stays strict (`frame-ancestors 'none'`, `object-src
/// 'none'`, `base-uri 'self'`, `default-src 'self'`).
pub const DEFAULT_CSP: &str = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'";

/// The dashboard needs none of these powerful features; deny them
/// outright.
pub const DEFAULT_PERMISSIONS_POLICY: &str =
    "geolocation=(), microphone=(), camera=(), payment=(), usb=()";

pub const HSTS_VALUE: &str = "max-age=63072000; includeSubDomains";

/// Neutral, version-free `Server` banner.
pub const SERVER_BANNER: &str = "agent-mcp";

/// Port of `_csp`: `AGENT_MCP_CSP` overrides the default when set to
/// a non-empty value (an empty override is treated as unset, matching
/// Python's `if override else _DEFAULT_CSP` falsy check).
pub fn csp(env_override: Option<&str>) -> String {
    match env_override {
        Some(v) if !v.is_empty() => v.to_string(),
        _ => DEFAULT_CSP.to_string(),
    }
}

/// Port of `_request_is_https` (the same heuristic
/// `login::cookie_secure_flag` uses): `X-Forwarded-Proto` is honoured
/// ONLY from a trusted proxy, and only for its two recognised values
/// (`"https"`/`"http"`, case-insensitive) -- anything else (absent,
/// untrusted, or garbage) falls through to the real transport scheme.
/// The exploitable direction of an ungated header here is HSTS-
/// *suppression* (spoofing `http` strips HSTS from an otherwise-secure
/// response); spoofing `https` has no client impact since RFC 6797
/// requires an already-validated TLS connection for a browser to
/// honour the header at all.
pub fn request_is_https(
    is_trusted_proxy: bool,
    forwarded_proto: Option<&str>,
    url_scheme: &str,
) -> bool {
    if is_trusted_proxy {
        if let Some(v) = forwarded_proto {
            let lower = v.to_ascii_lowercase();
            if lower == "https" {
                return true;
            }
            if lower == "http" {
                return false;
            }
        }
    }
    url_scheme.eq_ignore_ascii_case("https")
}

/// One security header this module wants applied. `overwrite: true`
/// means "set unconditionally, even over a handler's own value" (only
/// `Server`, per Python's own `hdrs["Server"] = ...` vs. every other
/// header's `hdrs.setdefault(...)`); `overwrite: false` means "fill in
/// only if the response doesn't already carry this header".
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SecurityHeader {
    pub name: &'static str,
    pub value: String,
    pub overwrite: bool,
}

fn setdefault(name: &'static str, value: impl Into<String>) -> SecurityHeader {
    SecurityHeader {
        name,
        value: value.into(),
        overwrite: false,
    }
}

/// Port of `_apply_headers`'s full intended header set -- see this
/// module's own doc for why applying it to a real response is PR 23's
/// job.
pub fn security_headers(is_https: bool, csp_value: &str) -> Vec<SecurityHeader> {
    let mut headers = vec![
        setdefault("X-Content-Type-Options", "nosniff"),
        setdefault("X-Frame-Options", "DENY"),
        setdefault("Referrer-Policy", "strict-origin-when-cross-origin"),
        setdefault("Content-Security-Policy", csp_value),
        setdefault("Permissions-Policy", DEFAULT_PERMISSIONS_POLICY),
        // Cross-origin isolation, defense-in-depth alongside
        // frame-ancestors/X-Frame-Options: COOP severs cross-origin
        // window handles; CORP refuses cross-origin embedding of the
        // router's own responses. The dashboard is fully same-origin,
        // so neither breaks a legitimate load.
        setdefault("Cross-Origin-Opener-Policy", "same-origin"),
        setdefault("Cross-Origin-Resource-Policy", "same-origin"),
    ];
    if is_https {
        // HSTS only over HTTPS -- never pin a plain-HTTP dev/VM to TLS.
        headers.push(setdefault("Strict-Transport-Security", HSTS_VALUE));
    }
    // SC-1: opt sensitive surfaces out of caching. `setdefault`
    // semantics so the static dashboard handlers keep their own
    // explicit value (`no-store` for HTML, `immutable` for
    // hash-named assets), set BEFORE this would apply.
    headers.push(setdefault("Cache-Control", "no-store"));
    headers.push(SecurityHeader {
        name: "Server",
        value: SERVER_BANNER.to_string(),
        overwrite: true,
    });
    headers
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn csp_falls_back_to_the_default_with_no_override() {
        assert_eq!(csp(None), DEFAULT_CSP);
    }

    #[test]
    fn csp_treats_an_empty_override_as_unset() {
        assert_eq!(csp(Some("")), DEFAULT_CSP);
    }

    #[test]
    fn csp_honours_a_non_empty_override() {
        assert_eq!(csp(Some("default-src 'none'")), "default-src 'none'");
    }

    #[test]
    fn request_is_https_honours_a_trusted_forwarded_proto() {
        assert!(request_is_https(true, Some("https"), "http"));
        assert!(!request_is_https(true, Some("http"), "https"));
    }

    #[test]
    fn request_is_https_ignores_an_untrusted_forwarded_proto() {
        assert!(!request_is_https(false, Some("https"), "http"));
    }

    #[test]
    fn request_is_https_falls_through_on_a_garbage_forwarded_value() {
        assert!(!request_is_https(true, Some("carrier-pigeon"), "http"));
        assert!(request_is_https(true, Some("carrier-pigeon"), "https"));
    }

    #[test]
    fn request_is_https_falls_back_to_the_real_scheme_with_no_header() {
        assert!(request_is_https(true, None, "https"));
        assert!(!request_is_https(true, None, "http"));
    }

    #[test]
    fn security_headers_includes_hsts_only_over_https() {
        let with_https = security_headers(true, DEFAULT_CSP);
        assert!(with_https
            .iter()
            .any(|h| h.name == "Strict-Transport-Security"));
        let without_https = security_headers(false, DEFAULT_CSP);
        assert!(!without_https
            .iter()
            .any(|h| h.name == "Strict-Transport-Security"));
    }

    #[test]
    fn security_headers_marks_only_server_as_overwrite() {
        let headers = security_headers(true, DEFAULT_CSP);
        for h in &headers {
            assert_eq!(
                h.overwrite,
                h.name == "Server",
                "{} had unexpected overwrite flag",
                h.name
            );
        }
    }

    #[test]
    fn security_headers_carries_the_given_csp_value_verbatim() {
        let headers = security_headers(false, "default-src 'none'");
        let csp_header = headers
            .iter()
            .find(|h| h.name == "Content-Security-Policy")
            .unwrap();
        assert_eq!(csp_header.value, "default-src 'none'");
    }

    /// R7-F2: aiohttp's protocol-parser error path leaked the raw
    /// `Server: Python/x.y aiohttp/z` banner for a request malformed
    /// enough to trip the parser BEFORE `web.Application`'s own
    /// middleware chain ever saw it -- there was nowhere left to patch
    /// except the framework's own `SERVER_SOFTWARE` module constant (see
    /// this module's own doc). This module's doc claims that vulnerability
    /// class doesn't exist on hyper/axum (neither emits a version-
    /// disclosing `Server` header by default in the first place); this
    /// test proves that claim empirically against a REAL bound listener
    /// (a well-formed `aiohttp_client`/reqwest-style client can't
    /// construct a genuinely malformed request -- same reasoning as the
    /// Python test, hence the raw socket) rather than leaving it as an
    /// unverified doc comment, and pins it against a future hyper/axum
    /// version silently changing that default.
    mod raw_socket_r7_f2 {
        use axum::routing::get;
        use axum::Router;
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        async fn handler() -> &'static str {
            "ok"
        }

        #[tokio::test]
        async fn malformed_content_length_does_not_leak_a_version_disclosing_server_banner() {
            // Deliberately a BARE router -- no `security_headers_layer`
            // (or any middleware) wired in. A malformed `Content-Length`
            // trips hyper's own HTTP/1 parser inside `axum::serve` before
            // the request is ever dispatched to the `Router`, so a
            // passing assertion here can only be explained by hyper's own
            // default behavior, not by this crate's per-response header
            // stamping (already covered by `middleware.rs`'s own
            // full-stack tests).
            let app: Router = Router::new().route("/", get(handler));
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let addr = listener.local_addr().unwrap();
            let server = tokio::spawn(async move {
                let _ = axum::serve(listener, app.into_make_service()).await;
            });

            let mut stream = tokio::net::TcpStream::connect(addr).await.unwrap();
            let raw_request = b"GET / HTTP/1.1\r\n\
                Host: localhost\r\n\
                Content-Length: abc\r\n\
                Connection: close\r\n\
                \r\n";
            stream.write_all(raw_request).await.unwrap();
            let mut buf = Vec::new();
            // Bounded: a genuine regression that hangs the connection
            // must fail the test, not stall the whole suite.
            let _ = tokio::time::timeout(
                std::time::Duration::from_secs(5),
                stream.read_to_end(&mut buf),
            )
            .await;
            server.abort();

            let response = String::from_utf8_lossy(&buf);
            let head = response.split("\r\n\r\n").next().unwrap_or("");
            let status_line = head.lines().next().unwrap_or("");
            assert!(
                status_line.contains("400"),
                "expected a 400 for the malformed Content-Length, got: {status_line:?} \
                 (full head: {head:?})"
            );
            let server_header = head
                .lines()
                .find(|l| l.to_ascii_lowercase().starts_with("server:"))
                .unwrap_or("")
                .to_ascii_lowercase();
            assert!(
                !server_header.contains("hyper") && !server_header.contains("axum"),
                "leaked a version-disclosing Server header: {server_header:?}"
            );
        }
    }
}
