//! Names the two single-tenant authorization policies (arch-r4 #8).
//! Port of `agent_mcp/router/single_tenant.py` (Phase E2 PR 16,
//! `conexus-router-lifecycle-foundations`).
//!
//! Single-tenant mode (ADR-0008) pins a router deploy to one
//! operator-owned host, seeded with exactly one project at install
//! time. That single fact reduces to a one-line boolean
//! (`single_tenant_name.is_some()`) but drives TWO semantically
//! DIFFERENT policies depending on which call site asks -- Python's
//! own docstring argues these are "deliberately contradictory-looking"
//! (one admits through a gate that would otherwise reject; the other
//! rejects a request that would otherwise be admitted) and names them
//! separately so a new call site can't reach for an unnamed `is_some()`
//! by accident. This module is the ONE home for that raw comparison,
//! matching Python's own single-source-of-truth design.
//!
//! **Correction to an earlier PR**: [`bypasses_operator_gate`] was
//! first ported privately inside `session_gate.rs` (PR 12) since that
//! was its only caller at the time. It's re-exported here as the real,
//! shared, top-level module Python's own design calls for -- `perm_gates.py`
//! (folded into this phase's later PRs) and the lifecycle handlers both
//! need it too.
#![allow(dead_code)]

use crate::mcp_handler::{HandlerBody, HandlerResponse};

/// True iff single-tenant mode skips the operator-session/capability
/// gate for this request (no second operator/tenant audience to gate
/// against).
pub fn bypasses_operator_gate(single_tenant_name: Option<&str>) -> bool {
    single_tenant_name.is_some()
}

/// True iff single-tenant mode disables this router-admin write
/// endpoint (project topology is fixed for the deploy's lifetime;
/// create/rename/delete/remove-alias have no valid target to act on).
pub fn disables_write_endpoint(single_tenant_name: Option<&str>) -> bool {
    single_tenant_name.is_some()
}

/// Port of `_single_tenant_disabled_response`: the 410 body shared by
/// every disabled write endpoint. Body shape is locked by the
/// dashboard contract (Phase 3.5).
pub fn single_tenant_disabled_response(single_tenant_name: Option<&str>) -> HandlerResponse {
    HandlerResponse {
        status: 410,
        headers: vec![("Cache-Control".to_string(), "no-store".to_string())],
        body: HandlerBody::Json(serde_json::json!({
            "error": "endpoint_disabled_in_single_tenant_mode",
            "single_tenant_name": single_tenant_name,
        })),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn both_policies_track_the_same_configured_fact() {
        assert!(bypasses_operator_gate(Some("demo")));
        assert!(disables_write_endpoint(Some("demo")));
        assert!(!bypasses_operator_gate(None));
        assert!(!disables_write_endpoint(None));
    }

    #[test]
    fn single_tenant_disabled_response_carries_the_configured_name() {
        let resp = single_tenant_disabled_response(Some("demo"));
        assert_eq!(resp.status, 410);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected a JSON body");
        };
        assert_eq!(body["error"], "endpoint_disabled_in_single_tenant_mode");
        assert_eq!(body["single_tenant_name"], "demo");
    }
}
