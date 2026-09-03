//! CoNexus Principal-resolution / tool-authorization layer (Phase C).
//!
//! Two pieces: [`capabilities::resolve_capabilities`] (the DB-backed
//! group-capability overlay that `conexus_core::capability`'s own
//! module doc deferred here), and the `Tool`/`Requirement`/
//! `all_tools()` machinery in [`requirement`]/[`tool`] (porting
//! `agent_mcp/core/authorize.py`). See
//! `/home/dennis/.claude/plans/prancy-napping-pie.md`.

pub mod capabilities;
pub mod requirement;
pub mod tool;

pub use capabilities::{resolve_capabilities, ResolveCapabilitiesInput};
pub use requirement::{AuthRejected, NoPolicyOverrides, PolicySource, Requirement};
pub use tool::{all_tools, dispatch, Tool, ToolDescriptor};
