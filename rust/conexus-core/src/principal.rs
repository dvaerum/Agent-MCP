//! `Principal` — the typed identity of "who is making this call".
//!
//! Faithful port of `agent_mcp/core/principal.py`. Built once at the
//! outermost auth seam and threaded through every downstream decision
//! point; read-only by design. `capabilities` uses [`crate::capability::
//! Capabilities`] (the sysadmin-wildcard-or-explicit-set sum type)
//! rather than Python's `frozenset[str]` — see that module's docs for
//! why.

use crate::capability::{Capabilities, Capability, ProjectRole};

/// Which authentication surface admitted the caller.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum PrincipalKind {
    /// The dashboard cookie path.
    OperatorSession,
    /// A per-agent token on `Authorization: Bearer`.
    AgentBearer,
    /// The signed `X-Agent-MCP-Forwarded-Operator` header the router
    /// attaches when proxying a cookie-authenticated dashboard request
    /// to the per-project backend.
    ForwardingHeader,
}

impl PrincipalKind {
    fn as_str(&self) -> &'static str {
        match self {
            PrincipalKind::OperatorSession => "operator_session",
            PrincipalKind::AgentBearer => "agent_bearer",
            PrincipalKind::ForwardingHeader => "forwarding_header",
        }
    }
}

/// Immutable snapshot of the caller's authenticated identity. See the
/// Python source's docstring for the full field-by-field rationale;
/// reproduced briefly here per field.
#[derive(Debug, Clone, PartialEq)]
pub struct Principal {
    pub kind: PrincipalKind,
    /// Operator id. `None` for `AgentBearer`.
    pub user_id: Option<String>,
    /// Agent id. `None` for both operator paths.
    pub agent_id: Option<String>,
    /// The project this request targets. `None` for router-admin
    /// endpoints.
    pub project_name: Option<String>,
    /// The operator's role inside `project_name`, for the two operator
    /// paths. `None` for `AgentBearer`, or when no membership row
    /// exists and the caller isn't a sysadmin (sysadmin is instead
    /// encoded via `capabilities: Capabilities::Sysadmin`, matching the
    /// Python source's wildcard-in-`capabilities` design — there is
    /// deliberately no separate `sysadmin: bool` field here).
    pub project_role: Option<ProjectRole>,
    /// The agent-bearer's role. `None` for the operator paths.
    pub agent_role: Option<crate::capability::AgentRole>,
    /// Whether the wake-loop bootstrap instruction should be appended
    /// to this caller's `initialize` response.
    pub can_wake_loop: bool,
    /// The raw bearer value (agent-bearer callers) or the SSO provider
    /// name (audit-log attribution). `None` when neither applies.
    pub source_token: Option<String>,
    pub capabilities: Capabilities,
}

impl Principal {
    /// Return `true` iff this principal carries `cap`.
    ///
    /// * Sysadmin short-circuit: [`Capabilities::Sysadmin`] admits ANY
    ///   capability unconditionally.
    /// * Otherwise the cap must be in `self.capabilities`.
    /// * For non-`system.*` caps, additionally require the caller to
    ///   have a resolved project membership (`project_role.is_some()`)
    ///   OR be an `AgentBearer`. `system.*` caps admit on cap-set
    ///   membership alone.
    ///
    /// Returns `false` for any capability not in the set — default-deny,
    /// matching the Python source exactly.
    pub fn has_capability(&self, cap: Capability) -> bool {
        // Sysadmin short-circuit MUST come first and return unconditionally
        // — a sysadmin has no project_role at all (see the struct docs),
        // so falling through to the membership check below would wrongly
        // deny every non-system capability.
        if matches!(self.capabilities, Capabilities::Sysadmin) {
            return true;
        }
        if !self.capabilities.contains(cap) {
            return false;
        }
        if cap.is_system_tier() {
            return true;
        }
        self.project_role.is_some() || self.kind == PrincipalKind::AgentBearer
    }

    /// A short string suitable for an audit-log `agent_id` column.
    /// Picks the most specific identifier available: `agent_id` for
    /// agent bearers, `user_id` for operator paths, falling back to the
    /// kind label if neither is set (defensive — shouldn't happen in
    /// practice).
    pub fn actor_label(&self) -> &str {
        if let Some(agent_id) = &self.agent_id {
            return agent_id;
        }
        if let Some(user_id) = &self.user_id {
            return user_id;
        }
        self.kind.as_str()
    }
}

/// True iff `principal` is an operator-tier caller.
///
/// Faithful port of `agent_mcp/core/principal_builder.py::
/// is_operator_tier` — the single definition, collapsing two that had
/// drifted in the Python source's history. Operator-tier = a caller
/// carrying the per-project operator write marker
/// (`system.config.write`, present in `project_role_bundle`'s operator
/// tier and short-circuited by the sysadmin wildcard), OR the legacy
/// `agent_id == "admin"` pseudo-agent label the test harness seeds. A
/// viewer-tier operator lacks the write marker and is excluded.
pub fn is_operator_tier(principal: &Principal) -> bool {
    principal.has_capability(Capability::SystemConfigWrite)
        || principal.agent_id.as_deref() == Some("admin")
}

#[cfg(test)]
mod tests {
    use crate::capability::{Capabilities, Capability};
    use crate::principal::{is_operator_tier, Principal, PrincipalKind};

    fn base_principal(kind: PrincipalKind, capabilities: Capabilities) -> Principal {
        Principal {
            kind,
            user_id: None,
            agent_id: None,
            project_name: None,
            project_role: None,
            agent_role: None,
            can_wake_loop: false,
            source_token: None,
            capabilities,
        }
    }

    #[test]
    fn sysadmin_wildcard_admits_any_capability_unconditionally() {
        let p = base_principal(PrincipalKind::OperatorSession, Capabilities::Sysadmin);
        // No project_role set at all — a non-wildcard principal would be
        // denied every non-system cap for lacking project membership,
        // but the wildcard short-circuits before that check ever runs.
        assert!(p.has_capability(Capability::TasksAssign));
        assert!(p.has_capability(Capability::SystemSsoConfigure));
    }

    #[test]
    fn cap_not_in_capability_set_is_denied() {
        let p = base_principal(
            PrincipalKind::AgentBearer,
            Capabilities::from_iter([Capability::TasksView]),
        );
        assert!(!p.has_capability(Capability::TasksAssign));
    }

    #[test]
    fn system_tier_cap_admits_regardless_of_project_membership() {
        let mut p = base_principal(
            PrincipalKind::OperatorSession,
            Capabilities::from_iter([Capability::SystemView]),
        );
        p.project_role = None; // no project membership at all
        assert!(p.has_capability(Capability::SystemView));
    }

    #[test]
    fn non_system_cap_requires_project_membership_or_agent_bearer() {
        // Operator session with the cap but NO project_role -> denied.
        let denied = base_principal(
            PrincipalKind::OperatorSession,
            Capabilities::from_iter([Capability::TasksAssign]),
        );
        assert!(!denied.has_capability(Capability::TasksAssign));

        // Operator session with the cap AND a resolved project_role -> admitted.
        let mut admitted = base_principal(
            PrincipalKind::OperatorSession,
            Capabilities::from_iter([Capability::TasksAssign]),
        );
        admitted.project_role = Some(crate::capability::ProjectRole::Operator);
        assert!(admitted.has_capability(Capability::TasksAssign));

        // Agent bearer with the cap, no project_role at all -> admitted
        // (agent_bearer is its own membership-equivalent).
        let agent = base_principal(
            PrincipalKind::AgentBearer,
            Capabilities::from_iter([Capability::TasksAssign]),
        );
        assert!(agent.has_capability(Capability::TasksAssign));
    }

    #[test]
    fn actor_label_prefers_agent_id_then_user_id_then_kind() {
        let mut p = base_principal(PrincipalKind::AgentBearer, Capabilities::Sysadmin);
        p.agent_id = Some("alice".to_string());
        p.user_id = Some("should-not-win".to_string());
        assert_eq!(p.actor_label(), "alice");

        p.agent_id = None;
        assert_eq!(p.actor_label(), "should-not-win");

        p.user_id = None;
        assert_eq!(p.actor_label(), "agent_bearer");
    }

    // ── is_operator_tier ────────────────────────────────────────────

    #[test]
    fn caller_with_system_config_write_is_operator_tier() {
        let p = base_principal(
            PrincipalKind::OperatorSession,
            Capabilities::from_iter([Capability::SystemConfigWrite]),
        );
        assert!(is_operator_tier(&p));
    }

    #[test]
    fn sysadmin_wildcard_is_operator_tier() {
        let p = base_principal(PrincipalKind::OperatorSession, Capabilities::Sysadmin);
        assert!(is_operator_tier(&p));
    }

    #[test]
    fn legacy_admin_agent_id_is_operator_tier_even_without_the_capability() {
        let mut p = base_principal(
            PrincipalKind::AgentBearer,
            Capabilities::from_iter([Capability::TasksView]),
        );
        p.agent_id = Some("admin".to_string());
        assert!(is_operator_tier(&p));
    }

    #[test]
    fn viewer_tier_lacking_the_write_marker_is_not_operator_tier() {
        let p = base_principal(
            PrincipalKind::OperatorSession,
            Capabilities::from_iter([Capability::TasksView]),
        );
        assert!(!is_operator_tier(&p));
    }

    #[test]
    fn agent_bearer_with_an_unrelated_agent_id_is_not_operator_tier() {
        let mut p = base_principal(
            PrincipalKind::AgentBearer,
            Capabilities::from_iter([Capability::TasksView]),
        );
        p.agent_id = Some("worker-1".to_string());
        assert!(!is_operator_tier(&p));
    }
}
