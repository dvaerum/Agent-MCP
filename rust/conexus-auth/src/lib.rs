//! CoNexus Principal-resolution / tool-authorization layer (Phase C).
//!
//! First piece: [`capabilities::resolve_capabilities`], the DB-backed
//! group-capability overlay that `conexus_core::capability`'s own
//! module doc deferred here. The `Tool` trait / `Requirement` enum /
//! `all_tools()` registry (the other half of Phase C, porting
//! `agent_mcp/core/authorize.py`) lands in a follow-up PR — see
//! `/home/dennis/.claude/plans/prancy-napping-pie.md`.

pub mod capabilities;

pub use capabilities::{resolve_capabilities, ResolveCapabilitiesInput};
