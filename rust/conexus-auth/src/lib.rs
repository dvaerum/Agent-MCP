//! CoNexus Principal-resolution / tool-authorization layer (Phase C/D1).
//!
//! Pieces: [`capabilities::resolve_capabilities`] (the DB-backed
//! group-capability overlay that `conexus_core::capability`'s own
//! module doc deferred here), the `Tool`/`Requirement`/`dispatch()`
//! machinery in [`requirement`]/[`tool`] (porting
//! `agent_mcp/core/authorize.py` — the registration LIST itself,
//! Python's `all_tools()`/`register_tool(...)` call sites, lives one
//! layer up in `conexus-tools`, not here), and [`forwarding_header`]
//! (the signed `X-Agent-MCP-Forwarded-Operator` header the router
//! attaches when proxying a cookie-authenticated request to a
//! per-project backend). See
//! `/home/dennis/.claude/plans/prancy-napping-pie.md`.

pub mod capabilities;
pub mod forwarding_header;
pub mod requirement;
pub mod tool;
pub mod wake_loop_eligibility;

pub use capabilities::{resolve_capabilities, ResolveCapabilitiesInput};
pub use forwarding_header::{
    sign as sign_forwarding_header, verify as verify_forwarding_header, ForwardedRole,
};
pub use requirement::{AuthRejected, NoPolicyOverrides, PolicySource, Requirement};
pub use tool::{
    dispatch, BoxFuture, ProgressSink, Tool, ToolCallContext, ToolCallFn, ToolDescriptor,
};
pub use wake_loop_eligibility::resolve_can_wake_loop;
