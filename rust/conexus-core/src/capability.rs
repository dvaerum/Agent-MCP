//! Capability-based authorization vocabulary.
//!
//! Faithful port of `agent_mcp/core/capabilities.py`, with one
//! deliberate improvement the migration plan's exploration flagged:
//! capability strings are stringly-typed in Python (a `frozenset[str]`
//! validated only by a regex convention + a smoke test); here they're a
//! real, closed `enum` — an unknown/typo'd capability is a `FromStr`
//! parse error at the boundary (config load, DB row, API body) instead
//! of a runtime string that silently never matches anything.
//!
//! [`Capabilities`] is the other deliberate improvement: Python encodes
//! "sysadmin" as `frozenset({"*"})` — a magic sentinel string smuggled
//! into a string set — which then needs its own defense-in-depth filter
//! everywhere a capability set is built from untrusted data (see the
//! Python source's own `resolve_capabilities` comment: "the wildcard
//! must ONLY ever be mintable by the `sysadmin=True` branch... never
//! sourced from a group row"). Splitting `Capabilities` into a real
//! `Sysadmin | Set(...)` sum type makes that class of bug impossible to
//! introduce by construction — there is no string a caller could smuggle
//! into a `HashSet<Capability>` that would ever compare equal to the
//! `Sysadmin` variant.
//!
//! What's ported here vs. elsewhere: [`Capability`], [`Capabilities`],
//! [`AgentRole`]/[`ProjectRole`], and the bundle constants
//! (`agent_role_bundle`/`project_role_bundle`) are all pure, zero-I/O
//! data — they belong in `conexus-core` alongside `Principal` and
//! `ToolResult`. The Python source's `resolve_capabilities` function
//! ALSO does a DB-backed group-capability overlay
//! (`group_capability_repository.fetch` / `group_resolver.
//! resolve_user_groups`) — that half is ported in `conexus-auth`
//! (Phase C)'s `capabilities::resolve_capabilities`, composed on top
//! of `project_role_bundle`/`agent_role_bundle` rather than
//! duplicating them.

use std::collections::HashSet;
use std::fmt;
use std::str::FromStr;

/// The exact 29-entry closed set of capability strings the system
/// recognizes. Locked by Wave 9 grilling (see the Python source); adding
/// or removing a variant is a design change, not a mechanical edit.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Capability {
    McpConnect,
    AgentsView,
    AgentsRegister,
    AgentsTerminate,
    AgentsRotateToken,
    AgentsUse,
    TasksView,
    TasksCreate,
    TasksUpdate,
    TasksDelete,
    TasksAssign,
    MemoriesView,
    MemoriesCreate,
    MemoriesUpdate,
    MemoriesDelete,
    MessagesView,
    MessagesSend,
    FilesUse,
    CoordinationAssist,
    CoordinationWait,
    RagQuery,
    RagRebuild,
    SystemView,
    SystemConfigWrite,
    SystemUsersManage,
    SystemGroupsManage,
    SystemGroupsCapabilitiesManage,
    SystemProjectsManage,
    SystemSsoConfigure,
}

impl Capability {
    /// Every known capability — the Rust equivalent of
    /// `KNOWN_CAPABILITIES`.
    pub const ALL: [Capability; 29] = [
        Capability::McpConnect,
        Capability::AgentsView,
        Capability::AgentsRegister,
        Capability::AgentsTerminate,
        Capability::AgentsRotateToken,
        Capability::AgentsUse,
        Capability::TasksView,
        Capability::TasksCreate,
        Capability::TasksUpdate,
        Capability::TasksDelete,
        Capability::TasksAssign,
        Capability::MemoriesView,
        Capability::MemoriesCreate,
        Capability::MemoriesUpdate,
        Capability::MemoriesDelete,
        Capability::MessagesView,
        Capability::MessagesSend,
        Capability::FilesUse,
        Capability::CoordinationAssist,
        Capability::CoordinationWait,
        Capability::RagQuery,
        Capability::RagRebuild,
        Capability::SystemView,
        Capability::SystemConfigWrite,
        Capability::SystemUsersManage,
        Capability::SystemGroupsManage,
        Capability::SystemGroupsCapabilitiesManage,
        Capability::SystemProjectsManage,
        Capability::SystemSsoConfigure,
    ];

    /// The canonical AWS-IAM-style dotted string, matching the Python
    /// vocabulary exactly (used for DB storage, API responses, audit
    /// logs — anywhere the capability needs a wire representation).
    pub fn as_str(&self) -> &'static str {
        match self {
            Capability::McpConnect => "mcp.connect",
            Capability::AgentsView => "agents.view",
            Capability::AgentsRegister => "agents.register",
            Capability::AgentsTerminate => "agents.terminate",
            Capability::AgentsRotateToken => "agents.rotate_token",
            Capability::AgentsUse => "agents.use",
            Capability::TasksView => "tasks.view",
            Capability::TasksCreate => "tasks.create",
            Capability::TasksUpdate => "tasks.update",
            Capability::TasksDelete => "tasks.delete",
            Capability::TasksAssign => "tasks.assign",
            Capability::MemoriesView => "memories.view",
            Capability::MemoriesCreate => "memories.create",
            Capability::MemoriesUpdate => "memories.update",
            Capability::MemoriesDelete => "memories.delete",
            Capability::MessagesView => "messages.view",
            Capability::MessagesSend => "messages.send",
            Capability::FilesUse => "files.use",
            Capability::CoordinationAssist => "coordination.assist",
            Capability::CoordinationWait => "coordination.wait",
            Capability::RagQuery => "rag.query",
            Capability::RagRebuild => "rag.rebuild",
            Capability::SystemView => "system.view",
            Capability::SystemConfigWrite => "system.config.write",
            Capability::SystemUsersManage => "system.users.manage",
            Capability::SystemGroupsManage => "system.groups.manage",
            Capability::SystemGroupsCapabilitiesManage => "system.groups.capabilities.manage",
            Capability::SystemProjectsManage => "system.projects.manage",
            Capability::SystemSsoConfigure => "system.sso.configure",
        }
    }

    /// Whether this is a `system.*` (router-side admin) capability.
    /// `system.*` caps are project-membership-ungated — they're
    /// deployment-wide admin verbs that don't belong to any one
    /// project; every other capability requires the caller to have a
    /// resolved project membership or be an agent bearer. See
    /// [`crate::principal::Principal::has_capability`].
    pub fn is_system_tier(&self) -> bool {
        self.as_str().starts_with("system.")
    }
}

impl fmt::Display for Capability {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Parse error for an unrecognized capability string. Deliberately
/// carries no detail beyond the offending string — default-deny means
/// the caller treats "unknown" and "malformed" identically.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnknownCapability(pub String);

impl fmt::Display for UnknownCapability {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "unknown capability: {:?}", self.0)
    }
}

impl std::error::Error for UnknownCapability {}

impl FromStr for Capability {
    type Err = UnknownCapability;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        Capability::ALL
            .into_iter()
            .find(|c| c.as_str() == s)
            .ok_or_else(|| UnknownCapability(s.to_string()))
    }
}

/// A principal's capability set: either the sysadmin wildcard (admits
/// every capability unconditionally) or an explicit set. See the module
/// docs for why this is a real sum type rather than Python's
/// sentinel-string-in-a-set encoding.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Capabilities {
    Sysadmin,
    Set(HashSet<Capability>),
}

impl Capabilities {
    pub fn contains(&self, cap: Capability) -> bool {
        match self {
            Capabilities::Sysadmin => true,
            Capabilities::Set(s) => s.contains(&cap),
        }
    }
}

impl FromIterator<Capability> for Capabilities {
    fn from_iter<T: IntoIterator<Item = Capability>>(iter: T) -> Self {
        Capabilities::Set(iter.into_iter().collect())
    }
}

/// `agents.agent_role` — the agent-bearer supervisory tier.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum AgentRole {
    Worker,
    Manager,
}

/// `project_membership.role` — the operator-tier project role.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ProjectRole {
    Viewer,
    Operator,
}

/// Capabilities granted to agent-bearer callers by virtue of
/// `agents.agent_role`. Worker is the baseline; manager is worker +
/// supervisory verbs (task assignment + memory edit-over-others).
/// `agents.rotate_token` is deliberately in NEITHER bundle — an agent
/// must never rotate a peer's, or its own, bearer.
pub fn agent_role_bundle(role: AgentRole) -> HashSet<Capability> {
    use Capability::*;
    let mut caps = HashSet::from([
        McpConnect,
        AgentsUse,
        TasksView,
        TasksCreate,
        TasksUpdate,
        MemoriesView,
        MessagesView,
        MessagesSend,
        FilesUse,
        CoordinationAssist,
        CoordinationWait,
        RagQuery,
    ]);
    if role == AgentRole::Manager {
        caps.insert(TasksAssign);
        caps.insert(MemoriesUpdate);
    }
    caps
}

/// Capabilities granted to operator-tier callers by virtue of
/// `project_membership.role`. Viewer is read-only; operator is full
/// write within the project scope (still requires project membership —
/// the resource gate in [`crate::principal::Principal::has_capability`]
/// is what makes resource caps project-scoped).
pub fn project_role_bundle(role: ProjectRole) -> HashSet<Capability> {
    use Capability::*;
    let mut caps = HashSet::from([
        AgentsView,
        TasksView,
        MemoriesView,
        MessagesView,
        SystemView,
    ]);
    if role == ProjectRole::Operator {
        caps.extend([
            AgentsRegister,
            AgentsTerminate,
            AgentsRotateToken,
            TasksCreate,
            TasksUpdate,
            TasksDelete,
            TasksAssign,
            MemoriesCreate,
            MemoriesUpdate,
            MemoriesDelete,
            MessagesSend,
            FilesUse,
            SystemConfigWrite,
            RagQuery,
            RagRebuild,
        ]);
    }
    caps
}

#[cfg(test)]
mod tests {
    use crate::capability::{
        agent_role_bundle, project_role_bundle, AgentRole, Capabilities, Capability, ProjectRole,
    };
    use std::str::FromStr;

    #[test]
    fn known_capabilities_has_exactly_29_entries() {
        // Matches agent_mcp/core/capabilities.py's KNOWN_CAPABILITIES —
        // locked by Wave 9 grilling; adding/removing one is a design
        // change, not a mechanical edit.
        assert_eq!(Capability::ALL.len(), 29);
    }

    #[test]
    fn every_capability_round_trips_through_its_string_form() {
        for cap in Capability::ALL {
            let s = cap.as_str();
            assert_eq!(
                Capability::from_str(s),
                Ok(cap),
                "round-trip failed for {s:?}"
            );
        }
    }

    #[test]
    fn string_forms_match_the_python_dotted_vocabulary() {
        assert_eq!(Capability::McpConnect.as_str(), "mcp.connect");
        assert_eq!(Capability::TasksAssign.as_str(), "tasks.assign");
        assert_eq!(
            Capability::SystemGroupsCapabilitiesManage.as_str(),
            "system.groups.capabilities.manage"
        );
        assert_eq!(
            Capability::AgentsRotateToken.as_str(),
            "agents.rotate_token"
        );
    }

    #[test]
    fn unknown_capability_string_fails_closed() {
        // Default-deny: an unknown/typo'd cap string must not silently
        // parse into some capability — it must be a hard parse error,
        // mirroring the Python smoke test's "unknown string -> not in
        // KNOWN_CAPABILITIES -> has_capability() returns False" contract.
        assert!(Capability::from_str("tasks.frobnicate").is_err());
        assert!(Capability::from_str("").is_err());
        assert!(Capability::from_str("*").is_err()); // the wildcard is NOT a capability
    }

    #[test]
    fn is_system_tier_true_only_for_system_dot_capabilities() {
        let system_caps = [
            Capability::SystemView,
            Capability::SystemConfigWrite,
            Capability::SystemUsersManage,
            Capability::SystemGroupsManage,
            Capability::SystemGroupsCapabilitiesManage,
            Capability::SystemProjectsManage,
            Capability::SystemSsoConfigure,
        ];
        for cap in system_caps {
            assert!(cap.is_system_tier(), "{cap:?} should be system-tier");
        }
        let non_system_count = Capability::ALL
            .iter()
            .filter(|c| !c.is_system_tier())
            .count();
        assert_eq!(non_system_count, 29 - system_caps.len());
    }

    // ── Capabilities (the Sysadmin-wildcard-or-explicit-set sum type) ──

    #[test]
    fn sysadmin_wildcard_contains_every_capability() {
        let caps = Capabilities::Sysadmin;
        for cap in Capability::ALL {
            assert!(caps.contains(cap));
        }
    }

    #[test]
    fn explicit_set_only_contains_what_was_put_in() {
        let caps = Capabilities::from_iter([Capability::TasksView, Capability::TasksCreate]);
        assert!(caps.contains(Capability::TasksView));
        assert!(!caps.contains(Capability::TasksAssign));
    }

    // ── Bundles ─────────────────────────────────────────────────────

    #[test]
    fn worker_bundle_excludes_supervisory_verbs() {
        let bundle = agent_role_bundle(AgentRole::Worker);
        assert!(bundle.contains(&Capability::TasksCreate));
        assert!(bundle.contains(&Capability::CoordinationWait));
        assert!(!bundle.contains(&Capability::TasksAssign));
        assert!(!bundle.contains(&Capability::MemoriesUpdate));
        assert!(!bundle.contains(&Capability::AgentsRotateToken));
    }

    #[test]
    fn manager_bundle_is_worker_bundle_plus_supervisory_verbs() {
        let worker = agent_role_bundle(AgentRole::Worker);
        let manager = agent_role_bundle(AgentRole::Manager);
        assert!(
            worker.is_subset(&manager),
            "manager must be a superset of worker"
        );
        assert!(manager.contains(&Capability::TasksAssign));
        assert!(manager.contains(&Capability::MemoriesUpdate));
        // Rotate-token is deliberately in NEITHER agent bundle — an
        // agent must never rotate a peer's (or its own) bearer.
        assert!(!manager.contains(&Capability::AgentsRotateToken));
    }

    #[test]
    fn viewer_bundle_is_read_only() {
        let viewer = project_role_bundle(ProjectRole::Viewer);
        assert!(viewer.contains(&Capability::TasksView));
        assert!(!viewer.contains(&Capability::TasksCreate));
        assert!(!viewer.contains(&Capability::AgentsRegister));
    }

    #[test]
    fn operator_bundle_is_viewer_bundle_plus_write_surfaces() {
        let viewer = project_role_bundle(ProjectRole::Viewer);
        let operator = project_role_bundle(ProjectRole::Operator);
        assert!(
            viewer.is_subset(&operator),
            "operator must be a superset of viewer"
        );
        assert!(operator.contains(&Capability::AgentsRegister));
        assert!(operator.contains(&Capability::AgentsRotateToken));
        assert!(operator.contains(&Capability::TasksAssign));
        assert!(operator.contains(&Capability::SystemConfigWrite));
        assert!(operator.contains(&Capability::RagRebuild));
    }
}
