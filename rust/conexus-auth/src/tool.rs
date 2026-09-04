//! `Tool` trait + hand-written `all_tools()` registration list.
//!
//! Port of the OTHER half of `agent_mcp/core/authorize.py` +
//! `agent_mcp/tools/registry.py::register_tool`: a `Tool` implementation
//! declares its own name and [`Requirement`] as associated consts, and
//! [`all_tools`] is a single, hand-maintained, greppable list — NOT a
//! proc-macro/`inventory::submit!` auto-collection scheme. See the
//! migration plan's Target Architecture section for why: a single,
//! human-reviewed registration site is a value this project holds, not
//! an accident to "improve away".
//!
//! Mechanism note: `const` items aren't dispatchable through `dyn
//! Tool`, so `all_tools()` can't return `Vec<&dyn Tool>` the way a more
//! naive design might reach for. [`ToolDescriptor`] is the type-erased
//! shape instead — `name`/`required` copied out of the const, `call` a
//! plain function pointer to the monomorphized impl. `ToolDescriptor::
//! of::<T>()` is `const fn`, so the whole registry can be a `static`
//! array built at compile time, same shape as a hand-written sequence
//! of Python's `register_tool(...)` calls, just collected into a slice
//! instead of executed as side-effecting calls at import time.

use crate::requirement::{PolicySource, Requirement};
use conexus_core::principal::Principal;
use conexus_core::tool_result::ToolResult;
use serde_json::Value;

/// A tool entry point. Real tool modules (Phase D1+) each define a
/// zero-sized type implementing this trait; `NAME` and `REQUIRED` are
/// the single declaration site for the tool's identity and
/// authorization story — there is no second, decorator-level place for
/// them to drift against (see `crate::requirement`'s module doc).
pub trait Tool {
    const NAME: &'static str;
    const REQUIRED: Requirement;

    /// The tool's one-line description, verbatim from the Python
    /// `register_tool(..., description=...)` call site — part of the
    /// `tools/list` surface a client sees.
    const DESCRIPTION: &'static str;

    /// The tool's JSON Schema for its `arguments`, as literal JSON
    /// text — mirrors the exact `input_schema` dict shape Python's
    /// `register_tool(...)` call sites pass. A `&'static str` rather
    /// than a `serde_json::Value` because `Value` isn't `const`-
    /// constructible; parse it via [`ToolDescriptor::parsed_schema`]
    /// when a real `Value` is needed (e.g. building a `tools/list`
    /// response).
    const SCHEMA: &'static str;

    /// Run the tool. Enforcement is the DISPATCHER's job (see
    /// [`dispatch`]), not this method's — a direct call here is
    /// intentionally ungated, the same way Python's raw impl function
    /// (before `@requires_*` wraps it) has no gate of its own. Every
    /// real call path must go through [`dispatch`] or an equivalent
    /// that checks `Self::REQUIRED` first.
    fn call(principal: Option<&Principal>, arguments: &Value) -> ToolResult;
}

/// The type-erased shape of a [`Tool`], suitable for a flat runtime
/// registry. See the module doc for why this exists instead of `dyn
/// Tool`.
pub struct ToolDescriptor {
    pub name: &'static str,
    pub description: &'static str,
    pub required: Requirement,
    pub schema: &'static str,
    pub call: fn(Option<&Principal>, &Value) -> ToolResult,
}

impl ToolDescriptor {
    pub const fn of<T: Tool>() -> Self {
        ToolDescriptor {
            name: T::NAME,
            description: T::DESCRIPTION,
            required: T::REQUIRED,
            schema: T::SCHEMA,
            call: T::call,
        }
    }

    /// Parse `schema` into a `serde_json::Value` — e.g. for a
    /// `tools/list` response. Panics on malformed JSON: a `Tool`
    /// impl's `SCHEMA` is developer-authored literal text, not
    /// untrusted runtime input, so a parse failure is a bug to catch
    /// via `every_registered_tools_schema_is_valid_json` (below) in
    /// CI, not a condition a real caller could ever trigger.
    pub fn parsed_schema(&self) -> serde_json::Value {
        serde_json::from_str(self.schema)
            .unwrap_or_else(|e| panic!("Tool {:?}'s SCHEMA is not valid JSON: {e}", self.name))
    }
}

/// The complete tool catalogue. One line per tool, hand-maintained and
/// greppable. Empty until Phase D1 starts porting real tool modules
/// (`agent_mcp/tools/*.py`) — this crate's job is the trait/enum/
/// registry scaffolding, not the tools themselves.
pub fn all_tools() -> &'static [ToolDescriptor] {
    &[]
}

/// Check `descriptor.required` against `principal`, THEN call the
/// tool. Denial becomes [`ToolResult::PermissionDenied`] rather than a
/// separate error type, matching how a real MCP/REST caller ultimately
/// renders it — the same "trait declares intent, dispatcher enforces
/// before calling" split the migration plan calls for, and the direct
/// analogue of `dispatch_tool_call`'s R20-F4 pre-schema-validation
/// gate (enforcement happens before the tool ever sees `arguments`).
pub fn dispatch(
    descriptor: &ToolDescriptor,
    principal: Option<&Principal>,
    policy_source: &dyn PolicySource,
    arguments: &Value,
) -> ToolResult {
    if let Err(rejected) = descriptor.required.check(principal, policy_source) {
        return ToolResult::PermissionDenied {
            reason: rejected.reason,
        };
    }
    (descriptor.call)(principal, arguments)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::requirement::NoPolicyOverrides;
    use conexus_core::capability::{Capabilities, Capability};
    use conexus_core::principal::PrincipalKind;
    use std::collections::BTreeSet;
    use std::sync::atomic::{AtomicBool, Ordering};

    static CALLED: AtomicBool = AtomicBool::new(false);

    struct EchoTool;
    impl Tool for EchoTool {
        const NAME: &'static str = "echo";
        const REQUIRED: Requirement = Requirement::Cap {
            cap: Capability::TasksView,
            reason: None,
        };
        const DESCRIPTION: &'static str = "Echoes its arguments back.";
        const SCHEMA: &'static str =
            r#"{"type":"object","properties":{},"additionalProperties":true}"#;
        fn call(_principal: Option<&Principal>, arguments: &Value) -> ToolResult {
            CALLED.store(true, Ordering::SeqCst);
            ToolResult::Ok {
                data: Some(arguments.clone()),
                message: None,
            }
        }
    }

    fn agent_bearer(caps: Capabilities) -> Principal {
        Principal {
            kind: PrincipalKind::AgentBearer,
            user_id: None,
            agent_id: Some("a1".to_string()),
            project_name: None,
            project_role: None,
            agent_role: None,
            can_wake_loop: false,
            source_token: None,
            capabilities: caps,
        }
    }

    #[test]
    fn tool_descriptor_of_captures_the_impls_const_declaration() {
        let d = ToolDescriptor::of::<EchoTool>();
        assert_eq!(d.name, "echo");
        assert_eq!(d.description, "Echoes its arguments back.");
        assert_eq!(
            d.required,
            Requirement::Cap {
                cap: Capability::TasksView,
                reason: None
            }
        );
        assert_eq!(
            d.schema,
            r#"{"type":"object","properties":{},"additionalProperties":true}"#
        );
    }

    #[test]
    fn parsed_schema_parses_the_literal_schema_text() {
        let d = ToolDescriptor::of::<EchoTool>();
        let parsed = d.parsed_schema();
        assert_eq!(parsed["type"], "object");
        assert_eq!(parsed["additionalProperties"], true);
    }

    #[test]
    #[should_panic(expected = "not valid JSON")]
    fn parsed_schema_panics_on_malformed_json() {
        struct BrokenSchemaTool;
        impl Tool for BrokenSchemaTool {
            const NAME: &'static str = "broken";
            const REQUIRED: Requirement = Requirement::Public;
            const DESCRIPTION: &'static str = "Has a malformed schema.";
            const SCHEMA: &'static str = "{not json";
            fn call(_: Option<&Principal>, _: &Value) -> ToolResult {
                unreachable!("not called by this test")
            }
        }
        ToolDescriptor::of::<BrokenSchemaTool>().parsed_schema();
    }

    // Both cases below share the `CALLED` static and MUST run in one
    // test function, not two -- split into separate `#[test]`s they'd
    // race under Rust's default parallel test execution (the exact
    // class of flake this workspace already fixed once in
    // `conexus-vec`'s `REGISTRATION_LOCK` tests: two tests mutating
    // shared global state can interleave and observe each other's
    // writes). Sequencing both assertions in one test makes the
    // ordering deterministic instead.
    #[test]
    fn dispatch_denies_before_calling_and_calls_the_tool_on_admission() {
        CALLED.store(false, Ordering::SeqCst);
        let d = ToolDescriptor::of::<EchoTool>();
        let denied_principal = agent_bearer(Capabilities::from_iter([])); // missing TasksView
        let denial = dispatch(
            &d,
            Some(&denied_principal),
            &NoPolicyOverrides,
            &Value::Null,
        );

        assert!(
            !CALLED.load(Ordering::SeqCst),
            "tool must not run on denial"
        );
        assert!(matches!(denial, ToolResult::PermissionDenied { .. }));

        let admitted_principal = agent_bearer(Capabilities::from_iter([Capability::TasksView]));
        let args = serde_json::json!({"x": 1});
        let admission = dispatch(&d, Some(&admitted_principal), &NoPolicyOverrides, &args);

        assert!(CALLED.load(Ordering::SeqCst));
        assert_eq!(
            admission,
            ToolResult::Ok {
                data: Some(args),
                message: None
            }
        );
    }

    // ── all_tools() / PUBLIC allowlist arch test ────────────────────
    //
    // Rust's compiler already makes "a tool with no stated
    // Requirement" impossible (REQUIRED has no default) -- the one
    // remaining thing Python's `tests/
    // test_arch_enforced_tool_capability_registration.py` checks that
    // the type system does NOT cover is "every Requirement::Public
    // tool is a REVIEWED, deliberate choice", since nothing stops a
    // careless `Tool` impl from picking `Public` when it shouldn't.
    // This is the first `test_arch_*` rewrite the plan calls for.

    /// Tools intentionally registered with `Requirement::Public` — a
    /// reviewed, justified allowlist. Adding a name here IS the
    /// security review this test exists to force.
    const PUBLIC_TOOL_ALLOWLIST: &[&str] = &[];

    #[test]
    fn public_tools_match_the_reviewed_allowlist() {
        let actual: BTreeSet<&str> = all_tools()
            .iter()
            .filter(|t| t.required == Requirement::Public)
            .map(|t| t.name)
            .collect();
        let expected: BTreeSet<&str> = PUBLIC_TOOL_ALLOWLIST.iter().copied().collect();
        assert_eq!(
            actual, expected,
            "a new Requirement::Public tool must be added to PUBLIC_TOOL_ALLOWLIST \
             (that addition IS the security review), or an allowlisted tool was \
             removed without updating this list"
        );
    }

    #[test]
    fn the_allowlist_check_actually_detects_an_unreviewed_public_tool() {
        // Proves the mechanism discriminates rather than trivially
        // passing on an empty catalogue -- same discipline as
        // conexus-vec's swappable fake entry points proving its
        // three-case degrade contract against fakes, not just the
        // real (always-present) extension.
        struct SneakyPublicTool;
        impl Tool for SneakyPublicTool {
            const NAME: &'static str = "sneaky";
            const REQUIRED: Requirement = Requirement::Public;
            const DESCRIPTION: &'static str = "Shouldn't be public.";
            const SCHEMA: &'static str = r#"{"type":"object"}"#;
            fn call(_: Option<&Principal>, _: &Value) -> ToolResult {
                unreachable!("not called by this test")
            }
        }
        let descriptors = [ToolDescriptor::of::<SneakyPublicTool>()];
        let actual: BTreeSet<&str> = descriptors
            .iter()
            .filter(|t| t.required == Requirement::Public)
            .map(|t| t.name)
            .collect();
        let allowlist: BTreeSet<&str> = BTreeSet::new();
        assert_ne!(
            actual, allowlist,
            "an unreviewed PUBLIC tool must make the allowlist comparison fail"
        );
    }

    #[test]
    fn every_registered_tools_schema_is_valid_json() {
        // Vacuous while all_tools() is still empty (Phase D1's real
        // tool ports haven't landed yet), but a real, non-tautological
        // check the moment they do -- `parsed_schema()` panics on
        // malformed JSON, so this test is what would actually catch a
        // typo'd SCHEMA literal in CI rather than at first runtime use.
        for descriptor in all_tools() {
            let _ = descriptor.parsed_schema();
        }
    }
}
