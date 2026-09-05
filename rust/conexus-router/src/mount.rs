//! External mount / URL-prefix derivation (ADR-0020). Port of
//! `agent_mcp/router/mount.py`.
//!
//! The router's routes live under an INTERNAL namespace (`/agent-mcp`),
//! but the EXTERNAL mount prefix is owned by the reverse proxy and
//! varies per front door: the tailnet serves the router under
//! `/agent-mcp/`, while a Traefik proxy may serve it at the host
//! ROOT. The same process handles both concurrently, so the external
//! prefix + origin must be derived PER REQUEST -- a static config can
//! encode only one mount.
//!
//! Three surfaces (same split as Python, see each fn's own doc):
//! [`canonical_path`] (routing/auth-check identity), [`external_prefix`]
//! (redirects/asset-prefix/snippet URLs), [`external_origin`]
//! (`scheme://host` the browser sees).
//!
//! **Trust, threaded explicitly, not read from a live request**:
//! Python's `_trusted()` calls `rate_limit.request_from_trusted_proxy`
//! directly; that module doesn't exist in this workspace yet (Phase E2
//! PR 15). Rather than block this module on that one, every function
//! here takes `is_trusted: bool` (and the raw `X-Forwarded-*` values,
//! already extracted) as EXPLICIT parameters -- matching this
//! migration's own established "explicit input over hidden dependency"
//! convention (`router_conn: Option<&Connection>`, Phase C). The real
//! caller (PR 15+ / app-wiring) computes `is_trusted` from the actual
//! peer-cred/trusted-proxy-list check and passes it in; this module
//! stays a pure function of its inputs either way.
//!
//! `#![allow(dead_code)]`: this is a BINARY crate (unlike this
//! workspace's library crates, `pub` alone doesn't exempt an item
//! from `dead_code`) and this PR (2/24) has no real consumer yet --
//! the session-gate/proxy-core PRs (8-13+) are. Same precedent as
//! `conexus-backend`'s `rest_gate::ResolvedRestPrincipal`/
//! `json_sanitize` module.
#![allow(dead_code)]

/// The app's internal route namespace. Every route + path-check is
/// expressed relative to this; it is decoupled from the external mount.
pub const INTERNAL_MOUNT: &str = "/agent-mcp";

fn arrived_under_mount(path: &str) -> bool {
    path == INTERNAL_MOUNT || path.starts_with(&format!("{INTERNAL_MOUNT}/"))
}

/// Normalise a prefix to `""` or `/seg[/seg...]` (no trailing `/`).
fn norm_prefix(raw: &str) -> String {
    let stripped = raw.trim().trim_matches('/');
    if stripped.is_empty() {
        String::new()
    } else {
        format!("/{stripped}")
    }
}

/// Request path in the internal `/agent-mcp` namespace.
///
/// Tailnet requests already arrive under `/agent-mcp` (unchanged). A
/// root request (proxy stripped the prefix / Traefik mounted at root)
/// is normalised to its `/agent-mcp` form so every existing path check
/// + the auth gate treat it identically to the tailnet twin.
///
/// **SECURITY**: the operator-session gate MUST key off this, never
/// the raw request path -- otherwise a root-aliased route skips the
/// `starts_with("/agent-mcp")` gate and serves unauthenticated.
pub fn canonical_path(path: &str) -> String {
    if arrived_under_mount(path) {
        return path.to_string();
    }
    if path == "/" {
        return format!("{INTERNAL_MOUNT}/");
    }
    format!("{INTERNAL_MOUNT}{path}")
}

/// The URL prefix the client's browser sees (`""` at root).
pub fn external_prefix(path: &str, is_trusted: bool, forwarded_prefix: Option<&str>) -> String {
    if is_trusted {
        if let Some(xfp) = forwarded_prefix {
            return norm_prefix(xfp);
        }
    }
    // No trusted declaration: infer from how the request arrived.
    if arrived_under_mount(path) {
        INTERNAL_MOUNT.to_string()
    } else {
        String::new()
    }
}

/// `scheme://host` the browser sees. Honours `X-Forwarded-Proto`/`-Host`
/// only when `is_trusted`; otherwise the untrusted transport values
/// (which an attacker can't forge past the real proxy).
pub fn external_origin(
    scheme: &str,
    host: &str,
    is_trusted: bool,
    forwarded_proto: Option<&str>,
    forwarded_host: Option<&str>,
) -> String {
    let proto = if is_trusted {
        forwarded_proto.unwrap_or(scheme)
    } else {
        scheme
    };
    let host = if is_trusted {
        forwarded_host.unwrap_or(host)
    } else {
        host
    };
    format!("{proto}://{host}")
}

/// Client-facing path from an internal suffix (the part AFTER the
/// mount). e.g. `/app/foo/` -> `/agent-mcp/app/foo/` on the tailnet,
/// `/app/foo/` at root.
pub fn external_path(
    path: &str,
    is_trusted: bool,
    forwarded_prefix: Option<&str>,
    internal_suffix: &str,
) -> String {
    let suffix = if let Some(stripped) = internal_suffix.strip_prefix('/') {
        format!("/{stripped}")
    } else {
        format!("/{internal_suffix}")
    };
    format!(
        "{}{}",
        external_prefix(path, is_trusted, forwarded_prefix),
        suffix
    )
}

/// Absolute client-facing URL: origin + external prefix + suffix.
#[allow(clippy::too_many_arguments)]
pub fn external_url(
    path: &str,
    scheme: &str,
    host: &str,
    is_trusted: bool,
    forwarded_prefix: Option<&str>,
    forwarded_proto: Option<&str>,
    forwarded_host: Option<&str>,
    internal_suffix: &str,
) -> String {
    format!(
        "{}{}",
        external_origin(scheme, host, is_trusted, forwarded_proto, forwarded_host),
        external_path(path, is_trusted, forwarded_prefix, internal_suffix)
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_path_passes_through_tailnet_requests_unchanged() {
        assert_eq!(canonical_path("/agent-mcp/login"), "/agent-mcp/login");
        assert_eq!(canonical_path("/agent-mcp"), "/agent-mcp");
    }

    #[test]
    fn canonical_path_normalises_a_root_mounted_request() {
        assert_eq!(canonical_path("/"), "/agent-mcp/");
        assert_eq!(canonical_path("/login"), "/agent-mcp/login");
    }

    #[test]
    fn external_prefix_honours_a_trusted_forwarded_prefix() {
        assert_eq!(
            external_prefix("/anything", true, Some("/custom/")),
            "/custom"
        );
        assert_eq!(external_prefix("/anything", true, Some("")), "");
    }

    #[test]
    fn external_prefix_ignores_forwarded_header_when_untrusted() {
        // An untrusted caller can't forge a prefix -- falls back to
        // path inference regardless of what it claims.
        assert_eq!(
            external_prefix("/agent-mcp/login", false, Some("/evil")),
            "/agent-mcp"
        );
        assert_eq!(external_prefix("/login", false, Some("/evil")), "");
    }

    #[test]
    fn external_prefix_infers_from_path_shape_with_no_forwarded_header() {
        assert_eq!(
            external_prefix("/agent-mcp/login", true, None),
            "/agent-mcp"
        );
        assert_eq!(external_prefix("/login", true, None), "");
    }

    #[test]
    fn external_origin_uses_transport_values_when_untrusted() {
        assert_eq!(
            external_origin(
                "http",
                "internal:8080",
                false,
                Some("https"),
                Some("evil.test")
            ),
            "http://internal:8080"
        );
    }

    #[test]
    fn external_origin_honours_forwarded_values_when_trusted() {
        assert_eq!(
            external_origin(
                "http",
                "internal:8080",
                true,
                Some("https"),
                Some("real.test")
            ),
            "https://real.test"
        );
    }

    #[test]
    fn external_url_composes_origin_prefix_and_suffix() {
        let url = external_url(
            "/agent-mcp/app",
            "https",
            "example.test",
            true,
            None,
            None,
            None,
            "/foo/",
        );
        assert_eq!(url, "https://example.test/agent-mcp/foo/");
    }
}
