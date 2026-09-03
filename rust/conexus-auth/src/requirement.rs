//! `Requirement` — what a tool requires of the calling Principal.
//!
//! Faithful port of the `Cap`/`Policy`/`Predicate`/`PUBLIC` taxonomy in
//! `agent_mcp/core/authorize.py`'s `ToolRequirement` hierarchy — an
//! already-existing Python design, not new Rust-side invention (see
//! that module's own comment: "registering a tool without stating its
//! authorization is impossible... a silent lie is an ImportError, not
//! a runtime surprise").
//!
//! One deliberate simplification the Rust port makes over Python:
//! Python needs the `ToolRequirement.verify(impl)` cross-check because
//! a tool's authorization lives in TWO disconnected places — the
//! `@requires_capability`/`@requires_policy`/`@requires_predicate`
//! decorator on the impl function, and the separate `requires=`
//! keyword at the `register_tool(...)` call site — and those two can
//! drift. Here, [`crate::tool::Tool::REQUIRED`] is the ONLY place a
//! tool's requirement is stated; there is no second site to drift
//! against, so the verify-cross-check machinery has no Rust
//! equivalent to port. What's left — the actual gate evaluation
//! (`check_capability_gate`/`check_policy_gate`/`check_predicate_gate`)
//! — is ported here as [`Requirement::check`], unified into one
//! function via the enum's `match` instead of three separate Python
//! functions each keyed off its own decorator's stamped attributes.

use conexus_core::capability::Capability;
use conexus_core::principal::{is_operator_tier, Principal, PrincipalKind};
use std::fmt;

/// Raised by [`Requirement::check`] when the caller's principal fails
/// the requirement. Port of `agent_mcp.core.authorize.AuthRejected` —
/// `reason` is short, user-facing text that reaches agent transcripts
/// / REST error bodies verbatim, so it must never carry internal
/// detail (matches [`conexus_core::tool_result::ToolResult::Failed`]'s
/// same discipline).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuthRejected {
    pub reason: String,
}

impl AuthRejected {
    pub fn new(reason: impl Into<String>) -> Self {
        Self {
            reason: reason.into(),
        }
    }
}

impl fmt::Display for AuthRejected {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.reason)
    }
}

impl std::error::Error for AuthRejected {}

/// Resolves a worker-toggle config key to its explicit per-project
/// value, if the project has one set. `None` means "no explicit
/// override — fall back to the [`Requirement::Policy`]'s own
/// `default`", mirroring Python's `_get_config_bool(key, default)`
/// split between an explicit `project_settings` row and a schema-
/// registry default.
///
/// Deliberately a trait rather than a concrete `conexus-db`-backed
/// type: no real `Policy`-gated tool exists yet (that's Phase D+), so
/// wiring this to the actual `project_settings_repository` +
/// settings-schema-registry default table is deferred until a real
/// tool needs it — the same "explicit input, not a hidden lookup"
/// discipline as `conexus-auth::capabilities::resolve_capabilities`'s
/// `router_conn: Option<&Connection>`.
pub trait PolicySource {
    fn get_bool(&self, key: &str) -> Option<bool>;
}

/// A [`PolicySource`] with no overrides at all — every key falls
/// through to the [`Requirement::Policy`]'s own `default`. The right
/// choice for any caller with no project-settings row in hand (mirrors
/// Python's behavior when `project_settings` has no row for the key).
pub struct NoPolicyOverrides;

impl PolicySource for NoPolicyOverrides {
    fn get_bool(&self, _key: &str) -> Option<bool> {
        None
    }
}

/// What a tool requires of the calling Principal before it may run.
/// See the module doc for the Python provenance of this taxonomy.
#[derive(Debug, Clone, Copy)]
pub enum Requirement {
    /// Gated on exactly one capability. `reason`, when set, replaces
    /// the generic denial text — Python's `requires_capability(cap,
    /// *, reason=...)` kwarg, preserved here since several tools carry
    /// a hand-written, actionable denial message worth keeping (Phase
    /// 2 Finding A).
    Cap {
        cap: Capability,
        reason: Option<&'static str>,
    },
    /// Gated on the worker-toggle policy: an agent-bearer caller
    /// passes iff at least one of `keys` resolves truthy (explicit
    /// project override via a [`PolicySource`], else `default`).
    /// Operator-tier callers always bypass this gate (see
    /// [`is_operator_tier`]).
    Policy {
        keys: &'static [&'static str],
        default: bool,
    },
    /// Gated on an arbitrary boolean predicate over the (possibly
    /// absent) Principal, with a mandatory human-readable denial
    /// reason — Python's `requires_predicate(predicate, reason)`.
    Predicate {
        check: fn(Option<&Principal>) -> bool,
        reason: &'static str,
    },
    /// No gate at all. The ONLY way to declare an ungated tool —
    /// deliberately named so it greps, and meant to be cross-checked
    /// against a hand-reviewed allowlist wherever `all_tools()` is
    /// walked (see `crate::tool`'s arch test).
    Public,
}

/// Hand-written rather than derived: `Predicate`'s `check` field is a
/// function pointer, and comparing function pointers for equality
/// (`#[derive(PartialEq)]` would do this field-by-field) is
/// unreliable — the compiler may merge identical function bodies or
/// give the same function different addresses across codegen units
/// (see `unpredictable_function_pointer_comparisons`). Two `Predicate`
/// requirements are considered equal by their `reason` text instead,
/// which is what every real comparison in this crate (test
/// assertions, the PUBLIC-allowlist arch test) actually needs — no
/// caller compares two distinct predicates expecting `check`-pointer
/// identity to matter.
impl PartialEq for Requirement {
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            (
                Requirement::Cap {
                    cap: c1,
                    reason: r1,
                },
                Requirement::Cap {
                    cap: c2,
                    reason: r2,
                },
            ) => c1 == c2 && r1 == r2,
            (
                Requirement::Policy {
                    keys: k1,
                    default: d1,
                },
                Requirement::Policy {
                    keys: k2,
                    default: d2,
                },
            ) => k1 == k2 && d1 == d2,
            (
                Requirement::Predicate { reason: r1, .. },
                Requirement::Predicate { reason: r2, .. },
            ) => r1 == r2,
            (Requirement::Public, Requirement::Public) => true,
            _ => false,
        }
    }
}

impl Eq for Requirement {}

impl Requirement {
    /// Evaluate this requirement against `principal`. Mirrors
    /// `check_capability_gate`/`check_policy_gate`/
    /// `check_predicate_gate` from the Python source, unified via
    /// `match` over the closed `Requirement` enum instead of three
    /// separate functions each reading its own decorator's stamped
    /// attribute.
    pub fn check(
        &self,
        principal: Option<&Principal>,
        policy_source: &dyn PolicySource,
    ) -> Result<(), AuthRejected> {
        match *self {
            Requirement::Cap { cap, reason } => {
                let Some(p) = principal else {
                    return Err(AuthRejected::new(
                        reason.unwrap_or("Unauthorized: Valid token required"),
                    ));
                };
                if p.has_capability(cap) {
                    Ok(())
                } else {
                    Err(AuthRejected::new(
                        reason.map(str::to_string).unwrap_or_else(|| {
                            format!("Unauthorized: capability {:?} required", cap.as_str())
                        }),
                    ))
                }
            }
            Requirement::Policy { keys, default } => {
                let Some(p) = principal else {
                    return Err(AuthRejected::new("Unauthorized: Valid token required"));
                };
                // Operator-tier callers (and the legacy agent_id ==
                // "admin" label) bypass the toggle check entirely.
                if is_operator_tier(p) {
                    return Ok(());
                }
                if p.kind != PrincipalKind::AgentBearer || p.agent_id.is_none() {
                    return Err(AuthRejected::new("Unauthorized: Valid token required"));
                }
                for key in keys {
                    if policy_source.get_bool(key).unwrap_or(default) {
                        return Ok(());
                    }
                }
                Err(AuthRejected::new(format!(
                    "Unauthorized: worker access denied by project policy (all of: {} are off). \
                     Ask admin to enable one in dashboard Settings.",
                    keys.join(", ")
                )))
            }
            Requirement::Predicate { check, reason } => {
                if check(principal) {
                    Ok(())
                } else {
                    Err(AuthRejected::new(reason))
                }
            }
            Requirement::Public => Ok(()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_core::capability::Capabilities;

    fn agent_bearer(agent_id: Option<&str>, caps: Capabilities) -> Principal {
        Principal {
            kind: PrincipalKind::AgentBearer,
            user_id: None,
            agent_id: agent_id.map(str::to_string),
            project_name: None,
            project_role: None,
            agent_role: None,
            can_wake_loop: false,
            source_token: None,
            capabilities: caps,
        }
    }

    fn operator(caps: Capabilities) -> Principal {
        Principal {
            kind: PrincipalKind::OperatorSession,
            user_id: Some("op-1".to_string()),
            agent_id: None,
            project_name: None,
            project_role: Some(conexus_core::capability::ProjectRole::Operator),
            agent_role: None,
            can_wake_loop: false,
            source_token: None,
            capabilities: caps,
        }
    }

    // ── Cap ─────────────────────────────────────────────────────────

    #[test]
    fn cap_admits_a_principal_carrying_it() {
        let req = Requirement::Cap {
            cap: Capability::TasksView,
            reason: None,
        };
        let p = agent_bearer(Some("a1"), Capabilities::from_iter([Capability::TasksView]));
        assert_eq!(req.check(Some(&p), &NoPolicyOverrides), Ok(()));
    }

    #[test]
    fn cap_denies_a_missing_principal_with_generic_text() {
        let req = Requirement::Cap {
            cap: Capability::TasksView,
            reason: None,
        };
        let err = req.check(None, &NoPolicyOverrides).unwrap_err();
        assert_eq!(err.reason, "Unauthorized: Valid token required");
    }

    #[test]
    fn cap_denies_a_principal_missing_the_cap_with_generic_text() {
        let req = Requirement::Cap {
            cap: Capability::TasksAssign,
            reason: None,
        };
        let p = agent_bearer(Some("a1"), Capabilities::from_iter([Capability::TasksView]));
        let err = req.check(Some(&p), &NoPolicyOverrides).unwrap_err();
        assert_eq!(
            err.reason,
            "Unauthorized: capability \"tasks.assign\" required"
        );
    }

    #[test]
    fn cap_custom_reason_overrides_both_the_missing_principal_and_missing_cap_text() {
        let req = Requirement::Cap {
            cap: Capability::TasksAssign,
            reason: Some("ask an operator to assign this for you"),
        };
        let missing_principal_err = req.check(None, &NoPolicyOverrides).unwrap_err();
        assert_eq!(
            missing_principal_err.reason,
            "ask an operator to assign this for you"
        );

        let p = agent_bearer(Some("a1"), Capabilities::from_iter([]));
        let missing_cap_err = req.check(Some(&p), &NoPolicyOverrides).unwrap_err();
        assert_eq!(
            missing_cap_err.reason,
            "ask an operator to assign this for you"
        );
    }

    // ── Policy ──────────────────────────────────────────────────────

    const WORKER_TOGGLE: Requirement = Requirement::Policy {
        keys: &["config_allow_worker_x"],
        default: false,
    };

    #[test]
    fn policy_denies_a_missing_principal() {
        let err = WORKER_TOGGLE.check(None, &NoPolicyOverrides).unwrap_err();
        assert_eq!(err.reason, "Unauthorized: Valid token required");
    }

    #[test]
    fn policy_operator_tier_bypasses_the_toggle_entirely() {
        let p = operator(Capabilities::from_iter([Capability::SystemConfigWrite]));
        // default=false and no override -- would deny a worker, but an
        // operator-tier caller bypasses the check outright.
        assert_eq!(WORKER_TOGGLE.check(Some(&p), &NoPolicyOverrides), Ok(()));
    }

    #[test]
    fn policy_legacy_admin_agent_id_also_bypasses() {
        let p = agent_bearer(Some("admin"), Capabilities::from_iter([]));
        assert_eq!(WORKER_TOGGLE.check(Some(&p), &NoPolicyOverrides), Ok(()));
    }

    #[test]
    fn policy_non_agent_bearer_non_operator_is_denied_valid_token_required() {
        // An operator-session caller with no operator-tier marker at
        // all (viewer role, say) is neither an operator bypass nor an
        // agent bearer.
        let p = operator(Capabilities::from_iter([Capability::TasksView]));
        let err = WORKER_TOGGLE
            .check(Some(&p), &NoPolicyOverrides)
            .unwrap_err();
        assert_eq!(err.reason, "Unauthorized: Valid token required");
    }

    #[test]
    fn policy_agent_bearer_with_no_agent_id_is_denied() {
        let p = agent_bearer(None, Capabilities::from_iter([]));
        let err = WORKER_TOGGLE
            .check(Some(&p), &NoPolicyOverrides)
            .unwrap_err();
        assert_eq!(err.reason, "Unauthorized: Valid token required");
    }

    #[test]
    fn policy_worker_with_default_false_and_no_override_is_denied_with_toggle_text() {
        let p = agent_bearer(Some("w1"), Capabilities::from_iter([]));
        let err = WORKER_TOGGLE
            .check(Some(&p), &NoPolicyOverrides)
            .unwrap_err();
        assert!(err.reason.contains("config_allow_worker_x"));
        assert!(err.reason.contains("dashboard Settings"));
    }

    #[test]
    fn policy_worker_admitted_when_default_is_true() {
        const ALWAYS_ON: Requirement = Requirement::Policy {
            keys: &["config_allow_worker_x"],
            default: true,
        };
        let p = agent_bearer(Some("w1"), Capabilities::from_iter([]));
        assert_eq!(ALWAYS_ON.check(Some(&p), &NoPolicyOverrides), Ok(()));
    }

    struct FakePolicySource(bool);
    impl PolicySource for FakePolicySource {
        fn get_bool(&self, _key: &str) -> Option<bool> {
            Some(self.0)
        }
    }

    #[test]
    fn policy_explicit_override_wins_over_default() {
        let p = agent_bearer(Some("w1"), Capabilities::from_iter([]));
        // default=false but the project explicitly turned it on.
        assert_eq!(
            WORKER_TOGGLE.check(Some(&p), &FakePolicySource(true)),
            Ok(())
        );
    }

    #[test]
    fn policy_admits_if_any_of_several_keys_resolves_truthy() {
        const MULTI_KEY: Requirement = Requirement::Policy {
            keys: &["config_a", "config_b"],
            default: false,
        };
        struct SecondKeyOnly;
        impl PolicySource for SecondKeyOnly {
            fn get_bool(&self, key: &str) -> Option<bool> {
                Some(key == "config_b")
            }
        }
        let p = agent_bearer(Some("w1"), Capabilities::from_iter([]));
        assert_eq!(MULTI_KEY.check(Some(&p), &SecondKeyOnly), Ok(()));
    }

    // ── Predicate ───────────────────────────────────────────────────

    #[test]
    fn predicate_admits_when_the_check_passes() {
        let req = Requirement::Predicate {
            check: |p| p.is_some(),
            reason: "must be authenticated",
        };
        let p = agent_bearer(Some("a1"), Capabilities::from_iter([]));
        assert_eq!(req.check(Some(&p), &NoPolicyOverrides), Ok(()));
    }

    #[test]
    fn predicate_denies_with_its_reason_when_the_check_fails() {
        let req = Requirement::Predicate {
            check: |p| p.is_some(),
            reason: "must be authenticated",
        };
        let err = req.check(None, &NoPolicyOverrides).unwrap_err();
        assert_eq!(err.reason, "must be authenticated");
    }

    #[test]
    fn predicate_receives_none_when_there_is_no_principal() {
        // Mirrors Python's PredicateFn accepting Optional[Principal] --
        // the predicate itself decides what an absent principal means,
        // rather than Requirement short-circuiting on its behalf.
        let req = Requirement::Predicate {
            check: |p| p.is_none(),
            reason: "unreachable",
        };
        assert_eq!(req.check(None, &NoPolicyOverrides), Ok(()));
    }

    // ── Public ──────────────────────────────────────────────────────

    #[test]
    fn public_admits_with_no_principal_at_all() {
        assert_eq!(Requirement::Public.check(None, &NoPolicyOverrides), Ok(()));
    }
}
