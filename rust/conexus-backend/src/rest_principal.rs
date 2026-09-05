//! `RestPrincipal` — the typed identity admitted at the backend's
//! `/api` REST door. Port of `agent_mcp/app/rest_principal.py` +
//! `agent_mcp/app/deps.py::require_operator_session`, narrowed to the
//! two doors the operator kept for this port (2026-09-05 decision,
//! `prancy-napping-pie.md` Phase E1): **forwarding-header only** —
//! the per-project backend stays router-DB-blind, exactly like `/mcp`
//! auth. Python's THIRD door (`kind="session"`, an `agent_mcp_session`
//! cookie) is deliberately NOT ported: `deps.py`'s own docstring
//! documents that path performing a LIVE `router.db` lookup
//! (`identity.get_session`/`group_resolver.resolve_user_*`) on every
//! request as "defence in depth for a misconfiguration that bypassed
//! the router middleware" — this backend is UDS-only and reachable
//! only through the router's proxy in every real deployment, so there
//! is no real "bypassed the router" caller to protect here, only
//! Python's own defensive redundancy that would otherwise require a
//! new `router.db` handle this decision explicitly rules out.
//!
//! Why this is a SEPARATE type from `conexus_core::principal::Principal`
//! (same reasoning as Python's module doc): `RestPrincipal` is an
//! ADMISSION record ("which door, what did it prove"); `Principal` is
//! an AUTHORIZATION subject ("what may they do"). The one conversion
//! between them is [`build_dispatch_principal`], mirroring Python's
//! `_dispatch_helpers._build_route_principal`.
//!
//! This distinction is NOT incidental — it is load-bearing for
//! [`is_confirmed_operator_tier`] below, which must answer differently
//! for the SAME forwarding-header admission depending on which
//! predicate asks: `conexus_core::principal::is_confirmed_operator_tier`
//! (the MCP-side, ADR-0025 predicate) confirms a forwarding caller
//! whose signed role is "operator" via its "backend can SEE a resolved
//! operator identity" clause; the REST-side predicate here does NOT,
//! deliberately, matching `agent_mcp/app/routers/composition.py::
//! is_confirmed_operator_tier`'s own documented scope note: even
//! though the same signed role is technically available on the REST
//! admission record, REST's `/api/tokens`/`/api/all-data` secret
//! surfaces don't widen who receives plaintext bearers just because
//! Finding D made the data available — a deliberate policy choice, not
//! a technical gap. Preserved bit-for-bit, not reconciled.

use conexus_core::capability::AgentRole;
use conexus_core::capability::{project_role_bundle, Capabilities, ProjectRole};
use conexus_core::principal::{Principal, PrincipalKind};
use conexus_db::agent_repository::AgentRepository;
use rusqlite::Connection;

pub use crate::principal_resolve::PrincipalRejected;

/// Which REST door admitted the caller, and what that door proved.
///
/// Only two variants exist (Python has three — see the module doc for
/// why the cookie door is dropped here).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RestPrincipal {
    /// A verified, HMAC-signed `X-Agent-MCP-Forwarded-Operator` header.
    /// `sysadmin` is deliberately absent: the wire format never carries
    /// it (matches `principal_resolve::resolve_principal`'s own
    /// hardcoded `sysadmin: false` for this door).
    Forwarding {
        operator_id: String,
        project_role: ProjectRole,
    },
    /// An `Authorization: Bearer <token>` whose `agents` row has
    /// `agent_role == Manager` — worker tokens are rejected at this
    /// door (no privilege escalation from worker to operator-tier
    /// REST surface), matching `deps._is_operator_tier_bearer`. The
    /// raw bearer is threaded through for audit-log `source_token`
    /// attribution on the dispatched `Principal`.
    OperatorBearer { bearer_token: String },
}

impl RestPrincipal {
    /// The identifier used for audit-log attribution. Port of
    /// `deps.caller_identity`, narrowed to the two remaining doors:
    /// forwarding surfaces the signed operator id; the bearer door
    /// identifies an agent rather than a person and falls back to the
    /// literal `"admin"`, matching Python exactly.
    pub fn caller_identity(&self) -> String {
        match self {
            RestPrincipal::Forwarding { operator_id, .. } => operator_id.clone(),
            RestPrincipal::OperatorBearer { .. } => "admin".to_string(),
        }
    }
}

/// Resolve the caller's [`RestPrincipal`] for one `/api` request.
///
/// Resolution order mirrors `principal_resolve::resolve_principal`'s
/// two shared doors exactly (same forwarding-header verify primitive,
/// same dormant-key fall-through), with one REST-specific narrowing:
/// a bearer that resolves to a non-`Manager` agent role (worker, or an
/// unrecognized role string) is REJECTED here rather than admitted —
/// `/mcp` admits any valid agent bearer, `/api` admits operator-tier
/// bearers only.
pub fn resolve_rest_principal(
    conn: &Connection,
    authorization_header: Option<&str>,
    forwarding_header_value: Option<&str>,
    hmac_key: Option<&[u8]>,
    now_unix: u64,
) -> Result<RestPrincipal, PrincipalRejected> {
    if let (Some(header_value), Some(key)) = (forwarding_header_value, hmac_key) {
        return match conexus_auth::forwarding_header::verify(
            header_value,
            key,
            now_unix,
            conexus_auth::forwarding_header::DEFAULT_REPLAY_WINDOW_SEC,
        ) {
            Some((operator_id, role)) => {
                let project_role = match role {
                    conexus_auth::forwarding_header::ForwardedRole::Operator => {
                        ProjectRole::Operator
                    }
                    conexus_auth::forwarding_header::ForwardedRole::Viewer => ProjectRole::Viewer,
                };
                Ok(RestPrincipal::Forwarding {
                    operator_id,
                    project_role,
                })
            }
            // A present-but-unverifiable header is a hard rejection --
            // NEVER falls through to the bearer path, matching both
            // `/mcp`'s gate and Python's own documented contract.
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
            reason: "Unauthorized: signed forwarding header or operator-tier bearer required."
                .to_string(),
        });
    };

    let row = AgentRepository::get_by_token(conn, token).map_err(|_| PrincipalRejected {
        reason: "Unauthorized: failed to resolve agent".to_string(),
    })?;
    let Some(row) = row else {
        return Err(PrincipalRejected {
            reason: "Unauthorized: signed forwarding header or operator-tier bearer required."
                .to_string(),
        });
    };

    if row.agent_role != "manager" {
        // Matches `deps._is_operator_tier_bearer`: a worker (or any
        // non-manager role string) bearer is authenticated but not
        // operator-tier -- rejected here, not silently downgraded, so
        // a worker can't reach the operator-only REST surface at all.
        return Err(PrincipalRejected {
            reason: "Unauthorized: signed forwarding header or operator-tier bearer required."
                .to_string(),
        });
    }

    Ok(RestPrincipal::OperatorBearer {
        bearer_token: token.to_string(),
    })
}

/// REST-side "is this caller CONFIRMED operator tier?" — the
/// defense-in-depth predicate behind the coarse capability gate,
/// deciding whether a caller may receive plaintext agent bearer
/// tokens / project secrets. Port of `agent_mcp/app/routers/
/// composition.py::is_confirmed_operator_tier`'s two-remaining-door
/// scope. See the module doc for why this deliberately does NOT reuse
/// `conexus_core::principal::is_confirmed_operator_tier` (the MCP-side
/// predicate answers differently for the identical forwarding
/// admission) and does NOT consult the forwarding door's signed role
/// at all -- a forwarding caller is unconditionally NOT confirmed on
/// REST, matching Python's own documented scope note bit-for-bit.
pub fn is_confirmed_operator_tier(principal: &RestPrincipal) -> bool {
    matches!(principal, RestPrincipal::OperatorBearer { .. })
}

/// Convert a REST admission into the [`Principal`] the tool dispatcher
/// runs a call under. Port of `_dispatch_helpers._build_route_principal`,
/// narrowed to the two doors above (both collapse to `OperatorSession`
/// kind, matching Python's own hard-coded `kind="operator_session"` —
/// REST never dispatches as `ForwardingHeader` kind, unlike `/mcp`).
///
/// AC-R5-1 preserved: the forwarding door threads its REAL signed
/// `project_role` (a viewer-signed header gets a viewer-role Principal
/// whose capability set denies mutation, never the full operator
/// bundle); the operator-bearer door has no project-role concept of
/// its own and gets the historical `Operator` default, matching
/// Python's `route_role() -> None` fallback exactly.
pub fn build_dispatch_principal(principal: &RestPrincipal) -> Principal {
    let (user_id, project_role, source_token) = match principal {
        RestPrincipal::Forwarding {
            operator_id,
            project_role,
        } => (operator_id.clone(), *project_role, None),
        RestPrincipal::OperatorBearer { bearer_token } => (
            principal.caller_identity(),
            ProjectRole::Operator,
            Some(bearer_token.clone()),
        ),
    };
    Principal {
        kind: PrincipalKind::OperatorSession,
        user_id: Some(user_id),
        agent_id: None,
        project_name: None,
        project_role: Some(project_role),
        agent_role: None::<AgentRole>,
        can_wake_loop: false,
        source_token,
        capabilities: Capabilities::Set(project_role_bundle(project_role)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
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
        assert!(resolve_rest_principal(&conn, None, None, None, 1000).is_err());
    }

    #[test]
    fn a_worker_bearer_is_rejected_even_though_its_a_valid_agent() {
        let conn = test_conn();
        seed_agent(&conn, "tok123", "agent-1", "worker");
        let result = resolve_rest_principal(&conn, Some("Bearer tok123"), None, None, 1000);
        assert!(
            result.is_err(),
            "a worker bearer must not reach the operator-only REST door"
        );
    }

    #[test]
    fn a_manager_bearer_resolves_operator_bearer() {
        let conn = test_conn();
        seed_agent(&conn, "tok123", "manager", "manager");
        let principal =
            resolve_rest_principal(&conn, Some("Bearer tok123"), None, None, 1000).unwrap();
        assert_eq!(
            principal,
            RestPrincipal::OperatorBearer {
                bearer_token: "tok123".to_string()
            }
        );
    }

    #[test]
    fn an_unknown_bearer_is_rejected() {
        let conn = test_conn();
        let result = resolve_rest_principal(&conn, Some("Bearer nope"), None, None, 1000);
        assert!(result.is_err());
    }

    #[test]
    fn a_verified_forwarding_header_resolves_its_real_signed_role() {
        let conn = test_conn();
        let key = b"test-key-bytes";
        let signed = conexus_auth::forwarding_header::sign(
            "op1",
            conexus_auth::forwarding_header::ForwardedRole::Viewer,
            key,
            1000,
            conexus_auth::forwarding_header::DEFAULT_TTL_SEC,
        );
        let principal =
            resolve_rest_principal(&conn, None, Some(&signed), Some(key.as_slice()), 1005).unwrap();
        assert_eq!(
            principal,
            RestPrincipal::Forwarding {
                operator_id: "op1".to_string(),
                project_role: ProjectRole::Viewer,
            }
        );
    }

    #[test]
    fn an_unverifiable_forwarding_header_is_rejected_and_never_falls_through_to_bearer() {
        let conn = test_conn();
        seed_agent(&conn, "tok123", "manager", "manager");
        let key = b"test-key-bytes";
        let result = resolve_rest_principal(
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
    fn forwarding_header_present_with_no_key_loaded_falls_through_to_bearer() {
        let conn = test_conn();
        seed_agent(&conn, "tok123", "manager", "manager");
        let principal = resolve_rest_principal(
            &conn,
            Some("Bearer tok123"),
            Some("op1.operator.9999999999.deadbeef"),
            None,
            1000,
        )
        .unwrap();
        assert_eq!(
            principal,
            RestPrincipal::OperatorBearer {
                bearer_token: "tok123".to_string()
            }
        );
    }

    #[test]
    fn operator_bearer_is_confirmed_operator_tier() {
        let p = RestPrincipal::OperatorBearer {
            bearer_token: "t".to_string(),
        };
        assert!(is_confirmed_operator_tier(&p));
    }

    #[test]
    fn forwarding_operator_role_is_never_confirmed_on_rest_even_though_mcp_would_confirm_it() {
        // ADR-0025's REST-specific scope note, preserved bit-for-bit:
        // the MCP-side predicate WOULD confirm this (project_role ==
        // Operator), but REST deliberately never does, regardless of
        // the signed role.
        let p = RestPrincipal::Forwarding {
            operator_id: "op1".to_string(),
            project_role: ProjectRole::Operator,
        };
        assert!(!is_confirmed_operator_tier(&p));
    }

    #[test]
    fn forwarding_viewer_role_is_never_confirmed_either() {
        let p = RestPrincipal::Forwarding {
            operator_id: "op1".to_string(),
            project_role: ProjectRole::Viewer,
        };
        assert!(!is_confirmed_operator_tier(&p));
    }

    #[test]
    fn dispatch_principal_for_forwarding_threads_the_real_signed_role() {
        let p = RestPrincipal::Forwarding {
            operator_id: "op1".to_string(),
            project_role: ProjectRole::Viewer,
        };
        let dispatched = build_dispatch_principal(&p);
        assert_eq!(dispatched.kind, PrincipalKind::OperatorSession);
        assert_eq!(dispatched.user_id.as_deref(), Some("op1"));
        assert_eq!(dispatched.project_role, Some(ProjectRole::Viewer));
        // AC-R5-1: a viewer-signed header must NOT resolve write
        // capabilities.
        assert!(!dispatched.has_capability(conexus_core::capability::Capability::SystemConfigWrite));
    }

    #[test]
    fn dispatch_principal_for_operator_bearer_defaults_to_the_operator_role() {
        let p = RestPrincipal::OperatorBearer {
            bearer_token: "tok123".to_string(),
        };
        let dispatched = build_dispatch_principal(&p);
        assert_eq!(dispatched.kind, PrincipalKind::OperatorSession);
        assert_eq!(dispatched.user_id.as_deref(), Some("admin"));
        assert_eq!(dispatched.project_role, Some(ProjectRole::Operator));
        assert_eq!(dispatched.source_token.as_deref(), Some("tok123"));
        assert!(dispatched.has_capability(conexus_core::capability::Capability::SystemConfigWrite));
    }
}
