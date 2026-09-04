//! The complete tool catalogue. One line per tool, hand-maintained and
//! greppable — see `conexus_auth::tool`'s module doc for why this is
//! a flat `static` slice rather than a proc-macro/`inventory::submit!`
//! auto-collection scheme. This is the same registration list Python
//! builds by executing every `agent_mcp/tools/*.py`'s
//! `register_*_tools()` call at import time; here it's a `const`
//! array built at compile time instead.

use conexus_auth::ToolDescriptor;

use crate::agent_communication_tools::{FetchEventsSinceTool, WaitForEventsTool};
use crate::assign_task_tools::{AssignTaskTool, CreateSelfTaskTool};
use crate::project_settings_tools::{
    DeleteProjectSettingsTool, UpdateProjectSettingsTool, ViewProjectSettingsTool,
};
use crate::rag_tools::AskProjectRagTool;
use crate::task_tools::{
    BulkTaskOperationsTool, CreateTaskTool, DeleteTaskTool, RequestAssistanceTool, SearchTasksTool,
    UpdateTaskStatusTool, UpdateTaskTool, ViewTasksTool,
};
use crate::utility_tools::TestTool;

// `ToolDescriptor::of` is `const fn`, so the whole registry is a
// compile-time `static` array (needs a named `static`, not an inline
// `&[...]` literal, since the array's elements aren't const-promotable
// through a non-const fn body).
static ALL_TOOLS: [ToolDescriptor; 17] = [
    ToolDescriptor::of::<ViewProjectSettingsTool>(),
    ToolDescriptor::of::<UpdateProjectSettingsTool>(),
    ToolDescriptor::of::<DeleteProjectSettingsTool>(),
    ToolDescriptor::of::<AskProjectRagTool>(),
    ToolDescriptor::of::<WaitForEventsTool>(),
    ToolDescriptor::of::<FetchEventsSinceTool>(),
    ToolDescriptor::of::<ViewTasksTool>(),
    ToolDescriptor::of::<SearchTasksTool>(),
    ToolDescriptor::of::<CreateTaskTool>(),
    ToolDescriptor::of::<UpdateTaskStatusTool>(),
    ToolDescriptor::of::<UpdateTaskTool>(),
    ToolDescriptor::of::<DeleteTaskTool>(),
    ToolDescriptor::of::<AssignTaskTool>(),
    ToolDescriptor::of::<CreateSelfTaskTool>(),
    ToolDescriptor::of::<RequestAssistanceTool>(),
    ToolDescriptor::of::<BulkTaskOperationsTool>(),
    ToolDescriptor::of::<TestTool>(),
];

pub fn all_tools() -> &'static [ToolDescriptor] {
    &ALL_TOOLS
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_auth::{Requirement, Tool};
    use conexus_core::principal::Principal;
    use conexus_core::tool_result::ToolResult;
    use rusqlite::Connection;
    use serde_json::Value;
    use std::collections::BTreeSet;
    use tokio::sync::Mutex as AsyncMutex;

    // ── all_tools() / PUBLIC allowlist arch test ────────────────────
    //
    // Moved here from conexus-auth's tool.rs when this crate became
    // the real home of `all_tools()` (Phase D1) -- the test only
    // means something colocated with a real registry, not a
    // permanently-empty placeholder. Rust's compiler already makes "a
    // tool with no stated Requirement" impossible (REQUIRED has no
    // default) -- this is the one remaining thing Python's `tests/
    // test_arch_enforced_tool_capability_registration.py` checks that
    // the type system does NOT cover: "every Requirement::Public tool
    // is a REVIEWED, deliberate choice", since nothing stops a
    // careless `Tool` impl from picking `Public` when it shouldn't.

    /// Tools intentionally registered with `Requirement::Public` — a
    /// reviewed, justified allowlist. Adding a name here IS the
    /// security review this test exists to force.
    ///
    /// `"test"` (Phase D5, `utility_tools.py`): a no-argument, no-
    /// side-effect tool that always returns the same static message
    /// ("Tool is working!") -- verifies the tool-calling mechanism
    /// itself, nothing project- or agent-specific. Reviewed: safe to
    /// leave unauthenticated, matching Python's own `PUBLIC` gate on
    /// this exact tool.
    const PUBLIC_TOOL_ALLOWLIST: &[&str] = &["test"];

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
        // passing -- same discipline as conexus-vec's swappable fake
        // entry points proving its degrade contract against fakes,
        // not just the always-Cap-gated real catalogue.
        struct SneakyPublicTool;
        impl Tool for SneakyPublicTool {
            const NAME: &'static str = "sneaky";
            const REQUIRED: Requirement = Requirement::Public;
            const DESCRIPTION: &'static str = "Shouldn't be public.";
            const SCHEMA: &'static str = r#"{"type":"object"}"#;
            fn call<'a>(
                _: Option<&'a Principal>,
                _: &'a Value,
                _: &'a AsyncMutex<Connection>,
                _: &'a str,
                _: &'a conexus_auth::ToolCallContext<'a>,
            ) -> conexus_auth::BoxFuture<'a, ToolResult> {
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
        // No longer vacuous now that all_tools() holds 3 real tools --
        // parsed_schema() panics on malformed JSON, so this is what
        // would actually catch a typo'd SCHEMA literal in CI.
        for descriptor in all_tools() {
            let _ = descriptor.parsed_schema();
        }
    }

    #[test]
    fn all_tools_names_are_unique() {
        let names: Vec<&str> = all_tools().iter().map(|t| t.name).collect();
        let unique: BTreeSet<&str> = names.iter().copied().collect();
        assert_eq!(
            names.len(),
            unique.len(),
            "duplicate tool name in all_tools() -- a real MCP client would only ever \
             see one of the two"
        );
    }

    #[test]
    fn all_tools_holds_the_project_settings_rag_event_and_task_tools() {
        let names: BTreeSet<&str> = all_tools().iter().map(|t| t.name).collect();
        assert_eq!(
            names,
            BTreeSet::from([
                "view_project_settings",
                "update_project_settings",
                "delete_project_settings",
                "ask_project_rag",
                "wait_for_events",
                "fetch_events_since",
                "view_tasks",
                "search_tasks",
                "create_task",
                "update_task_status",
                "update_task",
                "delete_task",
                "assign_task",
                "create_self_task",
                "request_assistance",
                "bulk_task_operations",
                "test",
            ])
        );
    }
}
