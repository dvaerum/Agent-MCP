//! Per-request [`Principal`] construction for the per-project backend.
//!
//! Port of `agent_mcp/app/main_app.py::_build_principal_from_request` +
//! `AuthHeaderMiddleware`'s `/mcp`-gating responsibility. Resolution
//! order (identical to Python):
//!
//! 1. **Forwarding header present + a key is loaded**: verify it.
//!    A verified header builds a `ForwardingHeader` Principal carrying
//!    the operator's REAL signed role (SEC-1: never hard-coded to
//!    "operator" — a viewer-tier header must resolve viewer-tier
//!    capabilities, never the full operator bundle). An
//!    UNVERIFIABLE header (wrong HMAC, expired, malformed) is a hard
//!    rejection — this never falls through to the bearer path.
//! 2. **Forwarding header present but NO key loaded**: the one soft
//!    case Python's own docstring calls out — "dormant-key
//!    transitional behavior" — falls through to the bearer path
//!    rather than rejecting.
//! 3. **`Authorization: Bearer <token>`**: resolved via
//!    `agent_repository::get_by_token`.
//! 4. **Neither present, or the resolved bearer doesn't exist**:
//!    rejected. Unlike a REST route with genuinely anonymous surfaces,
//!    `/mcp` itself has no anonymous path in Python either (the
//!    `AuthHeaderMiddleware` gate) — every real MCP request must carry
//!    SOME admitted identity.
//!
//! `router_conn: None` throughout: the per-project backend has no
//! router.db handle, so the group-capability overlay is always a
//! no-op here (matches Python's own documented behavior for this
//! exact seam, not a Rust-side shortcut).

use conexus_auth::forwarding_header;
use conexus_core::capability::{AgentRole, ProjectRole};
use conexus_core::principal::{Principal, PrincipalKind};
use conexus_db::agent_repository::AgentRepository;
use rusqlite::Connection;

/// Why a request's identity was rejected. Carries only a short,
/// caller-facing reason (never wraps an internal error) — matches
/// `conexus_auth::AuthRejected`'s own discipline.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PrincipalRejected {
    pub reason: String,
}

fn normalize_agent_role(raw: &str) -> Option<AgentRole> {
    match raw {
        "manager" => Some(AgentRole::Manager),
        "worker" => Some(AgentRole::Worker),
        // An unrecognized value fails closed to "no elevated role"
        // rather than guessing -- matches Python's
        // `normalize_agent_role`'s own unknown-value handling (treated
        // as worker-equivalent, never manager).
        _ => None,
    }
}

fn forwarded_role_to_project_role(role: forwarding_header::ForwardedRole) -> ProjectRole {
    match role {
        forwarding_header::ForwardedRole::Operator => ProjectRole::Operator,
        forwarding_header::ForwardedRole::Viewer => ProjectRole::Viewer,
    }
}

/// Resolve the caller's [`Principal`] for one `/mcp` request.
///
/// `hmac_key`: `None` when `--forwarding-hmac-in` was unset, the file
/// was unreadable, or it was empty (the dormant-key state — see
/// [`crate::boot::load_forwarding_hmac_key`]).
pub fn resolve_principal(
    conn: &Connection,
    authorization_header: Option<&str>,
    forwarding_header_value: Option<&str>,
    hmac_key: Option<&[u8]>,
    now_unix: u64,
) -> Result<Principal, PrincipalRejected> {
    if let (Some(header_value), Some(key)) = (forwarding_header_value, hmac_key) {
        return match forwarding_header::verify(
            header_value,
            key,
            now_unix,
            forwarding_header::DEFAULT_REPLAY_WINDOW_SEC,
        ) {
            Some((operator_id, role)) => {
                let project_role = forwarded_role_to_project_role(role);
                let caps = conexus_auth::resolve_capabilities(
                    None,
                    conexus_auth::ResolveCapabilitiesInput {
                        sysadmin: false,
                        kind: PrincipalKind::ForwardingHeader,
                        agent_role: None,
                        user_id: Some(&operator_id),
                        project_role: Some(project_role),
                        groups: None,
                    },
                )
                .map_err(|_| PrincipalRejected {
                    reason: "Unauthorized: failed to resolve capabilities".to_string(),
                })?;
                Ok(Principal {
                    kind: PrincipalKind::ForwardingHeader,
                    user_id: Some(operator_id),
                    agent_id: None,
                    project_name: None,
                    project_role: Some(project_role),
                    agent_role: None,
                    can_wake_loop: false,
                    source_token: None,
                    capabilities: caps,
                })
            }
            // A present-but-unverifiable header is a hard rejection --
            // NEVER falls through to the bearer path (matches
            // forwarding_header.py's own documented contract).
            None => Err(PrincipalRejected {
                reason: "Unauthorized: invalid forwarding header".to_string(),
            }),
        };
    }

    let bearer_token = authorization_header
        .and_then(|h| h.strip_prefix("Bearer "))
        .map(str::trim)
        .filter(|t| !t.is_empty());

    let Some(token) = bearer_token else {
        return Err(PrincipalRejected {
            reason: "Unauthorized: valid token required".to_string(),
        });
    };

    let row = AgentRepository::get_by_token(conn, token).map_err(|_| PrincipalRejected {
        reason: "Unauthorized: failed to resolve agent".to_string(),
    })?;
    let Some(row) = row else {
        return Err(PrincipalRejected {
            reason: "Unauthorized: valid token required".to_string(),
        });
    };

    let agent_role = normalize_agent_role(&row.agent_role);
    let caps = conexus_auth::resolve_capabilities(
        None,
        conexus_auth::ResolveCapabilitiesInput {
            sysadmin: false,
            kind: PrincipalKind::AgentBearer,
            agent_role,
            user_id: None,
            project_role: None,
            groups: None,
        },
    )
    .map_err(|_| PrincipalRejected {
        reason: "Unauthorized: failed to resolve capabilities".to_string(),
    })?;

    Ok(Principal {
        kind: PrincipalKind::AgentBearer,
        user_id: None,
        agent_id: Some(row.agent_id),
        project_name: None,
        project_role: None,
        agent_role,
        // Wake-loop eligibility resolution (Python's
        // `resolve_wake_loop=True` path) is Phase D3 territory -- not
        // yet ported. Every other Python call site of this builder
        // also leaves this at its historical `False` default, so this
        // isn't a regression versus most of Python's own call sites,
        // only versus the one MCP-wire seam that opts in.
        can_wake_loop: false,
        source_token: Some(token.to_string()),
        capabilities: caps,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_core::capability::Capabilities;
    use conexus_db::schema::init_schema;

    fn test_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        conn
    }

    fn seed_agent(conn: &Connection, token: &str, agent_id: &str, role: &str) {
        conn.execute(
            "INSERT INTO agents (token, agent_id, created_at, status, working_directory, agent_role) \
             VALUES (?1, ?2, '2026-01-01T00:00:00Z', 'active', '/tmp', ?3)",
            (token, agent_id, role),
        )
        .unwrap();
    }

    #[test]
    fn no_headers_at_all_is_rejected() {
        let conn = test_conn();
        let result = resolve_principal(&conn, None, None, None, 1000);
        assert!(result.is_err());
    }

    #[test]
    fn a_valid_bearer_resolves_an_agent_bearer_principal() {
        let conn = test_conn();
        seed_agent(&conn, "tok123", "agent-1", "worker");
        let principal = resolve_principal(&conn, Some("Bearer tok123"), None, None, 1000).unwrap();
        assert_eq!(principal.kind, PrincipalKind::AgentBearer);
        assert_eq!(principal.agent_id.as_deref(), Some("agent-1"));
        assert_eq!(principal.agent_role, Some(AgentRole::Worker));
    }

    #[test]
    fn an_unknown_bearer_token_is_rejected() {
        let conn = test_conn();
        let result = resolve_principal(&conn, Some("Bearer nope"), None, None, 1000);
        assert!(result.is_err());
    }

    #[test]
    fn a_malformed_authorization_header_is_rejected() {
        let conn = test_conn();
        let result = resolve_principal(&conn, Some("not-bearer-shaped"), None, None, 1000);
        assert!(result.is_err());
    }

    #[test]
    fn forwarding_header_present_with_no_key_loaded_falls_through_to_bearer() {
        let conn = test_conn();
        seed_agent(&conn, "tok123", "agent-1", "worker");
        let principal = resolve_principal(
            &conn,
            Some("Bearer tok123"),
            Some("op1.operator.9999999999.deadbeef"),
            None,
            1000,
        )
        .unwrap();
        assert_eq!(principal.kind, PrincipalKind::AgentBearer);
    }

    #[test]
    fn a_verified_forwarding_header_builds_a_forwarding_header_principal_with_its_signed_role() {
        let conn = test_conn();
        let key = b"test-key-bytes";
        let signed = forwarding_header::sign(
            "op1",
            forwarding_header::ForwardedRole::Viewer,
            key,
            1000,
            forwarding_header::DEFAULT_TTL_SEC,
        );
        let principal =
            resolve_principal(&conn, None, Some(&signed), Some(key.as_slice()), 1005).unwrap();
        assert_eq!(principal.kind, PrincipalKind::ForwardingHeader);
        assert_eq!(principal.user_id.as_deref(), Some("op1"));
        assert_eq!(principal.project_role, Some(ProjectRole::Viewer));
        // SEC-1 regression: a viewer-signed header must NOT resolve the
        // sysadmin/operator wildcard capability set.
        assert_ne!(principal.capabilities, Capabilities::Sysadmin);
    }

    #[test]
    fn an_unverifiable_forwarding_header_is_rejected_and_never_falls_through_to_bearer() {
        let conn = test_conn();
        seed_agent(&conn, "tok123", "agent-1", "worker");
        let key = b"test-key-bytes";
        let result = resolve_principal(
            &conn,
            Some("Bearer tok123"),
            Some("garbage.header.value.here"),
            Some(key.as_slice()),
            1000,
        );
        assert!(
            result.is_err(),
            "an invalid forwarding header must reject, not fall through to the valid bearer"
        );
    }

    #[test]
    fn an_unrecognized_agent_role_resolves_to_no_elevated_role() {
        // The real `agents.agent_role` column has a CHECK constraint
        // limiting it to 'worker'/'manager', so this defensive branch
        // can never actually be hit via a live DB row -- test the
        // pure mapping function directly instead of fighting the
        // schema, same as Python's own `normalize_agent_role` is unit-
        // tested directly rather than only through a DB round-trip.
        assert_eq!(normalize_agent_role("some-future-role"), None);
        assert_eq!(normalize_agent_role("worker"), Some(AgentRole::Worker));
        assert_eq!(normalize_agent_role("manager"), Some(AgentRole::Manager));
    }
}
