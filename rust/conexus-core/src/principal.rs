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

/// A caller's MCP-catalog role — the single value every catalog
/// surface (`tools/list`, `prompts/list`+`prompts/get`,
/// `resources/list`+`resources/read`) filters visibility on.
///
/// Faithful port of `agent_mcp/core/principal_builder.py::CatalogRole`
/// and `catalog_role()`. Before that Python function existed, the
/// three surfaces each re-derived "is this caller an admin"
/// differently and disagreed (a viewer-tier forwarding-header caller
/// resolved `"anonymous"` for `tools/list` but `"worker"` for
/// prompts) — a single source of truth removes that drift class here
/// too.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CatalogRole {
    /// No authenticated Principal in flight.
    Anonymous,
    /// Operator-tier (`is_operator_tier`) or the legacy `"admin"`
    /// pseudo-agent.
    Admin,
    /// Any other authenticated Principal — an agent bearer, or a
    /// viewer-tier operator/forwarding-header caller. Carries read
    /// capabilities, so the worker-tier catalog is exactly what it
    /// can act on; `Anonymous` would wrongly hide those.
    Worker,
}

impl CatalogRole {
    /// Lowercase role vocabulary matching Python's own `catalog_role`
    /// string return values (`"admin"`/`"worker"`/`"anonymous"`) --
    /// for user-facing message text only, never for equality
    /// comparisons (compare the enum directly for those).
    pub fn as_str(&self) -> &'static str {
        match self {
            CatalogRole::Anonymous => "anonymous",
            CatalogRole::Admin => "admin",
            CatalogRole::Worker => "worker",
        }
    }
}

/// The single source of truth for a caller's MCP-catalog role. See
/// [`CatalogRole`]'s own doc for why every catalog surface routes
/// through this one function rather than re-deriving admin-ness.
pub fn catalog_role(principal: Option<&Principal>) -> CatalogRole {
    match principal {
        None => CatalogRole::Anonymous,
        Some(p) if is_operator_tier(p) => CatalogRole::Admin,
        Some(_) => CatalogRole::Worker,
    }
}

/// True iff `principal` is CONFIRMED operator tier — the defense-in-
/// depth predicate BEHIND the coarse capability gate deciding whether
/// a caller may receive plaintext agent bearer tokens / project
/// secrets, or must have them masked.
///
/// Faithful port of `agent_mcp/core/operator_tier.py::
/// is_confirmed_operator_tier`, narrowed to the identity shapes this
/// crate's `Principal` can actually represent. Python's version also
/// accepts a REST-side `"operator_bearer"` kind and an `"admin"`
/// agent-role string that only exist on that separate `RestPrincipal`
/// type (no such kind or role exists on `PrincipalKind`/`AgentRole`
/// here), so this port only implements the two clauses reachable
/// through this crate's own `Principal`:
///
/// 1. `AgentBearer` is confirmed iff its `agent_role` is `Manager`
///    (the only operator-tier `AgentRole` variant this crate has —
///    Python's `Worker` OR `Manager` OR `Admin` narrows to just
///    `Manager` here, since `Worker` was never operator-tier and
///    `Admin` isn't a representable `AgentRole` value).
/// 2. `OperatorSession` and `ForwardingHeader` are BOTH confirmed
///    only via "the backend can SEE a resolved operator identity" —
///    the sysadmin wildcard, or `project_role == Operator`. Per the
///    Python module's own docstring, a signed-forwarding caller is
///    deliberately NOT given clause-1 treatment even though it
///    carries a signed role — ADR-0025's forwarding-tier-exclusion
///    principle, preserved bit-for-bit here.
pub fn is_confirmed_operator_tier(principal: &Principal) -> bool {
    match principal.kind {
        PrincipalKind::AgentBearer => {
            principal.agent_role == Some(crate::capability::AgentRole::Manager)
        }
        PrincipalKind::OperatorSession | PrincipalKind::ForwardingHeader => {
            matches!(principal.capabilities, Capabilities::Sysadmin)
                || principal.project_role == Some(ProjectRole::Operator)
        }
    }
}

#[cfg(test)]
mod tests {
    use crate::capability::{AgentRole, Capabilities, Capability, ProjectRole};
    use crate::principal::{
        catalog_role, is_confirmed_operator_tier, is_operator_tier, CatalogRole, Principal,
        PrincipalKind,
    };

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

    // ── is_confirmed_operator_tier ──────────────────────────────────

    #[test]
    fn manager_agent_bearer_is_confirmed() {
        let mut p = base_principal(PrincipalKind::AgentBearer, Capabilities::from_iter([]));
        p.agent_role = Some(AgentRole::Manager);
        assert!(is_confirmed_operator_tier(&p));
    }

    #[test]
    fn worker_agent_bearer_is_not_confirmed() {
        let mut p = base_principal(PrincipalKind::AgentBearer, Capabilities::from_iter([]));
        p.agent_role = Some(AgentRole::Worker);
        assert!(!is_confirmed_operator_tier(&p));
    }

    #[test]
    fn agent_bearer_with_no_role_is_not_confirmed() {
        let p = base_principal(PrincipalKind::AgentBearer, Capabilities::from_iter([]));
        assert!(!is_confirmed_operator_tier(&p));
    }

    #[test]
    fn sysadmin_operator_session_is_confirmed() {
        let p = base_principal(PrincipalKind::OperatorSession, Capabilities::Sysadmin);
        assert!(is_confirmed_operator_tier(&p));
    }

    #[test]
    fn operator_role_operator_session_is_confirmed() {
        let mut p = base_principal(PrincipalKind::OperatorSession, Capabilities::from_iter([]));
        p.project_role = Some(ProjectRole::Operator);
        assert!(is_confirmed_operator_tier(&p));
    }

    #[test]
    fn viewer_role_operator_session_is_not_confirmed() {
        let mut p = base_principal(PrincipalKind::OperatorSession, Capabilities::from_iter([]));
        p.project_role = Some(ProjectRole::Viewer);
        assert!(!is_confirmed_operator_tier(&p));
    }

    #[test]
    fn operator_session_with_no_role_at_all_is_not_confirmed() {
        let p = base_principal(PrincipalKind::OperatorSession, Capabilities::from_iter([]));
        assert!(!is_confirmed_operator_tier(&p));
    }

    #[test]
    fn forwarding_header_with_operator_role_is_confirmed() {
        // Clause 2 applies identically to ForwardingHeader as it does
        // to OperatorSession -- "the backend can SEE a resolved
        // operator identity" doesn't care which seam proved it.
        let mut p = base_principal(PrincipalKind::ForwardingHeader, Capabilities::from_iter([]));
        p.project_role = Some(ProjectRole::Operator);
        assert!(is_confirmed_operator_tier(&p));
    }

    #[test]
    fn forwarding_header_never_gets_clause_1_treatment() {
        // ADR-0025: a forwarding-header caller is deliberately NOT
        // confirmed via a bearer-like clause even if it somehow
        // carried an agent_role -- only clause 2 (sysadmin /
        // project_role) applies to this kind. Setting agent_role here
        // must have NO effect.
        let mut p = base_principal(PrincipalKind::ForwardingHeader, Capabilities::from_iter([]));
        p.agent_role = Some(AgentRole::Manager);
        assert!(!is_confirmed_operator_tier(&p));
    }

    #[test]
    fn forwarding_header_with_viewer_role_is_not_confirmed() {
        let mut p = base_principal(PrincipalKind::ForwardingHeader, Capabilities::from_iter([]));
        p.project_role = Some(ProjectRole::Viewer);
        assert!(!is_confirmed_operator_tier(&p));
    }

    // -- catalog_role ----------------------------------------------

    #[test]
    fn no_principal_resolves_to_anonymous() {
        assert_eq!(catalog_role(None), CatalogRole::Anonymous);
    }

    #[test]
    fn an_operator_tier_principal_resolves_to_admin() {
        let p = base_principal(
            PrincipalKind::AgentBearer,
            Capabilities::from_iter([Capability::SystemConfigWrite]),
        );
        assert_eq!(catalog_role(Some(&p)), CatalogRole::Admin);
    }

    #[test]
    fn the_legacy_admin_pseudo_agent_resolves_to_admin_even_without_the_capability() {
        let mut p = base_principal(PrincipalKind::AgentBearer, Capabilities::from_iter([]));
        p.agent_id = Some("admin".to_string());
        assert_eq!(catalog_role(Some(&p)), CatalogRole::Admin);
    }

    #[test]
    fn a_plain_worker_bearer_resolves_to_worker() {
        let p = base_principal(
            PrincipalKind::AgentBearer,
            Capabilities::from_iter([Capability::TasksView]),
        );
        assert_eq!(catalog_role(Some(&p)), CatalogRole::Worker);
    }

    #[test]
    fn as_str_uses_pythons_lowercase_role_vocabulary() {
        assert_eq!(CatalogRole::Anonymous.as_str(), "anonymous");
        assert_eq!(CatalogRole::Admin.as_str(), "admin");
        assert_eq!(CatalogRole::Worker.as_str(), "worker");
    }

    #[test]
    fn a_viewer_tier_forwarding_header_caller_resolves_to_worker_not_anonymous() {
        // The exact drift this function exists to close: a viewer-tier
        // forwarding-header caller is authenticated and carries read
        // capabilities, so it must resolve `Worker`, not `Anonymous`.
        let mut p = base_principal(PrincipalKind::ForwardingHeader, Capabilities::from_iter([]));
        p.project_role = Some(ProjectRole::Viewer);
        assert_eq!(catalog_role(Some(&p)), CatalogRole::Worker);
    }
}
