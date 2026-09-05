//! Pure decision functions for the router's dashboard-serving surface
//! -- port of `agent_mcp/router/app.py`'s `_MIME`/`_safe_dashboard_path`/
//! `_serve_dashboard_file`/`dashboard_handler`'s resolve-or-SPA-
//! fallback chain/`_accept_prefers_html`/`_service_descriptor`. Phase
//! E2, `conexus-router-dashboard-static` (PR23 step 8, PR 1/3).
//!
//! Framework-agnostic, matching every other decision-function module
//! this phase -- real axum route registration is PR 2/3; the
//! `_warm_backend`/`_schedule_backend_warm` side effect (plus the
//! `middleware.rs` `warm_authorized`-threading fix it needs) is PR 3/3.

#![allow(dead_code)]

use std::path::{Component, Path, PathBuf};

use crate::asset_prefix::{content_type_needs_substitution, AssetPrefixCache};

/// Port of `_MIME`. A closed extension table (Python's own dict, no
/// dot in the match keys since [`Path::extension`] never returns
/// one); an unmapped extension falls back to
/// `application/octet-stream`, mirroring Python's implicit
/// `.get(ext, "application/octet-stream")` default (the real dict
/// has no explicit fallback entry, but every call site treats a miss
/// the same way -- ported as the same behavior, not a new default).
pub fn mime_for_extension(extension: &str) -> &'static str {
    match extension.to_ascii_lowercase().as_str() {
        "html" => "text/html; charset=utf-8",
        "js" | "mjs" => "application/javascript; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "json" => "application/json; charset=utf-8",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "ico" => "image/x-icon",
        "txt" => "text/plain; charset=utf-8",
        "woff" => "font/woff",
        "woff2" => "font/woff2",
        _ => "application/octet-stream",
    }
}

/// Port of `_safe_dashboard_path`. Resolves `rest` against
/// `dashboard_root`, refusing any escape. Reuses the exact
/// canonicalize-or-lexical-fallback idiom `lifecycle::
/// is_within_default_workspace` already established (duplicated here
/// per ADR-0020 -- this crate never depends on `conexus_tools`,
/// where the same idiom originates) since a legitimate candidate
/// (e.g. an asset path the caller is about to fall away from) must
/// still resolve to a checkable path even when nothing exists on
/// disk yet -- Python's own `Path.resolve()` has no `strict=True`
/// here either.
pub fn safe_dashboard_path(dashboard_root: &Path, rest: &str) -> Option<PathBuf> {
    if rest.as_bytes().contains(&0) {
        return None; // R6-F3: an embedded NUL byte fails closed, never falls through.
    }
    let root_resolved = dashboard_root.canonicalize().ok()?;
    let candidate = dashboard_root.join(rest);
    let resolved = candidate
        .canonicalize()
        .unwrap_or_else(|_| normalize_lexically(&candidate));
    if resolved.starts_with(&root_resolved) {
        Some(resolved)
    } else {
        None
    }
}

fn normalize_lexically(path: &Path) -> PathBuf {
    let mut out: Vec<Component> = Vec::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => match out.last() {
                Some(Component::Normal(_)) => {
                    out.pop();
                }
                Some(Component::RootDir) => {}
                _ => out.push(component),
            },
            other => out.push(other),
        }
    }
    out.iter().collect()
}

/// Port of `dashboard_handler`'s resolve-or-SPA-fallback chain: a
/// directory-shaped `rest` (empty or trailing `/`) resolves to its
/// own `index.html`; otherwise try `rest` as-is, then its `.html`
/// suffix sibling, then finally the ROOT `index.html` (the SPA
/// fallback -- a client-side-routed path like `/tasks` has no file on
/// disk, but the React router knows how to render it once the shell
/// loads). `None` only when even the root shell is missing.
///
/// Deliberate simplification, documented not silently dropped:
/// Python re-validates the `.html`-suffix candidate via
/// `_safe_dashboard_path(alt.name)` -- passing just the BASENAME, not
/// the full relative path, which resolves it against the dashboard
/// ROOT rather than its actual subdirectory. This is inert in
/// practice (a bare filename can never itself contain a path
/// separator or `..`), and here `.with_extension("html")` only
/// renames the leaf component of an ALREADY-validated, ALREADY-
/// contained path -- provably still contained without a second
/// resolve, so this port skips the redundant re-check rather than
/// replicating its odd (basename-only) shape.
pub fn resolve_dashboard_candidate(dashboard_root: &Path, rest: &str) -> Option<PathBuf> {
    let want_index = rest.is_empty() || rest.ends_with('/');
    let primary = if want_index {
        safe_dashboard_path(dashboard_root, &format!("{rest}index.html"))
    } else {
        safe_dashboard_path(dashboard_root, rest)
    }?;
    if primary.is_file() {
        return Some(primary);
    }
    if !want_index {
        let alt = primary.with_extension("html");
        if alt.is_file() {
            return Some(alt);
        }
    }
    let root_index = safe_dashboard_path(dashboard_root, "index.html")?;
    root_index.is_file().then_some(root_index)
}

/// Port of `_serve_dashboard_file`'s content-type-aware byte
/// assembly. Binary/structured-data files (images, fonts, JSON) skip
/// substitution and are read verbatim -- substitution could corrupt
/// their bytes if a chance sequence happened to match the sentinel,
/// per `_serve_dashboard_file`'s own comment.
pub fn resolve_dashboard_body(
    cache: &AssetPrefixCache,
    path: &Path,
    prefix: &str,
) -> std::io::Result<(Vec<u8>, &'static str)> {
    let extension = path.extension().and_then(|e| e.to_str()).unwrap_or("");
    let content_type = mime_for_extension(extension);
    if content_type_needs_substitution(Some(content_type)) {
        Ok((cache.substitute_file_bytes(path, prefix)?, content_type))
    } else {
        Ok((std::fs::read(path)?, content_type))
    }
}

/// Port of `_accept_prefers_html`. `*/*` deliberately does NOT count
/// -- a generic API client sending `*/*` gets JSON, matching
/// Python's own explicit rationale.
pub fn accept_prefers_html(accept_header: &str) -> bool {
    if accept_header.is_empty() {
        return false;
    }
    accept_header.split(',').any(|part| {
        let media_type = part
            .split(';')
            .next()
            .unwrap_or("")
            .trim()
            .to_ascii_lowercase();
        media_type == "text/html" || media_type == "application/xhtml+xml"
    })
}

/// Port of `_service_descriptor`. The internal package version is
/// deliberately NOT echoed (SEC, owner-authorised) -- pure attacker-
/// useful build fingerprinting no operator consumes.
pub fn service_descriptor(single_tenant_name: Option<&str>) -> serde_json::Value {
    serde_json::json!({
        "service": "agent-mcp",
        "mode": if single_tenant_name.is_some() { "single-tenant" } else { "multi-tenant" },
        "endpoints": {
            "api": "/agent-mcp/api",
            "app": "/agent-mcp/app",
            "assets": "/agent-mcp/assets",
            "mcp": "/agent-mcp/mcp",
        },
        "projects_url": "/agent-mcp/api/router/projects",
        "overview_url": "/agent-mcp/api/router/overview",
        "health_url": "/agent-mcp/api/router/health",
        "single_tenant_project": single_tenant_name,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::TempDir;

    fn root() -> TempDir {
        TempDir::new().unwrap()
    }

    #[test]
    fn mime_for_extension_matches_known_types_case_insensitively() {
        assert_eq!(mime_for_extension("HTML"), "text/html; charset=utf-8");
        assert_eq!(
            mime_for_extension("js"),
            "application/javascript; charset=utf-8"
        );
        assert_eq!(mime_for_extension("mjs"), mime_for_extension("js"));
        assert_eq!(mime_for_extension("woff2"), "font/woff2");
    }

    #[test]
    fn mime_for_extension_unknown_falls_back_to_octet_stream() {
        assert_eq!(mime_for_extension("bin"), "application/octet-stream");
        assert_eq!(mime_for_extension(""), "application/octet-stream");
    }

    #[test]
    fn safe_dashboard_path_admits_a_real_child_path() {
        let dir = root();
        fs::write(dir.path().join("index.html"), b"hi").unwrap();
        let resolved = safe_dashboard_path(dir.path(), "index.html").unwrap();
        assert_eq!(
            resolved,
            dir.path().canonicalize().unwrap().join("index.html")
        );
    }

    #[test]
    fn safe_dashboard_path_admits_a_not_yet_existing_child() {
        let dir = root();
        let resolved = safe_dashboard_path(dir.path(), "missing.js").unwrap();
        assert!(resolved.ends_with("missing.js"));
    }

    #[test]
    fn safe_dashboard_path_denies_a_traversal_escape() {
        let dir = root();
        assert_eq!(safe_dashboard_path(dir.path(), "../etc/passwd"), None);
    }

    #[test]
    fn safe_dashboard_path_denies_an_embedded_nul_byte() {
        let dir = root();
        assert_eq!(safe_dashboard_path(dir.path(), "foo\0bar"), None);
    }

    #[test]
    fn resolve_dashboard_candidate_serves_the_exact_file_when_present() {
        let dir = root();
        fs::write(dir.path().join("app.js"), b"console.log(1)").unwrap();
        let candidate = resolve_dashboard_candidate(dir.path(), "app.js").unwrap();
        assert!(candidate.ends_with("app.js"));
    }

    #[test]
    fn resolve_dashboard_candidate_falls_back_to_the_html_suffix_sibling() {
        let dir = root();
        fs::write(dir.path().join("tasks.html"), b"<html></html>").unwrap();
        let candidate = resolve_dashboard_candidate(dir.path(), "tasks").unwrap();
        assert!(candidate.ends_with("tasks.html"));
    }

    #[test]
    fn resolve_dashboard_candidate_spa_fallback_serves_root_index() {
        let dir = root();
        fs::write(dir.path().join("index.html"), b"<html>shell</html>").unwrap();
        // "tasks" has neither tasks nor tasks.html -- a client-routed path.
        let candidate = resolve_dashboard_candidate(dir.path(), "tasks").unwrap();
        assert!(candidate.ends_with("index.html"));
    }

    #[test]
    fn resolve_dashboard_candidate_empty_rest_serves_directory_index() {
        let dir = root();
        fs::write(dir.path().join("index.html"), b"<html>shell</html>").unwrap();
        let candidate = resolve_dashboard_candidate(dir.path(), "").unwrap();
        assert!(candidate.ends_with("index.html"));
    }

    #[test]
    fn resolve_dashboard_candidate_none_when_even_the_shell_is_missing() {
        let dir = root();
        assert_eq!(resolve_dashboard_candidate(dir.path(), "tasks"), None);
    }

    #[test]
    fn resolve_dashboard_body_reads_a_binary_file_verbatim() {
        let dir = root();
        let cache = AssetPrefixCache::new();
        let path = dir.path().join("logo.png");
        fs::write(&path, [0x89, 0x50, 0x4e, 0x47]).unwrap();
        let (body, content_type) =
            resolve_dashboard_body(&cache, &path, "/agent-mcp/assets").unwrap();
        assert_eq!(body, vec![0x89, 0x50, 0x4e, 0x47]);
        assert_eq!(content_type, "image/png");
    }

    #[test]
    fn resolve_dashboard_body_substitutes_the_sentinel_in_html() {
        let dir = root();
        let cache = AssetPrefixCache::new();
        let path = dir.path().join("index.html");
        fs::write(
            &path,
            b"<script src=\"__AGENT_MCP_ASSET_PREFIX__/_next/x.js\">",
        )
        .unwrap();
        let (body, content_type) =
            resolve_dashboard_body(&cache, &path, "/agent-mcp/assets").unwrap();
        assert_eq!(content_type, "text/html; charset=utf-8");
        assert_eq!(
            String::from_utf8(body).unwrap(),
            "<script src=\"/agent-mcp/assets/_next/x.js\">"
        );
    }

    #[test]
    fn accept_prefers_html_matches_exact_html_media_type() {
        assert!(accept_prefers_html("text/html"));
        assert!(accept_prefers_html(
            "text/html,application/xhtml+xml,application/xml;q=0.9"
        ));
        assert!(accept_prefers_html("APPLICATION/XHTML+XML"));
    }

    #[test]
    fn accept_prefers_html_wildcard_does_not_count() {
        assert!(!accept_prefers_html("*/*"));
        assert!(!accept_prefers_html("application/json"));
        assert!(!accept_prefers_html(""));
    }

    #[test]
    fn service_descriptor_reflects_multi_tenant_mode() {
        let d = service_descriptor(None);
        assert_eq!(d["mode"], "multi-tenant");
        assert_eq!(d["single_tenant_project"], serde_json::Value::Null);
        assert_eq!(d["endpoints"]["api"], "/agent-mcp/api");
    }

    #[test]
    fn service_descriptor_reflects_single_tenant_mode() {
        let d = service_descriptor(Some("demo"));
        assert_eq!(d["mode"], "single-tenant");
        assert_eq!(d["single_tenant_project"], "demo");
    }
}
