//! Shared vocabulary + security-invariant decision functions for the
//! router's users/groups/project-memberships REST surface. Port of
//! `agent_mcp/router/admin_users_api.py`'s module-level helpers (2413
//! LOC total; this PR covers the envelope/validation/gate layer every
//! later create/edit/delete decision-function PR builds on). Phase
//! E2, `conexus-router-admin-users-gate` -- dedicated background
//! research pass (endpoint inventory, the last-sysadmin invariant's
//! exact 5 call sites, the 3 capability-amplification guards' exact 8
//! call sites, cycle detection) done before any code, matching this
//! migration's own discipline for large files.
//!
//! Framework-agnostic, matching every prior handler-layer module: real
//! axum route registration and the async body-read yield point
//! (`perm_gates.py`'s `read_body_and_revalidate`) stay deferred to
//! PR 23.
//!
//! **A genuine design fork, resolved by re-deriving rather than
//! following the research's own first suggestion**: the research
//! proposed unifying `_is_last_sysadmin`/`_no_sysadmin_would_remain`
//! into one function taking a `Scope` parameter. Tracing the actual
//! Python call sites shows their TIMING differs, not just their
//! scope: [`is_last_sysadmin`] is always called BEFORE the caller's
//! own mutation, excluding one specific user id ("if I remove this
//! one, would others remain?"); [`no_sysadmin_would_remain`] is always
//! called AFTER the mutation has already run on `conn`, re-evaluating
//! the CURRENT state with no exclusion at all. A single shared
//! signature would force every caller to remember which mode needs
//! which calling convention -- more error-prone than two narrowly-
//! named, cross-referencing functions, so they stay separate here
//! (matching Python's own two-function design), each documenting the
//! other's existence and its own scope/timing contract explicitly so
//! neither silently drifts.
#![allow(dead_code)]

use std::sync::LazyLock;

use conexus_core::capability::Capability;
use conexus_core::principal::Principal;
use conexus_db::group_membership_repository;
use regex::Regex;
use rusqlite::{Connection, OptionalExtension};

use crate::mcp_handler::{HandlerBody, HandlerResponse};
use crate::single_tenant::bypasses_operator_gate;

// ---------------------------------------------------------------------
// Envelope (mirrors lifecycle.rs's construction exactly -- a DIFFERENT
// closed discriminator set, per the research's own finding that this
// file's `validation_error`/generic `conflict` have no LifecycleError
// equivalent, so it earns its own enum rather than overloading that
// one).
// ---------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AdminUsersError {
    Validation,
    NotFound,
    Conflict,
    Internal,
    Forbidden,
    UnknownCapability,
    ResourceCapabilityNotDelegableToGroup,
}

impl AdminUsersError {
    pub fn discriminator(self) -> &'static str {
        match self {
            AdminUsersError::Validation => "validation_error",
            AdminUsersError::NotFound => "not_found",
            AdminUsersError::Conflict => "conflict",
            AdminUsersError::Internal => "internal_error",
            AdminUsersError::Forbidden => "forbidden",
            AdminUsersError::UnknownCapability => "unknown_capability",
            AdminUsersError::ResourceCapabilityNotDelegableToGroup => {
                "resource_capability_not_delegable_to_group"
            }
        }
    }

    pub fn default_status(self) -> u16 {
        match self {
            AdminUsersError::Validation => 400,
            AdminUsersError::NotFound => 404,
            AdminUsersError::Conflict => 409,
            AdminUsersError::Internal => 500,
            AdminUsersError::Forbidden => 403,
            AdminUsersError::UnknownCapability => 400,
            AdminUsersError::ResourceCapabilityNotDelegableToGroup => 400,
        }
    }
}

/// Port of `_error`: `{"success": false, "error", "message", ...extra}`.
pub fn error_envelope(
    error: AdminUsersError,
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

/// Port of `_success`: `{"success": true, ...payload}`.
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

// ---------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------

/// Usernames and group names share the identical pattern -- deliberately
/// distinct from `lifecycle::SLUG_RE` (project names): mixed case and
/// underscore are allowed here.
pub static USERNAME_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-zA-Z0-9_-]{1,64}$").unwrap());
pub static GROUP_NAME_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-zA-Z0-9_-]{1,64}$").unwrap());

pub fn validate_username(name: &str) -> Option<String> {
    if name.is_empty() {
        return Some("username is required".to_string());
    }
    if !USERNAME_RE.is_match(name) {
        return Some(format!(
            "username must match {}; got {name:?}",
            USERNAME_RE.as_str()
        ));
    }
    None
}

pub fn validate_group_name(name: &str) -> Option<String> {
    if name.is_empty() {
        return Some("name is required".to_string());
    }
    if !GROUP_NAME_RE.is_match(name) {
        return Some(format!(
            "name must match {}; got {name:?}",
            GROUP_NAME_RE.as_str()
        ));
    }
    None
}

pub fn validate_role(role: &str) -> Option<String> {
    if role != "operator" && role != "viewer" {
        return Some(format!(
            "role must be one of 'operator'|'viewer'; got {role:?}"
        ));
    }
    None
}

/// Port of `_reject_non_str` (PF-R7-1): guards a scalar-string body
/// field against a structured JSON type (dict/list), which would
/// otherwise reach a SQLite bind and raise uncaught. `allow_none`
/// covers an optional field (e.g. `email`) where an absent/null value
/// means "unset".
pub fn reject_non_str(
    value: Option<&serde_json::Value>,
    field: &str,
    allow_none: bool,
) -> Option<String> {
    match value {
        None | Some(serde_json::Value::Null) => {
            if allow_none {
                None
            } else {
                Some(format!("{field} is required"))
            }
        }
        Some(serde_json::Value::String(_)) => None,
        Some(other) => Some(format!(
            "{field} must be a string; got {}",
            json_type_name(other)
        )),
    }
}

fn json_type_name(v: &serde_json::Value) -> &'static str {
    match v {
        serde_json::Value::Null => "NoneType",
        serde_json::Value::Bool(_) => "bool",
        serde_json::Value::Number(n) => {
            if n.is_i64() || n.is_u64() {
                "int"
            } else {
                "float"
            }
        }
        serde_json::Value::String(_) => "str",
        serde_json::Value::Array(_) => "list",
        serde_json::Value::Object(_) => "dict",
    }
}

/// Port of `_parse_bool_field` (PF-R13-1): accepts ONLY a real JSON
/// boolean for a caller-supplied SECURITY flag (`is_sysadmin`) -- an
/// absent key yields `default`; any other JSON type (a truthy string,
/// a non-zero number, an object/array) is a validation error rather
/// than a silent, surprising coercion. Returns `(value, error)`.
pub fn parse_bool_field(
    value: Option<&serde_json::Value>,
    field: &str,
    default: bool,
) -> (bool, Option<String>) {
    match value {
        None => (default, None),
        Some(serde_json::Value::Bool(b)) => (*b, None),
        Some(other) => (
            default,
            Some(format!(
                "{field} must be a boolean (true/false); got {}",
                json_type_name(other)
            )),
        ),
    }
}

/// Port of `_split_membership_id`: `"u:<id>"`/`"g:<id>"` -> `(kind,
/// id)`. `None` on a malformed surrogate.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MembershipKind {
    User,
    Group,
}

pub fn split_membership_id(membership_id: &str) -> Option<(MembershipKind, &str)> {
    if let Some(rest) = membership_id.strip_prefix("u:") {
        return Some((MembershipKind::User, rest));
    }
    if let Some(rest) = membership_id.strip_prefix("g:") {
        return Some((MembershipKind::Group, rest));
    }
    None
}

// ---------------------------------------------------------------------
// Sysadmin-grant guard (self-escalation defence)
// ---------------------------------------------------------------------

/// Port of `_caller_is_sysadmin`. `principal_sysadmin`/`is_sysadmin_flag`
/// mirror Python's `principal.sysadmin`/`req["is_sysadmin"]` fallback --
/// threaded explicitly rather than read off a hidden request object,
/// matching this crate's "explicit input over hidden dependency"
/// convention.
pub fn caller_is_sysadmin(
    single_tenant_name: Option<&str>,
    principal_sysadmin: Option<bool>,
    is_sysadmin_flag: bool,
) -> bool {
    if bypasses_operator_gate(single_tenant_name) {
        return true;
    }
    if principal_sysadmin == Some(true) {
        return true;
    }
    is_sysadmin_flag
}

/// Port of `_forbid_sysadmin_write`.
pub fn forbid_sysadmin_write(username: &str) -> HandlerResponse {
    error_envelope(
        AdminUsersError::Forbidden,
        &format!(
            "operator {username:?} may not set 'is_sysadmin'; granting or clearing sysadmin \
             is reserved for sysadmins"
        ),
        None,
    )
}

/// Port of `_forbid_sysadmin_membership`.
pub fn forbid_sysadmin_membership(username: &str) -> HandlerResponse {
    error_envelope(
        AdminUsersError::Forbidden,
        &format!(
            "operator {username:?} may not add members to a sysadmin-flagged group; a member \
             inherits sysadmin via the group's transitive closure, so this is reserved for \
             sysadmins"
        ),
        None,
    )
}

/// Port of `_forbid_cap_amplification`.
pub fn forbid_cap_amplification(username: &str, offending: &[String]) -> HandlerResponse {
    let listed = offending
        .iter()
        .map(|c| format!("{c:?}"))
        .collect::<Vec<_>>()
        .join(", ");
    error_envelope(
        AdminUsersError::Forbidden,
        &format!("operator {username:?} may not grant capabilities they do not themselves hold: {listed}"),
        None,
    )
}

/// Port of `_is_last_sysadmin`: true iff `user_id` is the ONLY
/// remaining direct `users.is_sysadmin = 1` row. Scoped to the direct
/// flag (the canonical grant path) -- sysadmin conferred transitively
/// via a group is a separate, self-healing bit (any remaining direct
/// sysadmin can re-flag the group). Called BEFORE the caller's own
/// mutation, to decide whether to proceed. See
/// [`no_sysadmin_would_remain`] for the transitive, post-mutation
/// sibling every OTHER call site needs.
pub fn is_last_sysadmin(conn: &Connection, user_id: &str) -> rusqlite::Result<bool> {
    let other: Option<i64> = conn
        .query_row(
            "SELECT 1 FROM users WHERE is_sysadmin = 1 AND user_id != ?1 LIMIT 1",
            [user_id],
            |r| r.get(0),
        )
        .optional()?;
    Ok(other.is_none())
}

/// Port of `_no_sysadmin_would_remain` (R5-F4): true iff, given the
/// CURRENT (possibly not-yet-committed) state of `conn`, no user in
/// the deployment has effective sysadmin access -- neither the direct
/// flag nor transitively via any `is_sysadmin = 1` group's downward
/// membership closure. Callers run their mutation FIRST (inside a
/// caller-managed transaction), call this, and roll back (surfacing
/// [`last_sysadmin_error`]) when it returns `true`. See
/// [`is_last_sysadmin`] for the direct-only, pre-mutation sibling used
/// by exactly one call site (a user's own is_sysadmin demote, which
/// never touches `group_membership`).
pub fn no_sysadmin_would_remain(conn: &Connection) -> rusqlite::Result<bool> {
    let any_direct: Option<i64> = conn
        .query_row(
            "SELECT 1 FROM users WHERE is_sysadmin = 1 LIMIT 1",
            [],
            |r| r.get(0),
        )
        .optional()?;
    if any_direct.is_some() {
        return Ok(false);
    }
    let mut stmt = conn.prepare("SELECT group_id FROM groups WHERE is_sysadmin = 1")?;
    let group_ids: Vec<String> = stmt
        .query_map([], |r| r.get::<_, String>(0))?
        .collect::<rusqlite::Result<_>>()?;
    for group_id in group_ids {
        if group_membership_repository::group_has_transitive_user_member(conn, &group_id)? {
            return Ok(false);
        }
    }
    Ok(true)
}

/// Port of `_last_sysadmin_error`.
pub fn last_sysadmin_error(verb: &str) -> HandlerResponse {
    error_envelope(
        AdminUsersError::Conflict,
        &format!("cannot {verb} the last remaining sysadmin; promote another user or group to sysadmin first"),
        None,
    )
}

/// Port of `_group_resolved_capabilities`: every capability a NEW
/// member of `group_id` would inherit -- the union of
/// `group_capability` grants across `group_id` and its ancestor
/// closure. Defensive against a pre-migration DB missing the
/// `group_capability` table (degrades to empty, mirroring
/// `resolve_capabilities`'s own swallow-and-degrade posture).
pub fn group_resolved_capabilities(
    conn: &Connection,
    group_id: &str,
) -> rusqlite::Result<Vec<String>> {
    let ancestors = group_membership_repository::resolve_group_ancestors(conn, group_id)?;
    if ancestors.is_empty() {
        return Ok(Vec::new());
    }
    let placeholders = ancestors.iter().map(|_| "?").collect::<Vec<_>>().join(",");
    let sql = format!(
        "SELECT DISTINCT capability FROM group_capability WHERE group_id IN ({placeholders})"
    );
    let mut stmt = match conn.prepare(&sql) {
        Ok(s) => s,
        Err(rusqlite::Error::SqliteFailure(_, _)) => return Ok(Vec::new()), // table absent on a pre-0004 DB
        Err(e) => return Err(e),
    };
    let params: Vec<&dyn rusqlite::ToSql> = ancestors
        .iter()
        .map(|s| s as &dyn rusqlite::ToSql)
        .collect();
    let caps: Vec<String> = stmt
        .query_map(params.as_slice(), |r| r.get(0))?
        .collect::<rusqlite::Result<_>>()?;
    Ok(caps)
}

/// Port of `_caps_caller_lacks`: the subset of `caps` a NON-sysadmin
/// caller does not hold. A sysadmin may grant/confer anything
/// (`[]`). Fail closed with no Principal: every cap is un-held.
pub fn caps_caller_lacks(
    is_sysadmin: bool,
    principal: Option<&Principal>,
    caps: &[String],
) -> Vec<String> {
    if is_sysadmin {
        return Vec::new();
    }
    let Some(principal) = principal else {
        let mut sorted = caps.to_vec();
        sorted.sort();
        return sorted;
    };
    let mut lacking: Vec<String> = caps
        .iter()
        .filter(|c| match c.parse::<Capability>() {
            Ok(cap) => !principal.has_capability(cap),
            // An unrecognised capability string can never be "held" --
            // fail closed exactly like a real, un-held capability.
            Err(_) => true,
        })
        .cloned()
        .collect();
    lacking.sort();
    lacking
}

/// Port of `_membership_grant_denied` (SEC round 5, AZ-R5-1): denies a
/// non-sysadmin caller conferring PROJECT access above their own
/// effective role. A sysadmin may confer anything (`None`); a caller
/// with no resolved role on `project_name` may confer nothing; a
/// caller may not confer a role ranked above their own. Fail closed
/// with no caller identity.
pub fn membership_grant_denied(
    is_sysadmin: bool,
    caller_username: &str,
    caller_role: Option<&str>,
    project_name: &str,
    conferred_role: &str,
) -> Option<HandlerResponse> {
    if is_sysadmin {
        return None;
    }
    let denied = match caller_role {
        None => true,
        Some(role) => {
            group_membership_repository::role_rank(conferred_role)
                > group_membership_repository::role_rank(role)
        }
    };
    if !denied {
        return None;
    }
    let held = caller_role.unwrap_or("none");
    Some(error_envelope(
        AdminUsersError::Forbidden,
        &format!(
            "operator {caller_username:?} may not confer role {conferred_role:?} on project \
             {project_name:?}: a non-sysadmin may only grant membership at or below their own \
             role on that project (currently {held})"
        ),
        None,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::identity;
    use conexus_db::schema::init_router_schema;

    fn conn() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch("PRAGMA foreign_keys = ON;").unwrap();
        init_router_schema(&c).unwrap();
        c
    }
    const NOW_STR: &str = "2026-01-01T00:00:00.000+00:00";

    // -- envelopes ----------------------------------------------------

    #[test]
    fn error_envelope_has_the_documented_shape() {
        let resp = error_envelope(AdminUsersError::Conflict, "already exists", None);
        assert_eq!(resp.status, 409);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON");
        };
        assert_eq!(body["success"], false);
        assert_eq!(body["error"], "conflict");
    }

    #[test]
    fn success_envelope_merges_payload() {
        let resp = success_envelope(serde_json::json!({"user": {"username": "alice"}}), 201);
        assert_eq!(resp.status, 201);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON");
        };
        assert_eq!(body["success"], true);
        assert_eq!(body["user"]["username"], "alice");
    }

    // -- validators -----------------------------------------------------

    #[test]
    fn validate_username_accepts_and_rejects() {
        assert!(validate_username("alice_1").is_none());
        assert!(validate_username("").is_some());
        assert!(validate_username("has space").is_some());
    }

    #[test]
    fn validate_role_accepts_only_operator_or_viewer() {
        assert!(validate_role("operator").is_none());
        assert!(validate_role("viewer").is_none());
        assert!(validate_role("admin").is_some());
    }

    #[test]
    fn reject_non_str_allows_none_only_when_allowed() {
        assert!(reject_non_str(None, "email", true).is_none());
        assert!(reject_non_str(None, "email", false).is_some());
        assert!(reject_non_str(Some(&serde_json::json!("a@b.test")), "email", false).is_none());
        assert!(reject_non_str(Some(&serde_json::json!({"a": 1})), "email", true).is_some());
    }

    #[test]
    fn parse_bool_field_accepts_only_real_booleans() {
        assert_eq!(parse_bool_field(None, "is_sysadmin", false), (false, None));
        assert_eq!(
            parse_bool_field(Some(&serde_json::json!(true)), "is_sysadmin", false),
            (true, None)
        );
        let (value, err) = parse_bool_field(Some(&serde_json::json!("true")), "is_sysadmin", false);
        assert!(!value);
        assert!(err.is_some());
        let (value, err) = parse_bool_field(Some(&serde_json::json!(1)), "is_sysadmin", false);
        assert!(!value);
        assert!(err.is_some());
    }

    #[test]
    fn split_membership_id_parses_both_prefixes_and_rejects_garbage() {
        assert_eq!(
            split_membership_id("u:abc"),
            Some((MembershipKind::User, "abc"))
        );
        assert_eq!(
            split_membership_id("g:def"),
            Some((MembershipKind::Group, "def"))
        );
        assert_eq!(split_membership_id("abc"), None);
    }

    // -- caller_is_sysadmin ---------------------------------------------

    #[test]
    fn caller_is_sysadmin_checks_single_tenant_then_principal_then_flag() {
        assert!(caller_is_sysadmin(Some("demo"), Some(false), false));
        assert!(caller_is_sysadmin(None, Some(true), false));
        assert!(caller_is_sysadmin(None, None, true));
        assert!(!caller_is_sysadmin(None, Some(false), false));
    }

    // -- last-sysadmin invariant ------------------------------------------

    #[test]
    fn is_last_sysadmin_true_when_no_other_direct_sysadmin_exists() {
        let mut c = conn();
        let uid = identity::create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW_STR,
        )
        .unwrap();
        assert!(is_last_sysadmin(&c, &uid).unwrap());
    }

    #[test]
    fn is_last_sysadmin_false_when_another_direct_sysadmin_exists() {
        let mut c = conn();
        let alice = identity::create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW_STR,
        )
        .unwrap();
        c.execute("INSERT INTO users (user_id, username, created_at, is_sysadmin) VALUES ('bob', 'bob', ?1, 1)", [NOW_STR]).unwrap();
        assert!(!is_last_sysadmin(&c, &alice).unwrap());
    }

    #[test]
    fn no_sysadmin_would_remain_true_when_no_direct_and_no_transitive_sysadmin() {
        let c = conn();
        assert!(no_sysadmin_would_remain(&c).unwrap());
    }

    #[test]
    fn no_sysadmin_would_remain_false_with_a_direct_sysadmin() {
        let mut c = conn();
        identity::create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW_STR,
        )
        .unwrap();
        assert!(!no_sysadmin_would_remain(&c).unwrap());
    }

    #[test]
    fn no_sysadmin_would_remain_false_when_a_sysadmin_group_still_has_a_live_member() {
        let mut c = conn();
        // No direct sysadmin user, but a sysadmin-flagged group with a
        // live user member -- this is exactly the vector
        // `_is_last_sysadmin`-shaped per-row checks structurally miss.
        c.execute_batch(
            "INSERT INTO users (user_id, username, created_at, is_sysadmin) VALUES ('u1', 'u1', '2026-01-01T00:00:00Z', 0);
             INSERT INTO groups (group_id, name, is_sysadmin, created_at) VALUES ('g1', 'g1', 1, '2026-01-01T00:00:00Z');
             INSERT INTO group_membership (group_id, member_user_id, added_at) VALUES ('g1', 'u1', '2026-01-01T00:00:00Z');",
        )
        .unwrap();
        let _ = &mut c;
        assert!(!no_sysadmin_would_remain(&c).unwrap());
    }

    // -- capability amplification -----------------------------------------

    #[test]
    fn caps_caller_lacks_is_empty_for_a_sysadmin() {
        assert!(caps_caller_lacks(true, None, &["system.users.manage".to_string()]).is_empty());
    }

    #[test]
    fn caps_caller_lacks_fails_closed_with_no_principal() {
        let lacking = caps_caller_lacks(false, None, &["system.users.manage".to_string()]);
        assert_eq!(lacking, vec!["system.users.manage".to_string()]);
    }

    // -- membership_grant_denied -------------------------------------------

    #[test]
    fn membership_grant_denied_allows_a_sysadmin_to_confer_anything() {
        assert!(membership_grant_denied(true, "alice", None, "proj-a", "operator").is_none());
    }

    #[test]
    fn membership_grant_denied_denies_a_non_member_conferring_anything() {
        assert!(membership_grant_denied(false, "alice", None, "proj-a", "viewer").is_some());
    }

    #[test]
    fn membership_grant_denied_denies_conferring_above_the_callers_own_rank() {
        assert!(
            membership_grant_denied(false, "alice", Some("viewer"), "proj-a", "operator").is_some()
        );
    }

    #[test]
    fn membership_grant_denied_allows_conferring_at_or_below_the_callers_own_rank() {
        assert!(
            membership_grant_denied(false, "alice", Some("operator"), "proj-a", "viewer").is_none()
        );
        assert!(
            membership_grant_denied(false, "alice", Some("operator"), "proj-a", "operator")
                .is_none()
        );
    }
}
