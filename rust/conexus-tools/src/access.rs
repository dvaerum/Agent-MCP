//! Port of `agent_mcp/tools/access.py` -- role-filtered `tools/list`
//! (Phase E1 PR C).
//!
//! Python derives a per-tool visibility tier from the LIVE
//! authorization gate (`_derive_access_level`) rather than hand-
//! maintaining a classification table, closing a real historical drift
//! bug: a capability-gated tool shipped without a hand-synced
//! `visibility=` kwarg used to leak into every worker's/anonymous
//! caller's `tools/list` even though its cap gate would reject them.
//! This module ports that same derive-don't-hand-maintain philosophy,
//! made structurally stronger by this migration's own design: Python
//! needs a `requires.verify(implementation)` cross-check because a
//! tool's authorization lives in TWO places (a decorator stamp + the
//! `register_tool(requires=...)` kwarg) that can drift; `Tool::REQUIRED`
//! is the ONE declaration site here, so there is no second place for a
//! tier to disagree with in the first place.
//!
//! **Re-derivation, not a literal port**: Python's `_derive_access_
//! level`/`is_visible_to_role` also carry a `"manager"` tier and a
//! matching `role == "manager"` branch -- both confirmed UNREACHABLE
//! in the real, current call graph. `catalog_role()` (the ONLY
//! function that ever supplies `role` to `is_visible_to_role`,
//! confirmed by reading every real call site in `agent_mcp/`) collapses
//! a manager agent bearer to `"worker"`, never `"manager"` -- so a tool
//! whose capability derives to Python's "manager" tier is, in the code
//! that actually runs, invisible to every role except admin: IDENTICAL
//! to "operator" tier. [`AccessTier`] has no separate Manager variant;
//! a manager-bundle-only capability derives directly to `Operator`.

use conexus_auth::{Requirement, ToolDescriptor};
use conexus_core::capability::{agent_role_bundle, AgentRole, Capability};
use conexus_core::principal::CatalogRole;
use conexus_db::project_settings_repository;
use rusqlite::Connection;

/// A tool's `tools/list` visibility tier. Port of Python's access-level
/// strings (`"operator"`, `"worker"`, `"any"`,
/// `"worker-if-toggled:<keys>"`) -- see module doc for why there is no
/// `Manager` variant.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AccessTier {
    Operator,
    Worker,
    Any,
    WorkerIfToggled(&'static [&'static str], bool),
}

/// Deliberate, hand-reviewed tighten-only overrides -- Python's
/// `visibility=` kwarg, ported ONLY where it changes the outcome (it
/// genuinely restricts a tier the live gate would otherwise admit, or
/// supplies the sole signal for a Predicate-gated tool that isn't
/// already `"any"`). Adding an entry here IS the review Python's own
/// kwarg mechanism performed at registration time -- every entry below
/// traces to a real Python `visibility=...` kwarg, confirmed by
/// reading `agent_mcp/tools/*.py` directly, not assumed.
///
/// Every OTHER Predicate-gated tool in this catalogue (`ask_project_
/// rag`, `wait_for_events`, `fetch_events_since`, `add_task_comment`/
/// `edit_task_comment`/`delete_task_comment`, `view_file_metadata`,
/// `validate_context_consistency`, `create_project_context`/
/// `update_project_context`/`bulk_update_project_context`/
/// `delete_project_context`, `check_file_status`/`update_file_status`)
/// was confirmed to carry NO `visibility=` kwarg in Python -- all
/// default to `Any`, which needs no entry here.
///
/// NOT included (verified redundant): `update_task` (`Cap("tasks.
/// assign")`, a manager-bundle-only capability that already derives to
/// the equivalent-of-`Operator` tier per this module's own collapse)
/// and `delete_task` (`Cap("tasks.delete")`, in neither agent bundle,
/// already `Operator` by capability alone). Both carry a
/// `visibility="operator"` kwarg in Python that merely echoes the
/// derived tier -- exactly the "19 hand-synced kwargs that merely
/// echoed a derivable tier" class `access.py`'s own docstring says was
/// deleted elsewhere; no reason to introduce a fresh echo here.
const TIER_OVERRIDES: &[(&str, AccessTier)] = &[
    // Predicate-gated (an OR of two capability families -- no single
    // Cap to derive a tier from); Python: `visibility="worker"`.
    ("view_agents", AccessTier::Worker),
    // Cap("tasks.create")/Cap("tasks.update") both derive to `Worker`
    // naturally (both caps ARE in the worker bundle) -- Python
    // deliberately tightens both to operator-only ("admin-
    // orchestration surfaces", access.py's own docstring); the
    // override is load-bearing here, not an echo.
    ("create_task", AccessTier::Operator),
    ("bulk_task_operations", AccessTier::Operator),
];

/// Map a required capability to its visibility tier. Port of
/// `_visibility_for_capability`, collapsed to two outcomes per this
/// module's own doc on the dead "manager" tier.
fn tier_for_capability(cap: Capability) -> AccessTier {
    if agent_role_bundle(AgentRole::Worker).contains(&cap) {
        AccessTier::Worker
    } else {
        AccessTier::Operator
    }
}

/// The `tools/list` visibility tier for `descriptor`. Port of
/// `_derive_access_level`.
pub fn access_tier(descriptor: &ToolDescriptor) -> AccessTier {
    if let Some((_, tier)) = TIER_OVERRIDES
        .iter()
        .find(|(name, _)| *name == descriptor.name)
    {
        return *tier;
    }
    match &descriptor.required {
        Requirement::Cap { cap, .. } => tier_for_capability(*cap),
        Requirement::Policy { keys, default } => AccessTier::WorkerIfToggled(keys, *default),
        Requirement::Predicate { .. } | Requirement::Public => AccessTier::Any,
    }
}

/// True iff `role` should see a tool at `tier` in `tools/list`. Port
/// of `is_visible_to_role`, narrowed to the 3 reachable roles (see
/// module doc for why "manager" never appears). `conn` resolves a
/// `WorkerIfToggled` key's live `project_settings` override, falling
/// back to the tier's own carried default (the SAME default the
/// call-time `Requirement::Policy` gate uses -- one source, not
/// Python's separate `_TOGGLE_DEFAULTS` table, which can't drift from
/// the call-time gate here the way two independently-maintained
/// Python default sources theoretically could).
pub fn is_visible_to_role(tier: AccessTier, role: CatalogRole, conn: &Connection) -> bool {
    if role == CatalogRole::Admin {
        return true;
    }
    match tier {
        AccessTier::Operator => false,
        AccessTier::Worker => role == CatalogRole::Worker,
        AccessTier::Any => true,
        AccessTier::WorkerIfToggled(keys, default) => {
            if role != CatalogRole::Worker {
                return false;
            }
            keys.iter()
                .any(|k| project_settings_repository::get_bool(conn, k, default))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_core::capability::Capability;
    use conexus_db::schema::init_schema;

    fn test_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        conn
    }

    fn cap_descriptor(name: &'static str, cap: Capability) -> ToolDescriptor {
        ToolDescriptor {
            name,
            description: "d",
            required: Requirement::Cap { cap, reason: None },
            schema: "{}",
            call: |_, _, _, _, _| Box::pin(async { unreachable!() }),
        }
    }

    // -- access_tier ----------------------------------------------------

    #[test]
    fn a_worker_bundle_capability_derives_to_worker_tier() {
        let d = cap_descriptor("view_tasks", Capability::TasksView);
        assert_eq!(access_tier(&d), AccessTier::Worker);
    }

    #[test]
    fn an_operator_only_capability_derives_to_operator_tier() {
        let d = cap_descriptor("view_status", Capability::SystemConfigWrite);
        assert_eq!(access_tier(&d), AccessTier::Operator);
    }

    #[test]
    fn a_manager_bundle_only_capability_collapses_to_operator_tier() {
        // tasks.assign is manager-bundle-only, not worker-bundle -- per
        // this module's own doc, Python's "manager" tier is
        // unreachable and behaves identically to "operator" here.
        let d = cap_descriptor("update_task", Capability::TasksAssign);
        assert_eq!(access_tier(&d), AccessTier::Operator);
    }

    #[test]
    fn a_policy_requirement_derives_to_worker_if_toggled_with_its_own_default() {
        let d = ToolDescriptor {
            name: "assign_task",
            description: "d",
            required: Requirement::Policy {
                keys: &["config_allow_worker_self_assign"],
                default: true,
            },
            schema: "{}",
            call: |_, _, _, _, _| Box::pin(async { unreachable!() }),
        };
        assert_eq!(
            access_tier(&d),
            AccessTier::WorkerIfToggled(&["config_allow_worker_self_assign"], true)
        );
    }

    #[test]
    fn a_predicate_requirement_with_no_override_defaults_to_any() {
        let d = ToolDescriptor {
            name: "wait_for_events",
            description: "d",
            required: Requirement::Predicate {
                check: |_| true,
                reason: "r",
            },
            schema: "{}",
            call: |_, _, _, _, _| Box::pin(async { unreachable!() }),
        };
        assert_eq!(access_tier(&d), AccessTier::Any);
    }

    #[test]
    fn a_public_requirement_defaults_to_any() {
        let d = ToolDescriptor {
            name: "test",
            description: "d",
            required: Requirement::Public,
            schema: "{}",
            call: |_, _, _, _, _| Box::pin(async { unreachable!() }),
        };
        assert_eq!(access_tier(&d), AccessTier::Any);
    }

    #[test]
    fn the_view_agents_override_forces_worker_tier() {
        let d = ToolDescriptor {
            name: "view_agents",
            description: "d",
            required: Requirement::Predicate {
                check: |_| true,
                reason: "r",
            },
            schema: "{}",
            call: |_, _, _, _, _| Box::pin(async { unreachable!() }),
        };
        assert_eq!(access_tier(&d), AccessTier::Worker);
    }

    #[test]
    fn the_create_task_override_tightens_a_worker_bundle_cap_to_operator() {
        let d = cap_descriptor("create_task", Capability::TasksCreate);
        // Without the override this would derive to Worker (tasks.create
        // IS in the worker bundle) -- the override is load-bearing.
        assert_eq!(access_tier(&d), AccessTier::Operator);
    }

    #[test]
    fn the_bulk_task_operations_override_tightens_a_worker_bundle_cap_to_operator() {
        let d = cap_descriptor("bulk_task_operations", Capability::TasksUpdate);
        assert_eq!(access_tier(&d), AccessTier::Operator);
    }

    // -- is_visible_to_role -----------------------------------------------

    #[test]
    fn admin_sees_every_tier_unconditionally() {
        let conn = test_conn();
        for tier in [
            AccessTier::Operator,
            AccessTier::Worker,
            AccessTier::Any,
            AccessTier::WorkerIfToggled(&["some_key"], false),
        ] {
            assert!(is_visible_to_role(tier, CatalogRole::Admin, &conn));
        }
    }

    #[test]
    fn operator_tier_is_hidden_from_worker_and_anonymous() {
        let conn = test_conn();
        assert!(!is_visible_to_role(
            AccessTier::Operator,
            CatalogRole::Worker,
            &conn
        ));
        assert!(!is_visible_to_role(
            AccessTier::Operator,
            CatalogRole::Anonymous,
            &conn
        ));
    }

    #[test]
    fn worker_tier_is_visible_to_worker_but_not_anonymous() {
        let conn = test_conn();
        assert!(is_visible_to_role(
            AccessTier::Worker,
            CatalogRole::Worker,
            &conn
        ));
        assert!(!is_visible_to_role(
            AccessTier::Worker,
            CatalogRole::Anonymous,
            &conn
        ));
    }

    #[test]
    fn any_tier_is_visible_to_everyone_including_anonymous() {
        let conn = test_conn();
        assert!(is_visible_to_role(
            AccessTier::Any,
            CatalogRole::Anonymous,
            &conn
        ));
    }

    #[test]
    fn worker_if_toggled_is_never_visible_to_anonymous_regardless_of_toggle() {
        let conn = test_conn();
        assert!(!is_visible_to_role(
            AccessTier::WorkerIfToggled(&["config_allow_worker_to_worker"], true),
            CatalogRole::Anonymous,
            &conn
        ));
    }

    #[test]
    fn worker_if_toggled_uses_the_carried_default_when_no_row_exists() {
        let conn = test_conn();
        assert!(is_visible_to_role(
            AccessTier::WorkerIfToggled(&["config_allow_worker_to_worker"], true),
            CatalogRole::Worker,
            &conn
        ));
        assert!(!is_visible_to_role(
            AccessTier::WorkerIfToggled(&["config_allow_worker_to_worker"], false),
            CatalogRole::Worker,
            &conn
        ));
    }

    #[test]
    fn worker_if_toggled_reads_a_real_project_settings_override() {
        let conn = test_conn();
        project_settings_repository::upsert(
            &conn,
            "config_allow_worker_to_worker",
            "false",
            None,
            false,
            "operator",
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        assert!(!is_visible_to_role(
            AccessTier::WorkerIfToggled(&["config_allow_worker_to_worker"], true),
            CatalogRole::Worker,
            &conn
        ));
    }

    #[test]
    fn worker_if_toggled_any_truthy_key_is_enough() {
        let conn = test_conn();
        project_settings_repository::upsert(
            &conn,
            "config_allow_worker_self_assign",
            "false",
            None,
            false,
            "operator",
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        // config_allow_worker_create_unassigned has no row -- falls back
        // to its own `true` default, so the OR still admits.
        assert!(is_visible_to_role(
            AccessTier::WorkerIfToggled(
                &[
                    "config_allow_worker_self_assign",
                    "config_allow_worker_create_unassigned"
                ],
                true
            ),
            CatalogRole::Worker,
            &conn
        ));
    }
}
