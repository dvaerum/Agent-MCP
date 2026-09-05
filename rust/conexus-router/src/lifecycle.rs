//! Pure decision/validation primitives for the router's project-
//! lifecycle REST surface. Port of the non-handler-body half of
//! `agent_mcp/router/admin_api.py` (1792 LOC) + the lifecycle-specific
//! pieces of `agent_mcp/router/app.py` it calls into (`_error_envelope`/
//! `_success_envelope`, `_validate_name`/`_SLUG_RE`/`_RESERVED_NAMES`/
//! `_NAME_MAX`, `_workspace_label`, `_is_within_default_workspace`).
//! Phase E2 PR 16, `conexus-router-lifecycle-foundations` -- the
//! smallest, most-contained slice of the lifecycle-REST research's own
//! proposed breakdown.
//!
//! Framework-agnostic, matching every prior handler-layer module: real
//! axum route registration is PR 23's job. The genuine `async`/lock/
//! systemctl-yield-point revalidation wrappers (`perm_gates.py`'s
//! `revalidated_lock`/`revalidate_after`/`read_body_and_revalidate`)
//! are deliberately NOT built here either -- see this migration's own
//! DEFERRED entry for `perm_gates.py` in the plan file: designing a
//! generic async fusion wrapper ahead of axum's real extractor/yield
//! shape would be guessing at yield points that don't exist yet. The
//! *synchronous* half of that pattern (a fresh DB-backed capability +
//! membership re-check, which needs no lock/systemctl/await at all --
//! `session_gate.rs::evaluate_session_gate` already proves this) is
//! PR 17's job, once this PR's envelope/validation vocabulary exists
//! for it to return through.
#![allow(dead_code)]

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use regex::Regex;

use crate::mcp_handler::{HandlerBody, HandlerResponse};

/// Slug regex for a project name -- single-letter names allowed
/// (`^[a-z]$`); longer names start with a letter and end with an
/// alphanumeric, hyphens permitted in the middle. `_` is deliberately
/// EXCLUDED from the character class -- that's how the router's own
/// `__operation`-shaped namespace stays structurally protected from a
/// project-name collision.
pub static SLUG_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-z](?:[a-z0-9-]*[a-z0-9])?$").unwrap());

pub const NAME_MAX: usize = 64;

/// Segments that collide with the router's own top-level path
/// namespace (`/agent-mcp/api/`, `/agent-mcp/app/`, `/agent-mcp/assets/`,
/// `/agent-mcp/mcp/`) -- ADR-0014 adds `router`, the single admin-
/// namespace segment under `/api/router/...`. Rejected at create/
/// rename time so the registry never holds a name that would collide.
pub const RESERVED_NAMES: &[&str] = &["api", "app", "assets", "mcp", "router"];

/// Port of `_validate_name`: `None` if valid, else a human-readable
/// error message (never echoes anything the caller didn't already
/// supply -- `name` itself is always safe to echo back, it's the
/// value under validation).
pub fn validate_name(name: &str, existing: &HashSet<String>) -> Option<String> {
    if name.is_empty() {
        return Some("name is required".to_string());
    }
    if name.len() > NAME_MAX {
        return Some(format!("name is longer than {NAME_MAX} characters"));
    }
    if !SLUG_RE.is_match(name) {
        return Some(format!(
            "name must match {} — lowercase letters, digits, and hyphens only; \
             first char is a letter, no leading/trailing hyphen, no underscores \
             (single letter ok)",
            SLUG_RE.as_str()
        ));
    }
    if RESERVED_NAMES.contains(&name) {
        let mut sorted: Vec<&&str> = RESERVED_NAMES.iter().collect();
        sorted.sort();
        let joined = sorted
            .iter()
            .map(|s| s.to_string())
            .collect::<Vec<_>>()
            .join(", ");
        return Some(format!(
            "name {name:?} is reserved — it conflicts with the top-level router \
             path /agent-mcp/{name}/. Reserved names: {joined}."
        ));
    }
    if existing.contains(name) {
        return Some(format!("project {name:?} is already registered"));
    }
    None
}

/// Port of `_reject_non_str_name` (PF-R8-1): guards a JSON body's
/// `name` field against a non-string value BEFORE any `.strip()`-
/// shaped handling runs. `None` (the field is absent) is fine --
/// the caller's own "" default + [`validate_name`] still produce the
/// canonical "name is required" message.
pub fn reject_non_str_name(value: Option<&serde_json::Value>) -> Option<HandlerResponse> {
    match value {
        Some(v) if !v.is_string() => Some(error_envelope(
            LifecycleError::InvalidName,
            "name must be a string",
            None,
        )),
        _ => None,
    }
}

/// Port of `_workspace_label`: an operator-facing, non-disclosing
/// label for a project's workspace -- relative to
/// `default_workspace_parent` for the common managed layout, else just
/// the leaf directory name, so the deployment's absolute filesystem
/// layout never leaks into an API response.
pub fn workspace_label(workspace: &str, default_workspace_parent: &Path) -> String {
    let p = PathBuf::from(workspace);
    let leaf = || {
        p.file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default()
    };
    let Ok(canonical) = p.canonicalize() else {
        return leaf();
    };
    let Ok(parent_canonical) = default_workspace_parent.canonicalize() else {
        return leaf();
    };
    match canonical.strip_prefix(&parent_canonical) {
        Ok(rel) => rel.to_string_lossy().into_owned(),
        Err(_) => leaf(),
    }
}

/// Port of `_is_within_default_workspace`: defence in depth for a
/// hard `rm` -- only allowed when the workspace path is rooted inside
/// `default_workspace_parent`, comparing resolved (symlink-followed)
/// paths so traversal can't escape the bound. Fails CLOSED (denies
/// the hard rm) on an embedded-NUL path or an unresolvable parent.
///
/// **Found-and-fixed bug**: an earlier version of this function
/// required the WORKSPACE path itself to exist (`Path::canonicalize`
/// is strict), so `delete_project_handler`'s `?delete_workspace=true`
/// path for an ALREADY-absent-on-disk workspace (a real, documented
/// Python branch -- `workspace_delete_skipped_reason = "workspace did
/// not exist on disk"`) was wrongly rejected as "outside the default
/// workspace parent" instead. Python's own `Path.resolve()` (no
/// `strict=True`) tolerates a nonexistent tail; this now matches by
/// falling back to a pure lexical normalization (dot-segment removal,
/// no filesystem access) when the strict canonicalize fails for a
/// reason OTHER than an embedded NUL byte, reusing the same
/// canonicalize-or-lexical-fallback idiom `conexus_tools::
/// file_metadata_tools::normalize_filepath` already established for
/// the identical Python `Path.resolve()`-without-`strict` semantics.
pub fn is_within_default_workspace(workspace_path: &Path, default_workspace_parent: &Path) -> bool {
    if workspace_path.as_os_str().as_encoded_bytes().contains(&0) {
        return false; // R6-F3: an embedded NUL byte fails closed, never falls through.
    }
    let Ok(parent_resolved) = default_workspace_parent.canonicalize() else {
        return false;
    };
    let workspace_resolved = workspace_path
        .canonicalize()
        .unwrap_or_else(|_| normalize_lexically(workspace_path));
    workspace_resolved.starts_with(&parent_resolved)
}

fn normalize_lexically(path: &Path) -> PathBuf {
    use std::path::Component;
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

/// The closed set of lifecycle-REST error discriminators -- port of
/// `_ERROR_INVALID_NAME`/`_ERROR_ALREADY_REGISTERED`/etc, matching this
/// crate's own `RegistryError`/`EnsureError` "closed enum over string
/// routing" precedent rather than Python's bare string constants.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LifecycleError {
    InvalidName,
    AlreadyRegistered,
    NotRegistered,
    ActiveSessions,
    NameTaken,
    AliasCollision,
    Internal,
    /// Port of `admin_users_api._ERROR_NOT_FOUND` -- the uniform
    /// existence-oracle-closing 404 `_deny_cross_tenant_project_read`
    /// (PR 17) shares with the lifecycle handlers' own not-found shape.
    NotFound,
    /// The generic `"forbidden"` discriminator (rank/membership
    /// denials, and `perm_gates.py`'s capability-gate rejection --
    /// both render the identical string in Python).
    Forbidden,
}

impl LifecycleError {
    pub fn discriminator(self) -> &'static str {
        match self {
            LifecycleError::InvalidName => "invalid_name",
            LifecycleError::AlreadyRegistered => "already_registered",
            LifecycleError::NotRegistered => "not_registered",
            LifecycleError::ActiveSessions => "active_sessions",
            LifecycleError::NameTaken => "name_taken",
            LifecycleError::AliasCollision => "alias_collision",
            LifecycleError::Internal => "internal_error",
            LifecycleError::NotFound => "not_found",
            LifecycleError::Forbidden => "forbidden",
        }
    }

    /// The default HTTP status for this discriminator -- a caller can
    /// still override (Python's own `_error_envelope` takes `status`
    /// explicitly at every call site rather than deriving it from
    /// `error`; kept here as a documented convenience default, not a
    /// hidden mapping call sites can't deviate from).
    pub fn default_status(self) -> u16 {
        match self {
            LifecycleError::InvalidName => 400,
            LifecycleError::AlreadyRegistered => 409,
            LifecycleError::NotRegistered => 404,
            LifecycleError::ActiveSessions => 409,
            LifecycleError::NameTaken => 409,
            LifecycleError::AliasCollision => 409,
            LifecycleError::Internal => 500,
            LifecycleError::NotFound => 404,
            LifecycleError::Forbidden => 403,
        }
    }
}

/// Port of `_error_envelope`: the unified error envelope shared by
/// every `/api/router/projects` handler. `extra` fields (e.g.
/// `active_connections`) merge in alongside `success`/`error`/
/// `message`.
pub fn error_envelope(
    error: LifecycleError,
    message: &str,
    extra: Option<serde_json::Value>,
) -> HandlerResponse {
    let mut body = serde_json::json!({
        "success": false,
        "error": error.discriminator(),
        "message": message,
    });
    if let Some(serde_json::Value::Object(extra_map)) = extra {
        if let serde_json::Value::Object(map) = &mut body {
            map.extend(extra_map);
        }
    }
    HandlerResponse {
        status: error.default_status(),
        headers: vec![("Cache-Control".to_string(), "no-store".to_string())],
        body: HandlerBody::Json(body),
    }
}

/// Same as [`error_envelope`] but with an explicit status override --
/// for the rare call site (R9-F2's rank denial, R1-F1's escape hatch)
/// that reuses a discriminator's message shape at a status other than
/// its documented default.
pub fn error_envelope_with_status(
    error: LifecycleError,
    message: &str,
    status: u16,
    extra: Option<serde_json::Value>,
) -> HandlerResponse {
    let mut resp = error_envelope(error, message, extra);
    resp.status = status;
    resp
}

/// Port of `_success_envelope`: `payload`'s fields merge in alongside
/// `success: true`.
pub fn success_envelope(payload: serde_json::Value, status: u16) -> HandlerResponse {
    let mut body = serde_json::json!({"success": true});
    if let serde_json::Value::Object(payload_map) = payload {
        if let serde_json::Value::Object(map) = &mut body {
            map.extend(payload_map);
        }
    }
    HandlerResponse {
        status,
        headers: vec![("Cache-Control".to_string(), "no-store".to_string())],
        body: HandlerBody::Json(body),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn set(names: &[&str]) -> HashSet<String> {
        names.iter().map(|s| s.to_string()).collect()
    }

    // -- validate_name --------------------------------------------------

    #[test]
    fn validate_name_requires_a_name() {
        assert_eq!(validate_name("", &set(&[])).unwrap(), "name is required");
    }

    #[test]
    fn validate_name_rejects_too_long() {
        let long = "a".repeat(65);
        assert!(validate_name(&long, &set(&[]))
            .unwrap()
            .contains("longer than"));
    }

    #[test]
    fn validate_name_rejects_a_non_matching_slug() {
        assert!(validate_name("Not_Valid", &set(&[]))
            .unwrap()
            .contains("lowercase"));
        assert!(validate_name("-leading-hyphen", &set(&[])).is_some());
        assert!(validate_name("trailing-hyphen-", &set(&[])).is_some());
    }

    #[test]
    fn validate_name_allows_a_single_letter() {
        assert!(validate_name("a", &set(&[])).is_none());
    }

    #[test]
    fn validate_name_rejects_a_reserved_name() {
        let err = validate_name("router", &set(&[])).unwrap();
        assert!(err.contains("reserved"));
        assert!(err.contains("api, app, assets, mcp, router"));
    }

    #[test]
    fn validate_name_rejects_an_existing_name() {
        let err = validate_name("proj-a", &set(&["proj-a"])).unwrap();
        assert!(err.contains("already registered"));
    }

    #[test]
    fn validate_name_accepts_a_fresh_valid_name() {
        assert!(validate_name("proj-a", &set(&["proj-b"])).is_none());
    }

    // -- reject_non_str_name ---------------------------------------------

    #[test]
    fn reject_non_str_name_allows_a_missing_field() {
        assert!(reject_non_str_name(None).is_none());
    }

    #[test]
    fn reject_non_str_name_allows_a_string_value() {
        assert!(reject_non_str_name(Some(&serde_json::json!("proj-a"))).is_none());
    }

    #[test]
    fn reject_non_str_name_rejects_a_non_string_value() {
        let resp = reject_non_str_name(Some(&serde_json::json!({"nested": true}))).unwrap();
        assert_eq!(resp.status, 400);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON body");
        };
        assert_eq!(body["error"], "invalid_name");
    }

    // -- workspace_label / is_within_default_workspace --------------------

    #[test]
    fn workspace_label_relativizes_under_the_managed_parent() {
        let dir = tempfile::tempdir().unwrap();
        let parent = dir.path().join("projects");
        let workspace = parent.join("proj-a");
        std::fs::create_dir_all(&workspace).unwrap();
        assert_eq!(
            workspace_label(workspace.to_str().unwrap(), &parent),
            "proj-a"
        );
    }

    #[test]
    fn workspace_label_falls_back_to_the_leaf_name_outside_the_managed_parent() {
        let dir = tempfile::tempdir().unwrap();
        let parent = dir.path().join("projects");
        let outside = dir.path().join("elsewhere").join("custom-ws");
        std::fs::create_dir_all(&outside).unwrap();
        std::fs::create_dir_all(&parent).unwrap();
        assert_eq!(
            workspace_label(outside.to_str().unwrap(), &parent),
            "custom-ws"
        );
    }

    #[test]
    fn is_within_default_workspace_admits_a_real_child_path() {
        let dir = tempfile::tempdir().unwrap();
        let parent = dir.path().join("projects");
        let workspace = parent.join("proj-a");
        std::fs::create_dir_all(&workspace).unwrap();
        assert!(is_within_default_workspace(&workspace, &parent));
    }

    #[test]
    fn is_within_default_workspace_denies_a_path_outside_the_parent() {
        let dir = tempfile::tempdir().unwrap();
        let parent = dir.path().join("projects");
        std::fs::create_dir_all(&parent).unwrap();
        let outside = dir.path().join("elsewhere");
        std::fs::create_dir_all(&outside).unwrap();
        assert!(!is_within_default_workspace(&outside, &parent));
    }

    #[test]
    fn is_within_default_workspace_denies_a_nonexistent_sibling_outside_the_parent() {
        let dir = tempfile::tempdir().unwrap();
        let parent = dir.path().join("projects");
        std::fs::create_dir_all(&parent).unwrap();
        assert!(!is_within_default_workspace(
            &dir.path().join("does-not-exist"),
            &parent
        ));
    }

    #[test]
    fn is_within_default_workspace_admits_a_not_yet_existing_child_of_the_parent() {
        // Found-and-fixed bug regression: delete_project_handler's
        // "workspace did not exist on disk" branch (a real, documented
        // Python outcome) requires a not-yet-existing workspace path
        // to still be recognised as WITHIN the parent -- Python's own
        // Path.resolve() (no strict=True) tolerates a nonexistent
        // tail, matching this.
        let dir = tempfile::tempdir().unwrap();
        let parent = dir.path().join("projects");
        std::fs::create_dir_all(&parent).unwrap();
        let never_created = parent.join("proj-a");
        assert!(is_within_default_workspace(&never_created, &parent));
    }

    #[test]
    fn is_within_default_workspace_denies_an_embedded_nul_byte_even_when_lexically_inside() {
        use std::os::unix::ffi::OsStrExt;
        let dir = tempfile::tempdir().unwrap();
        let parent = dir.path().join("projects");
        std::fs::create_dir_all(&parent).unwrap();
        let malformed = parent.join(std::ffi::OsStr::from_bytes(b"proj\0a"));
        assert!(!is_within_default_workspace(&malformed, &parent));
    }

    // -- envelopes --------------------------------------------------------

    #[test]
    fn lifecycle_error_discriminators_and_default_statuses() {
        assert_eq!(LifecycleError::InvalidName.discriminator(), "invalid_name");
        assert_eq!(LifecycleError::InvalidName.default_status(), 400);
        assert_eq!(
            LifecycleError::AlreadyRegistered.discriminator(),
            "already_registered"
        );
        assert_eq!(LifecycleError::AlreadyRegistered.default_status(), 409);
        assert_eq!(LifecycleError::NotRegistered.default_status(), 404);
        assert_eq!(LifecycleError::ActiveSessions.default_status(), 409);
        assert_eq!(LifecycleError::NameTaken.default_status(), 409);
        assert_eq!(LifecycleError::AliasCollision.default_status(), 409);
        assert_eq!(LifecycleError::Internal.default_status(), 500);
        assert_eq!(LifecycleError::NotFound.default_status(), 404);
        assert_eq!(LifecycleError::Forbidden.default_status(), 403);
    }

    #[test]
    fn error_envelope_has_the_documented_shape() {
        let resp = error_envelope(LifecycleError::NameTaken, "already taken", None);
        assert_eq!(resp.status, 409);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON body");
        };
        assert_eq!(body["success"], false);
        assert_eq!(body["error"], "name_taken");
        assert_eq!(body["message"], "already taken");
    }

    #[test]
    fn error_envelope_merges_extra_fields() {
        let resp = error_envelope(
            LifecycleError::ActiveSessions,
            "busy",
            Some(serde_json::json!({"active_connections": 3, "agents": []})),
        );
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON body");
        };
        assert_eq!(body["active_connections"], 3);
        assert_eq!(body["agents"], serde_json::json!([]));
    }

    #[test]
    fn error_envelope_with_status_overrides_the_default() {
        let resp = error_envelope_with_status(LifecycleError::NotFound, "hidden", 404, None);
        assert_eq!(resp.status, 404);
    }

    #[test]
    fn success_envelope_merges_payload_alongside_success_true() {
        let resp = success_envelope(serde_json::json!({"project": {"name": "proj-a"}}), 201);
        assert_eq!(resp.status, 201);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON body");
        };
        assert_eq!(body["success"], true);
        assert_eq!(body["project"]["name"], "proj-a");
    }
}
