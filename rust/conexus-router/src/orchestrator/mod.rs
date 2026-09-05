//! Port of `agent_mcp/router/project_orchestrator.py` (1302 LOC,
//! Phase E2 PR 6, `conexus-router-orchestrator`) -- the router's
//! state machine + systemd shell-out that starts/stops per-project
//! backend processes on demand and idle-stops them.
//!
//! Split across several PRs, smallest/most-foundational first
//! (matching this migration's own established discipline for large
//! modules -- `task_tools.py`/`admin_tools.py` got the same
//! treatment): `runtime` (this PR, PR 6a -- pure in-memory state, zero
//! I/O) lands first, then `systemctl` (the shell-out + unit/socket-
//! path resolution, PR 6b), then `ensure` (the lazy-activation state
//! machine composing both, PR 6c), then `reaper` (the two background
//! sweep loops, PR 6d). `ProjectOrchestrator`'s own class surface is
//! NOT ported 1:1 -- a dedicated research pass found it has no real
//! production caller besides `.resolve()` (every other method is
//! duplicated independently by `admin_api.py`'s REST handlers, which
//! wrap the same module-level primitives with their own TOCTOU/
//! security hardening); this crate exposes the module-level
//! primitives directly instead of a thin facade class with no real
//! caller to justify it.

// No caller yet -- main.rs wires nothing orchestrator-facing until
// PR 6c (`ensure`) gives this state a real consumer, same
// helpers-ahead-of-their-first-consumer precedent as every other
// not-yet-wired module in this crate (mount.rs/path_policy.rs/
// identity.rs/project_registry.rs).
#![allow(dead_code)]

pub mod ensure;
pub mod primitives;
pub mod runtime;
